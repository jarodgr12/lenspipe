"""stage2.exclude_channels: channels left out of the fits, per spectral window, in either mode."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig, Stage2Config
from lenspipe.difmap.scripts import stage2_commands
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import make_fit_ranges, make_fits, product_tag, run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.ui.commands import RunRequest, build_steps
from lenspipe.ui.pages_run import _expected_tag


def test_make_fits_if_mode_removes_channels_and_skips_empty_windows() -> None:
    # 4 IFs x 4 channels, edge 0: exclude channel 1 of spw 1 (DifMAP 6) and all of spw 3.
    fits, skipped = make_fits(16, "if", 4, None, 0, "spw1:1; spw3")
    assert skipped == [4]
    assert [(f.fit_index, f.segments) for f in fits] == [
        (1, ((1, 4),)), (2, ((5, 5), (7, 8))), (3, ((9, 12),)),
    ]
    assert fits[1].select_arguments == "5,5,7,8" and fits[1].n_channels == 3 and not fits[1].contiguous
    # The span view used by shard plans and legacy comparisons.
    assert make_fit_ranges(16, "if", 4, None, 0, "spw1:1; spw3") == [(1, 1, 4), (2, 5, 8), (3, 9, 12)]
    # Edge exclusion and the spec combine; no exclusions reproduce the old ranges exactly.
    assert make_fit_ranges(16, "if", 4, None, 1) == [(1, 2, 3), (2, 6, 7), (3, 10, 11), (4, 14, 15)]
    assert make_fit_ranges(16, "if", 4, None, 1, "spw0:1") == [(1, 3, 3), (2, 6, 7), (3, 10, 11), (4, 14, 15)]
    with pytest.raises(ValueError, match="leaves no IF"):
        make_fits(8, "if", 4, None, 0, "spw0; spw1")


def test_make_fits_channel_mode_drops_excluded_channels() -> None:
    fits, skipped = make_fits(8, "channel", 4, "1-6", 0, "spw0:0; 5")
    assert skipped == [] and [f.fit_index for f in fits] == [2, 3, 4, 6]
    with pytest.raises(ValueError, match="leaves no channel"):
        make_fits(8, "channel", 4, "1-2", 0, "spw0:0-1")


def test_product_tag_carries_the_exclusions() -> None:
    assert product_tag(Stage2Config(mode="if", channels_per_if=64, exclude_edge_channels=3)) == "if64_edge3"
    assert product_tag(Stage2Config(mode="if", channels_per_if=64, exclude_edge_channels=3, exclude_channels="spw3:0-3,60-63; spw7")) == "if64_edge3_x_spw3_0-3_60-63_spw7"
    assert product_tag(Stage2Config(mode="channel", exclude_channels="spw7")) == "channel_x_spw7"
    assert product_tag(Stage2Config(mode="channel", channels="1-3", exclude_channels="spw7")) == "channel_1-3_x_spw7"
    cfg = LenspipeConfig()
    assert _expected_tag(cfg, "if", 64, None, 3, "spw7") == "if64_edge3_x_spw7"
    assert _expected_tag(cfg, "if", 64, None, 3, None) == "if64_edge3"


def test_stage2_commands_select_several_runs_for_a_gapped_fit() -> None:
    fits, _ = make_fits(16, "if", 4, None, 0, "spw1:1")
    text = stage2_commands(
        calibrated_uvfits=Path("/d/x.cal.uvf"), stage2_model=Path("/d/x.stage2.mod"),
        fit_ranges=[f.as_range() for f in fits], work_directory=Path("/w"), mode="if", config=Stage2Config(mode="if"),
        segments={f.fit_index: f.segments for f in fits},
    )
    assert "select i,1,4\n" in text and "select i,5,5,7,8\n" in text and "select i,9,12\n" in text
    # Without segments the old contiguous form is emitted (legacy parity).
    plain = stage2_commands(
        calibrated_uvfits=Path("/d/x.cal.uvf"), stage2_model=Path("/d/x.stage2.mod"),
        fit_ranges=[(2, 5, 8)], work_directory=Path("/w"), mode="if", config=Stage2Config(mode="if"),
    )
    assert "select i,5,8\n" in plain


def test_stage2_if_mode_with_exclusions_end_to_end_and_stage3_reads_it(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    base = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}})
    run_stage1(project, base, reporter=quiet, workers=1)
    # 2 IFs x 4 channels: drop channel 1 of spw 0 (DifMAP 2) and the whole of spw 1 -> one fit, over 1,3-4.
    cfg = base.with_overrides(stage2={"mode": "if", "channels_per_if": 4, "exclude_channels": "spw0:1; spw1"})
    dry = run_stage2(project, cfg, reporter=quiet, workers=1, dry_run=True, epochs={"A"})
    assert "select i,1,1,3,4\n" in (dry[0].commands or "") and "select i,5" not in (dry[0].commands or "")
    results = run_stage2(project, cfg, reporter=quiet, workers=1)
    assert all(r.ok for r in results), [r.message for r in results]
    tag = "if4_edge0_x_spw0_1_spw1"
    directory = project / "stage2" / "full-band" / "MG0414.A"
    with (directory / f"MG0414.A.{tag}.spectrum.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [int(r["fit_index"]) for r in rows] == [1]
    assert (int(rows[0]["first_channel"]), int(rows[0]["last_channel"]), int(rows[0]["channels_averaged"])) == (1, 4, 3)
    meta = json.loads((directory / f"MG0414.A.{tag}.stage2.metadata.json").read_text())
    assert meta["channel_exclusion"] == {
        "exclude_channels": "spw0:1; spw1", "skipped_ifs": [2], "non_contiguous_fits": {"1": [[1, 1], [3, 4]]},
    }
    assert results[0].message.startswith("1/1 fits ok")

    # Stage 3 accepts the tagged product. One fit is too few for a power law, so use a gentler spec.
    gentle = base.with_overrides(stage2={"mode": "channel", "exclude_channels": "spw1:3"})
    results = run_stage2(project, gentle, reporter=quiet, workers=1)
    assert all(r.ok for r in results), [r.message for r in results]
    summary = run_stage3(project, gentle.with_overrides(stage3={"figure_formats": ["png"]}), products={"channel_x_spw1_3"}, reporter=quiet, workers=1)
    assert summary.ok
    s3 = next((project / "stage3").rglob("MG0414.A.channel_x_spw1_3.stage3.metadata.json"))
    assert json.loads(s3.read_text())["n_measurements"] == 7


def test_cli_and_run_page_pass_the_exclusions(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["stage2", str(tmp_path), "--mode", "if", "--exclude-channels", "spw7", "--dry-run"])
    assert "No such option" not in result.output
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[2], stage2_mode="if", stage2_exclude_channels="spw3:0-3; spw7"))
    assert "--exclude-channels" in steps[0].argv and steps[0].argv[steps[0].argv.index("--exclude-channels") + 1] == "spw3:0-3; spw7"
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[2], stage2_mode="if", stage2_exclude_channels=""))
    assert steps[0].argv[steps[0].argv.index("--exclude-channels") + 1] == ""  # clears a configured spec
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[2], stage2_mode="if"))
    assert "--exclude-channels" not in steps[0].argv


def test_stage2_exclusions_accept_window_ranges() -> None:
    fits, skipped = make_fits(16, "if", 4, None, 0, "spw0-2:3; spw3")
    assert skipped == [4] and [f.segments for f in fits] == [((1, 3),), ((5, 7),), ((9, 11),)]
    assert product_tag(Stage2Config(mode="if", channels_per_if=4, exclude_channels="spw0-2:3; spw3")) == "if4_edge0_x_spw0-2_3_spw3"
