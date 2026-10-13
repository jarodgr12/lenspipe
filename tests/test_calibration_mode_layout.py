"""Stage trees are split per calibration mode: stage<N>/<mode>/...; flat pre-2.1 projects migrate."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.project import Layout, inventory, migrate_flat_layout
from lenspipe.provenance import verify_project
from lenspipe.stage2 import discover_inputs, make_paths, run_stage2
from lenspipe.ui.pages_results import _epoch_options, resolve_target


def _stage1_meta(root: Path, prefix: str, mode: str | None, **extra) -> Path:
    directory = root / "stage1" / (f"{mode}/{prefix}" if mode else prefix)
    directory.mkdir(parents=True, exist_ok=True)
    source, epoch = prefix.rsplit(".", 1)
    payload = {"stage": 1, "source": source, "epoch": epoch, "version": "2.1.0", "provenance": {}, **extra}
    path = directory / f"{prefix}.stage1.metadata.json"
    path.write_text(json.dumps(payload))
    return path


def test_flat_layout_migrates_into_full_band_trees(tmp_path: Path) -> None:
    root = tmp_path / "old"
    (root / "inputs").mkdir(parents=True)
    _stage1_meta(root, "S.A", None)
    (root / "stage2" / "S.A").mkdir(parents=True)
    (root / "stage2" / "S.A" / "S.A.channel.stage2.metadata.json").write_text(
        json.dumps({"stage": 2, "source": "S", "epoch": "A", "mode": "channel", "provenance": {}})
    )
    (root / "stage3" / "S.A" / "channel").mkdir(parents=True)
    (root / "stage3" / "S.A" / "channel" / "S.A.channel.stage3.metadata.json").write_text(
        json.dumps({"stage": 3, "source": "S", "epoch": "A", "product_tag": "channel", "provenance": {}})
    )
    (root / "stage3" / "combined" / "S" / "channel").mkdir(parents=True)
    (root / "stage2" / ".scratch").mkdir()  # hidden dirs are not epoch directories and stay put

    notes = migrate_flat_layout(root)
    assert len(notes) == 4 and all(note.startswith("moved ") for note in notes)
    assert (root / "stage1" / "full-band" / "S.A" / "S.A.stage1.metadata.json").is_file()
    assert (root / "stage2" / "full-band" / "S.A" / "S.A.channel.stage2.metadata.json").is_file()
    assert (root / "stage3" / "full-band" / "S.A" / "channel").is_dir()
    assert (root / "stage3" / "full-band" / "combined" / "S" / "channel").is_dir()
    assert not (root / "stage1" / "S.A").exists() and (root / "stage2" / ".scratch").is_dir()
    assert migrate_flat_layout(root) == []  # idempotent

    # A second flat directory whose destination exists is left alone and reported.
    _stage1_meta(root, "S.A", None)
    notes = migrate_flat_layout(root)
    assert len(notes) == 1 and notes[0].startswith("left ")


def test_inventory_migrates_and_lists_one_row_per_mode(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "S.A.uvfits").write_bytes(b"x")
    (root / "inputs" / "S.B.uvfits").write_bytes(b"x")
    (root / "inputs" / "S.gmod").write_text("! GROUP A\n! COMPONENT a\n1 0 0\n")
    _stage1_meta(root, "S.A", None, final_residual_rms_jy_per_beam=0.5)  # flat, pre-2.1
    _stage1_meta(root, "S.A", "per-if", native_residual_rms_jy_per_beam=0.7)  # no common-grid pass yet

    summary = inventory(root)
    assert (root / "stage1" / "full-band" / "S.A").is_dir()  # migrated on the way
    rows = [(r["epoch"], r["calibration_mode"]) for r in summary["epochs"]]
    assert rows == [("A", "full-band"), ("A", "per-if"), ("B", None)]
    by_mode = {r["calibration_mode"]: r for r in summary["epochs"] if r["epoch"] == "A"}
    assert by_mode["full-band"]["stage1"]["rms_jy_per_beam"] == 0.5
    assert by_mode["per-if"]["stage1"]["rms_jy_per_beam"] == 0.7  # falls back to the native RMS
    assert all(r["input"] and r["master_model"] for r in summary["epochs"])
    assert {c.calibration_mode for c in verify_project(root)} == {"full-band", "per-if"}

    options = _epoch_options({"epochs": [{**by_mode["per-if"], "stage2": [{"product": "if64_edge0"}]}], "combined": []})
    assert options == {"per-if/S.A": "S.A  [per-if]"}
    assert resolve_target(root, "per-if/S.A", 2, "if64_edge0").directory == root / "stage2" / "per-if" / "S.A"


def test_stage2_reads_the_tree_of_the_configured_calibration_mode(project: Path, fake_difmap: Path) -> None:
    from lenspipe.stage1 import run_stage1

    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}}, stage2={"mode": "channel", "shards": 1}
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1)
    assert all(r.ok for r in results), [r.message for r in results]
    layout = Layout.at(project)
    assert layout.modes_present(1) == ["full-band"]
    # Stand in for a per-IF Stage 1 run: the same products under stage1/per-if/.
    shutil.copytree(layout.stage_dir(1, "full-band"), layout.stage_dir(1, "per-if"))

    per_if = cfg.with_overrides(stage1={"calibration_mode": "per-if"})
    assert [p.parent.parent.name for p in discover_inputs(project, "*.cal.uvf", None, "per-if")] == ["per-if"] * 2
    assert make_paths(project, discover_inputs(project, "*.cal.uvf", None, "per-if")[0], "channel", "per-if").output_directory == (
        layout.stage_dir(2, "per-if") / "MG0414.A"
    )
    results = run_stage2(project, per_if, reporter=quiet, workers=1, epochs={"A"})
    assert results[0].ok, results[0].message
    assert results[0].paths.output_directory == layout.stage_dir(2, "per-if") / "MG0414.A"
    meta = json.loads(results[0].paths.metadata_file.read_text())
    assert meta["calibration_mode"] == "per-if"
    assert meta["stage1_directory"] == "stage1/per-if/MG0414.A"
    assert not layout.stage_dir(2, "full-band").exists()

    # Asking for a mode that has no Stage 1 tree is a clear error naming the ones that exist.
    shutil.rmtree(layout.stage_dir(1, "full-band"))
    with pytest.raises(FileNotFoundError, match="calibration mode full-band.*per-if"):
        run_stage2(project, cfg, reporter=quiet, workers=1)

    summary = inventory(project)
    assert [(r["epoch"], r["calibration_mode"], len(r["stage2"])) for r in summary["epochs"]] == [
        ("A", "per-if", 1), ("B", "per-if", 0),
    ]
