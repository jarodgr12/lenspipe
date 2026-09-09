"""Quick-look plots and error bars can be switched off from the configuration and the CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path

from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import plotting, run_stage3

runner = CliRunner()


def _config(fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    return cfg.with_overrides(stage2={"mode": "channel", "shards": 1, **stage2})


def test_stage2_plot_spectrum_off_writes_no_pngs(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    results = run_stage2(project, _config(fake_difmap, plot_spectrum=False), reporter=quiet, workers=1)
    assert all(r.ok for r in results)
    assert not list((project / "stage2").rglob("*.png"))
    manifest = json.loads(results[0].paths.manifest_file.read_text())
    assert manifest["plot"] is None and manifest["grouped_spectrum_plot"] is None
    assert json.loads(results[0].paths.metadata_file.read_text())["config"]["plot_spectrum"] is False


def test_stage2_error_bars_off_still_plots(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    results = run_stage2(project, _config(fake_difmap, plot_error_bars=False), reporter=quiet, workers=1)
    assert all(r.ok for r in results)
    assert results[0].paths.plot_file.is_file() and results[0].paths.grouped_plot_file.is_file()
    assert json.loads(results[0].paths.metadata_file.read_text())["config"]["plot_error_bars"] is False


def test_stage3_error_bars_toggle_reaches_every_figure(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap), reporter=quiet, workers=1)
    cfg = _config(fake_difmap).with_overrides(
        stage3={"plot_error_bars": False, "figure_formats": ["png"]}
    )
    summary = run_stage3(project, cfg, reporter=quiet, workers=2)  # workers=2 exercises the pool init
    assert summary.ok
    assert plotting.SHOW_ERROR_BARS is False
    per_visit = next((project / "stage3" / "MG0414.A").rglob("*.stage3.metadata.json"))
    assert json.loads(per_visit.read_text())["plot_style"]["error_bars"] is False
    assert len(list((project / "stage3").rglob("*.png"))) >= 12
    plotting.configure_error_bars(True)


def test_yerr_helper_respects_toggle() -> None:
    plotting.configure_error_bars(False)
    try:
        assert plotting._yerr([1, 2, 3]) is None
    finally:
        plotting.configure_error_bars(True)
    assert plotting._yerr([1, 2, 3]) == [1, 2, 3]


def test_cli_flags_map_to_config(project: Path, fake_difmap: Path) -> None:
    result = runner.invoke(app, ["stage1", str(project), "--difmap", str(fake_difmap)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["stage2", str(project), "--difmap", str(fake_difmap), "--mode", "channel", "--no-plots", "--no-error-bars"],
    )
    assert result.exit_code == 0, result.output
    assert not list((project / "stage2").rglob("*.png"))
    result = runner.invoke(app, ["stage3", str(project), "--product", "channel", "--no-error-bars", "--formats", "png", "--workers", "1"])
    assert result.exit_code == 0, result.output
    per_visit = next((project / "stage3" / "MG0414.A").rglob("*.stage3.metadata.json"))
    assert json.loads(per_visit.read_text())["plot_style"]["error_bars"] is False
    plotting.configure_error_bars(True)
