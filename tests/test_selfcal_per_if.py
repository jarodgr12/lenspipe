"""stage1.selfcal_per_if runs the self-cal schedule once per IF instead of once on the full band."""

from __future__ import annotations

import io
import json
from pathlib import Path

from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig, render_toml
from lenspipe.difmap.scripts import if_channel_ranges, stage1_commands
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.ui.commands import RunRequest, build_steps


def _script(cfg: LenspipeConfig) -> str:
    text, _ = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
        config=cfg.stage1,
    )
    return text


def test_if_channel_ranges_are_one_based_and_skip_edges() -> None:
    assert if_channel_ranges(3, 4, 1) == [(2, 3), (6, 7), (10, 11)]
    assert if_channel_ranges(2, 64, 0) == [(1, 64), (65, 128)]


def test_per_if_schedule_repeats_every_step_per_if_then_fits_the_full_band() -> None:
    cfg = LenspipeConfig().with_overrides(
        stage1={
            "selfcal": [[False, False, 3600, 50], [True, True, 1, 30, True]],
            "selfcal_per_if": True,
            "final_if_selfcal": {"enabled": False, "if_count": 2, "channels_per_if": 4, "edge_channels": 1},
        }
    )
    text = _script(cfg)
    one_if = "rmodel /i/x.gmod\nselfcal false,false,3600\nmodelfit 50\n\nselfcal true,true,1\nmodelfit 30\ngscale true\nmodelfit 30\n"
    assert "! IF 1: channels 2-3\nselect i,2,3\n" + one_if in text
    assert "! IF 2: channels 6-7\nselect i,6,7\n" + one_if in text
    assert text.count("select i,") == 2
    # Final model is a single full-band fit from the starting model, using the last step's iterations.
    tail = text.split("! Full band: fit the final model to the per-IF calibrated data.\n")[1]
    assert tail.startswith("select i\nrmodel /i/x.gmod\nmodelfit 30\n")
    assert "wmodel /o/x.gmod" in tail and tail.count("selfcal") == 0
    # The final-IF block was left disabled, so no second per-IF pass follows.
    assert "selfcal true,true,3600\nselfcal false,false,0.5" not in text


def test_per_if_is_off_by_default_and_leaves_the_band_schedule_unchanged() -> None:
    cfg = LenspipeConfig()
    assert cfg.stage1.selfcal_per_if is False
    text = _script(cfg)
    assert text.count("select i") == 1 and "! IF 1" not in text
    assert "selfcal_per_if = false" in render_toml()


def test_per_if_stage1_runs_and_counts_progress_per_if(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage1={
            "selfcal": [[False, False, 3600, 50], [False, False, 1, 50]],
            "selfcal_per_if": True,
            "final_if_selfcal": {"enabled": False, "if_count": 3, "channels_per_if": 4, "edge_channels": 0},
        },
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert results[0].ok, results[0].message
    log = results[0].paths.log_file.read_text()
    assert log.count("Applying selfcal corrections") == 6  # 2 steps x 3 IFs
    meta = json.loads(results[0].paths.stage_metadata.read_text())
    assert meta["config"]["selfcal_per_if"] is True
    assert meta["final_if_selfcal"]["enabled"] is False


def test_cli_and_run_page_can_override_per_if(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["stage1", str(tmp_path), "--selfcal-per-if", "--dry-run"])
    assert "No such option" not in result.output
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_selfcal_per_if=False))
    assert steps[0].argv[-1] == "--no-selfcal-per-if"
