"""Memory-aware sharding, resumable Stage 2, figure formats, and provenance checks."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.project import decide_shards, inventory
from lenspipe.provenance import verify_project
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage2_shards import work_directory_for
from lenspipe.stage3 import run_stage3
from tests.conftest import write_synthetic_uvfits

GIB = 1 << 30


def _config(fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}})
    return cfg.with_overrides(stage2=stage2) if stage2 else cfg


@pytest.fixture
def quiet() -> Reporter:
    return Reporter(stream=io.StringIO())


# ---------------------------------------------------------------------------
# Memory-aware sharding


def test_auto_shards_respect_memory_budget() -> None:
    kwargs = dict(fit_count=3072, input_bytes=GIB, memory_fraction=0.5, memory_multiple=3.0,
                  total_memory_bytes=16 * GIB, cpu_count=12)
    one_epoch = decide_shards("auto", epoch_workers=1, **kwargs)
    two_epochs = decide_shards("auto", epoch_workers=2, **kwargs)
    assert one_epoch.cpu_cap == 8  # 11 spare cores, capped at 8
    assert one_epoch.memory_cap == 2 and one_epoch.shards == 2  # 8 GiB budget / 3 GiB per process
    assert two_epochs.cpu_cap == 5  # the 11 spare cores are shared by two epochs
    assert two_epochs.memory_cap == 1 and two_epochs.shards == 1  # budget halves per concurrent epoch


def test_cpu_cap_never_oversubscribes_the_machine() -> None:
    cores = 10
    for workers in (1, 2, 3, 4, 9, 20):
        decision = decide_shards("auto", 3072, epoch_workers=workers, cpu_count=cores)
        assert decision.cpu_cap * workers <= max(cores - 1, workers)
        assert decision.cpu_cap >= 1


def test_auto_shards_fall_back_to_cpu_without_size() -> None:
    decision = decide_shards("auto", 3072, cpu_count=4)
    assert decision.shards == 3 and decision.memory_cap is None
    assert decide_shards("auto", 2, cpu_count=32).shards == 2  # never more shards than fits


def test_explicit_shards_are_honoured_but_capped_by_fit_count() -> None:
    decision = decide_shards(6, 3072, input_bytes=GIB, total_memory_bytes=4 * GIB, cpu_count=2)
    assert decision.shards == 6 and decision.memory_cap == 1
    assert decide_shards(6, 4).shards == 4


def test_memory_settings_are_validated() -> None:
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage2": {"memory_fraction": 1.5}})
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage2": {"memory_multiple": 0}})


# ---------------------------------------------------------------------------
# Resumable Stage 2


def test_stage2_resumes_after_a_crashed_shard(project: Path, fake_difmap: Path, quiet, monkeypatch, tmp_path: Path) -> None:
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    clean_root = tmp_path / "clean"
    shutil.copytree(project, clean_root)
    clean = run_stage2(clean_root, _config(fake_difmap, mode="channel", shards=4), reporter=quiet)
    assert all(r.ok for r in clean)

    monkeypatch.setenv("FAKE_DIFMAP_FAIL_ON_CHANNEL", "5")
    crashed = run_stage2(project, _config(fake_difmap, mode="channel", shards=4), reporter=quiet, workers=1)
    assert all(r.status == "failed" for r in crashed)
    assert "--resume" in crashed[0].message
    work_dir = work_directory_for(crashed[0].paths.output_directory, crashed[0].paths.product_prefix)
    assert work_dir.is_dir() and (work_dir / "plan.json").is_file()
    done_markers = sorted(p.name for p in work_dir.glob("shard_*.done.json"))
    assert len(done_markers) == 3, done_markers  # three of four shards finished
    assert not crashed[0].paths.spectrum_csv.exists()

    monkeypatch.delenv("FAKE_DIFMAP_FAIL_ON_CHANNEL")
    blocked = run_stage2(project, _config(fake_difmap, mode="channel", shards=4), reporter=quiet, workers=1)
    assert all(r.status == "failed" and "interrupted run" in r.message for r in blocked)

    resumed = run_stage2(
        project, _config(fake_difmap, mode="channel", shards=4), reporter=quiet, workers=1, resume=True
    )
    assert all(r.ok and r.status == "completed" for r in resumed), [r.message for r in resumed]
    assert not work_dir.exists()
    for epoch in ("A", "B"):
        name = f"MG0414.{epoch}.channel"
        ours = (project / "stage2" / f"MG0414.{epoch}" / f"{name}.spectrum.csv").read_text()
        theirs = (clean_root / "stage2" / f"MG0414.{epoch}" / f"{name}.spectrum.csv").read_text()
        assert ours == theirs
        meta = json.loads((project / "stage2" / f"MG0414.{epoch}" / f"{name}.stage2.metadata.json").read_text())
        assert meta["execution"]["resumed"] is True
        assert meta["execution"]["shards"] == 4
        assert meta["execution"]["shard_decision"]["requested"] == 4
        assert meta["provenance"]["calibrated_uvfits"]["sha256"]


def test_resume_after_parse_failure_reuses_finished_difmap_work(project: Path, fake_difmap: Path, quiet, monkeypatch, tmp_path: Path) -> None:
    """The 1555 epoch B situation: DifMAP finished, parsing failed, nothing should re-run."""
    import lenspipe.stage2 as stage2_module

    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    clean_root = tmp_path / "clean"
    shutil.copytree(project, clean_root)
    run_stage2(clean_root, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1)

    def broken_parser(*args, **kwargs):
        raise ValueError("Fit 3033: RMS marker encountered without a preceding final Flux/Stdev table.")

    monkeypatch.setattr(stage2_module, "modelfit_flux_measurements_by_fit", broken_parser)
    failed = run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1, epochs={"A"})
    assert failed[0].status == "failed" and "--resume" in failed[0].message
    work_dir = work_directory_for(failed[0].paths.output_directory, failed[0].paths.product_prefix)
    done_before = sorted((p.name, p.stat().st_mtime_ns) for p in work_dir.glob("shard_*.done.json"))
    assert len(done_before) == 2  # both shards finished; only the parse failed
    assert failed[0].paths.log_file.exists()

    monkeypatch.undo()
    resumed = run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1, epochs={"A"}, resume=True)
    assert resumed[0].ok, resumed[0].message
    csv = failed[0].paths.spectrum_csv
    assert csv.read_text() == (clean_root / "stage2" / "MG0414.A" / "MG0414.A.channel.spectrum.csv").read_text()
    meta = json.loads(failed[0].paths.metadata_file.read_text())
    assert meta["execution"]["resumed"] is True
    assert not work_dir.exists()


def test_resume_refuses_when_settings_changed(project: Path, fake_difmap: Path, quiet, monkeypatch) -> None:
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    monkeypatch.setenv("FAKE_DIFMAP_FAIL_ON_CHANNEL", "2")
    run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1, epochs={"A"})
    monkeypatch.delenv("FAKE_DIFMAP_FAIL_ON_CHANNEL")
    changed = run_stage2(
        project, _config(fake_difmap, mode="channel", shards=2, modelfit_iterations=5),
        reporter=quiet, workers=1, resume=True, epochs={"A"},
    )
    assert changed[0].status == "failed" and "settings changed" in changed[0].message
    restarted = run_stage2(
        project, _config(fake_difmap, mode="channel", shards=2, modelfit_iterations=5),
        reporter=quiet, workers=1, overwrite=True, epochs={"A"},
    )
    assert restarted[0].ok


def test_stage2_survives_stderr_warnings_mid_line(project: Path, fake_difmap: Path, quiet, monkeypatch, tmp_path: Path) -> None:
    """The fake writes DifMAP's pixel warning to stderr mid-line, as 2.5q does; separate
    stream capture keeps the table rule intact and the products identical."""
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    clean_root = tmp_path / "clean"
    shutil.copytree(project, clean_root)
    run_stage2(clean_root, _config(fake_difmap, mode="channel", shards=1), reporter=quiet, workers=1)

    monkeypatch.setenv("FAKE_DIFMAP_STDERR_SPLIT", "1")
    results = run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1)
    assert all(r.ok for r in results), [r.message for r in results]
    for epoch in ("A", "B"):
        name = f"MG0414.{epoch}.channel"
        ours = (project / "stage2" / f"MG0414.{epoch}" / f"{name}.spectrum.csv").read_text()
        assert ours == (clean_root / "stage2" / f"MG0414.{epoch}" / f"{name}.spectrum.csv").read_text()
        log = (project / "stage2" / f"MG0414.{epoch}" / f"{name}.stage2.difmap.log").read_text()
        assert "! [stderr] Your choice of large map pixels" in log
        assert "#Your choice" not in log


# ---------------------------------------------------------------------------
# Figure formats


@pytest.mark.parametrize("formats, present, absent", [(["png"], ".png", ".pdf"), (["pdf"], ".pdf", ".png")])
def test_stage3_figure_formats(project: Path, fake_difmap: Path, quiet, formats, present, absent) -> None:
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap, mode="channel", shards=1), reporter=quiet, workers=1)
    cfg = _config(fake_difmap).with_overrides(stage3={"figure_formats": formats})
    summary = run_stage3(project, cfg, reporter=quiet, workers=2)
    assert summary.ok
    figures = list((project / "stage3").rglob("*" + present))
    assert figures
    assert not list((project / "stage3").rglob("*" + absent))
    meta = next((project / "stage3").rglob("*.combined.stage3.metadata.json"))
    assert json.loads(meta.read_text())["figure_formats"] == formats


def test_figure_formats_validation() -> None:
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage3": {"figure_formats": []}})
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage3": {"figure_formats": ["jpg"]}})
    assert LenspipeConfig.model_validate({"stage3": {"figure_formats": ["png", "png"]}}).stage3.figure_formats == ["png"]


# ---------------------------------------------------------------------------
# Provenance


def _statuses(root: Path) -> dict[str, str]:
    return {f"s{c.stage}:{c.key}": c.status for c in verify_project(root)}


def test_verify_tracks_changes_through_the_stages(project: Path, fake_difmap: Path, quiet) -> None:
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap, mode="channel", shards=1), reporter=quiet, workers=1)
    assert run_stage3(project, _config(fake_difmap), reporter=quiet, workers=1).ok
    statuses = _statuses(project)
    assert statuses and set(statuses.values()) == {"fresh"}

    # Touch without changing content: still fresh (hash confirms it).
    uvfits_b = project / "inputs" / "MG0414.B.uvfits"
    uvfits_b.touch()
    assert _statuses(project)["s1:MG0414.B"] == "fresh"

    # Replace epoch A's input: stage 1 stale, everything downstream upstream-stale, B untouched.
    write_synthetic_uvfits(project / "inputs" / "MG0414.A.uvfits", seed=99)
    statuses = _statuses(project)
    assert statuses["s1:MG0414.A"] == "stale"
    assert statuses["s2:MG0414.A.channel"] == "upstream-stale"
    assert statuses["s3:MG0414.A.channel"] == "upstream-stale"
    assert statuses["s3:MG0414.combined.channel"] == "upstream-stale"
    assert statuses["s1:MG0414.B"] == "fresh" and statuses["s2:MG0414.B.channel"] == "fresh"

    # Re-running stage 1 for A makes it fresh and exposes stage 2 as directly stale.
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1, epochs={"A"}, overwrite=True)
    statuses = _statuses(project)
    assert statuses["s1:MG0414.A"] == "fresh"
    assert statuses["s2:MG0414.A.channel"] == "stale"

    summary = inventory(project)
    row_a = next(row for row in summary["epochs"] if row["epoch"] == "A")
    assert row_a["stage1"]["status"] == "fresh"
    assert row_a["stage2"][0]["status"] == "stale"
    assert summary["freshness"]["stale"] >= 1


def test_verify_cli_exit_codes(project: Path, fake_difmap: Path, quiet) -> None:
    runner = CliRunner()
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    result = runner.invoke(app, ["verify", str(project)])
    assert result.exit_code == 0 and "fresh" in result.output
    write_synthetic_uvfits(project / "inputs" / "MG0414.A.uvfits", seed=7)
    result = runner.invoke(app, ["verify", str(project), "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.output.split("\n1 stale")[0]) if "1 stale" in result.output else json.loads(result.output)
    assert any(item["status"] == "stale" for item in payload)


def test_legacy_products_without_provenance_are_unknown(project: Path) -> None:
    stage1_dir = project / "stage1" / "MG0414.A"
    stage1_dir.mkdir(parents=True)
    (stage1_dir / "MG0414.A.stage1.metadata.json").write_text(
        json.dumps({"stage": 1, "source": "MG0414", "epoch": "A", "version": "1.1.0"})
    )
    checks = verify_project(project)
    assert checks[0].status == "unknown"
