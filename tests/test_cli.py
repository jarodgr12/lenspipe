"""The command line drives the whole pipeline and reports configuration problems clearly."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from lenspipe.cli import app

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, [str(a) for a in args], catch_exceptions=False)


def test_init_and_describe(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    result = _invoke("init", root)
    assert result.exit_code == 0, result.output
    assert (root / "lenspipe.toml").is_file() and (root / "inputs").is_dir()
    assert _invoke("init", root).exit_code == 1  # refuses to clobber without --force
    described = _invoke("describe", root)
    assert described.exit_code == 0
    payload = json.loads(described.output)
    assert payload["stage2"]["shards"] == "auto" and payload["stage3"]["rcusp_images"] == ["A1", "A2", "B"]


def test_invalid_config_is_reported_not_traced(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lenspipe.toml").write_text('[stage2]\nmode = "sideways"\n')
    result = _invoke("describe", root)
    assert result.exit_code == 1
    assert "invalid configuration" in result.output and "stage2.mode" in result.output
    (root / "lenspipe.toml").write_text("[stage2\n")
    result = _invoke("describe", root)
    assert result.exit_code == 1 and "not valid TOML" in result.output


def test_run_all_stages_then_inventory(project: Path, fake_difmap: Path) -> None:
    _invoke("init", project)
    result = _invoke(
        "stage1", project, "--difmap", fake_difmap, "--workers", "2",
    )
    assert result.exit_code == 0, result.output
    result = _invoke(
        "stage2", project, "--difmap", fake_difmap, "--mode", "channel", "--shards", "2",
    )
    assert result.exit_code == 0, result.output
    assert "fits=8, shards=2" in result.output
    result = _invoke("stage3", project, "--product", "channel", "--workers", "1", "--no-annotations")
    assert result.exit_code == 0, result.output
    assert not list((project / "stage3").rglob("*spectra_annotated*"))

    inventory = _invoke("inventory", project, "--json")
    payload = json.loads(inventory.output)
    assert [row["epoch"] for row in payload["epochs"]] == ["A", "B"]
    assert all(row["stage1"] and row["stage2"][0]["product"] == "channel" for row in payload["epochs"])
    assert payload["combined"][0]["n_visits"] == 2

    # Re-running without --overwrite skips and is not a failure for the operator.
    again = _invoke("stage2", project, "--difmap", fake_difmap, "--mode", "channel")
    assert "skipped=2" in again.output and again.exit_code == 2

    listing = _invoke("stage3", project, "--list-products")
    assert listing.exit_code == 0 and "MG0414.A: channel" in listing.output


def test_dry_run_prints_commands_without_writing(project: Path, fake_difmap: Path) -> None:
    result = _invoke("stage1", project, "--dry-run", "--epoch", "A")
    assert result.exit_code == 0
    assert 'print "STAGE1_RMS", imstat(rms)' in result.output
    assert not (project / "stage1").exists()


def test_stage3_with_missing_stage2_is_a_clean_error(project: Path) -> None:
    result = _invoke("stage3", project)
    assert result.exit_code == 1 and "Stage 2 directory not found" in result.output
