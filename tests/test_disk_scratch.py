"""2.0.12: DifMAP keeps a hidden scratch copy of the input per process, so shards are capped by disk,
the doctor reports the scratch requirement, and stage2.scratch_dir moves the copies elsewhere."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import lenspipe.doctor as doctor_module
import lenspipe.stage2 as stage2_module
from lenspipe.config import LenspipeConfig, Stage2Config
from lenspipe.difmap.scripts import stage2_commands
from lenspipe.doctor import run_checks
from lenspipe.progress import Reporter
from lenspipe.project import DISK_RESERVE_BYTES, SCRATCH_PER_PROCESS, decide_shards, free_disk_bytes
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2

GIB = 1 << 30


# ---------------------------------------------------------------------------
# Shard decision


def test_auto_shards_are_capped_by_free_disk() -> None:
    kwargs = dict(fit_count=3072, input_bytes=10 * GIB, memory_fraction=0.5, memory_multiple=0.1,
                  total_memory_bytes=64 * GIB, cpu_count=24)
    roomy = decide_shards("auto", epoch_workers=1, free_disk_bytes=200 * GIB, **kwargs)
    assert roomy.cpu_cap == 8 and roomy.memory_cap >= 8
    assert roomy.disk_cap == 17 and roomy.shards == 8  # (200 - 5 reserve) / 11 per process
    assert roomy.scratch_bytes_per_process == int(10 * GIB * SCRATCH_PER_PROCESS)

    tight = decide_shards("auto", epoch_workers=1, free_disk_bytes=40 * GIB, **kwargs)
    assert tight.disk_cap == 3 and tight.shards == 3

    shared = decide_shards("auto", epoch_workers=2, free_disk_bytes=40 * GIB, **kwargs)
    assert shared.disk_cap == 1 and shared.shards == 1  # two epochs each keep their own copies

    none_fit = decide_shards("auto", epoch_workers=1, free_disk_bytes=8 * GIB, **kwargs)
    assert none_fit.disk_cap == 0 and none_fit.shards == 1 and none_fit.disk_short


def test_explicit_shards_ignore_the_disk_cap_but_record_it() -> None:
    decision = decide_shards(6, 3072, input_bytes=10 * GIB, free_disk_bytes=30 * GIB, cpu_count=24)
    assert decision.shards == 6 and decision.disk_cap == 2
    assert "disk_cap=2" in decision.describe()
    assert decision.to_dict()["free_disk_bytes"] == 30 * GIB


def test_no_disk_figure_means_no_disk_cap() -> None:
    decision = decide_shards("auto", 3072, input_bytes=GIB, cpu_count=4)
    assert decision.disk_cap is None and not decision.disk_short


def test_free_disk_bytes_walks_up_to_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "not" / "yet" / "there"
    assert free_disk_bytes(missing) == free_disk_bytes(tmp_path)
    assert free_disk_bytes(tmp_path) > 0
    assert DISK_RESERVE_BYTES == 5 * GIB


# ---------------------------------------------------------------------------
# Doctor


def _project_with_config(project: Path, fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    return cfg.with_overrides(stage2=stage2) if stage2 else cfg


@pytest.mark.parametrize(
    "free_bytes, expected_status, needle",
    [
        (500 * GIB, "ok", "Stage 2 scratch"),
        (1024, "fail", "not enough room for one DifMAP process"),  # inputs here are about 14 KiB
    ],
)
def test_doctor_disk_check_reports_the_scratch_requirement(
    project: Path, fake_difmap: Path, monkeypatch, free_bytes: int, expected_status: str, needle: str
) -> None:
    monkeypatch.setattr(doctor_module, "free_disk_bytes", lambda path: free_bytes)
    checks = {c.name: c for c in run_checks(project, _project_with_config(project, fake_difmap))}
    assert checks["disk"].status == expected_status, checks["disk"]
    assert needle in checks["disk"].detail or needle in (checks["disk"].fix or "")
    assert "hidden copy" in checks["disk"].detail


def test_doctor_disk_check_warns_when_the_plan_would_not_fit(project: Path, fake_difmap: Path, monkeypatch) -> None:
    """Explicit shards beyond the disk cap: the run would start, so warn rather than fail."""
    # Inputs here are 14 KiB, so make one process need more than a third of a tiny disk.
    monkeypatch.setattr(doctor_module, "free_disk_bytes", lambda path: DISK_RESERVE_BYTES + 40 * 1024)
    cfg = _project_with_config(project, fake_difmap, shards=8)
    checks = {c.name: c for c in run_checks(project, cfg)}
    assert checks["disk"].status == "warn", checks["disk"]
    assert "8 shard(s)" in checks["disk"].detail and "scratch_dir" in (checks["disk"].fix or "")


def test_doctor_measures_disk_at_the_scratch_dir_when_set(project: Path, fake_difmap: Path, monkeypatch, tmp_path: Path) -> None:
    seen: list[Path] = []

    def fake_free(path: Path) -> int:
        seen.append(Path(path))
        return 500 * GIB

    monkeypatch.setattr(doctor_module, "free_disk_bytes", fake_free)
    scratch = tmp_path / "bigdisk"
    checks = {c.name: c for c in run_checks(project, _project_with_config(project, fake_difmap, scratch_dir=str(scratch)))}
    assert scratch in seen and str(scratch) in checks["disk"].detail


# ---------------------------------------------------------------------------
# Stage 2: scratch_dir and unflag


def test_stage2_runs_difmap_in_the_scratch_dir_and_cleans_it_up(project: Path, fake_difmap: Path, monkeypatch, tmp_path: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    scratch = tmp_path / "scratch"
    cfg = _project_with_config(project, fake_difmap, mode="channel", shards=2, scratch_dir=str(scratch))
    run_stage1(project, cfg, reporter=quiet, workers=1)

    cwds: list[Path] = []
    real_run = stage2_module.run_difmap

    def spy(*args, **kwargs):
        cwd = Path(kwargs["cwd"])
        cwds.append(cwd)
        assert cwd.is_dir(), "DifMAP's working directory must exist before it starts"
        return real_run(*args, **kwargs)

    monkeypatch.setattr(stage2_module, "run_difmap", spy)
    results = run_stage2(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert results and all(r.ok for r in results), [r.message for r in results]
    assert cwds and all(cwd.parent == scratch and cwd.name.startswith("MG0414.A.channel") for cwd in cwds)
    assert not any(cwd.exists() for cwd in cwds)  # per-epoch scratch directory removed on completion
    assert scratch.is_dir()  # the configured root itself is left alone


def test_stage2_default_scratch_is_the_work_dir(project: Path, fake_difmap: Path, monkeypatch) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _project_with_config(project, fake_difmap, mode="channel", shards=1)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    cwds: list[Path] = []
    real_run = stage2_module.run_difmap
    monkeypatch.setattr(stage2_module, "run_difmap", lambda *a, **k: (cwds.append(Path(k["cwd"])), real_run(*a, **k))[1])
    run_stage2(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert cwds and cwds[0] == project / "stage2" / "MG0414.A" / ".MG0414.A.channel.work"


def test_stage2_unflag_is_explicit_about_all_channels() -> None:
    text = stage2_commands(
        calibrated_uvfits=Path("/p/x.cal.uvf"), stage2_model=Path("/p/x.mod"), fit_ranges=[(1, 1, 1)],
        work_directory=Path("/p/work"), mode="channel", config=Stage2Config(mode="channel", unflag=True),
    )
    assert "\nselect i\nunflag *, true\n" in text
