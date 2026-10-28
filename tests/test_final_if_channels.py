"""The final IF-by-IF self-cal leaves out the channels named by an exclusion spec, per IF."""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from lenspipe.channels import (
    excluded_channels_from_spec,
    include_spec_to_exclusions,
    selections_from_exclusions,
)
from lenspipe.cli import app
from lenspipe.config import FinalIfSelfcal, LenspipeConfig
from lenspipe.difmap.scripts import if_selfcal_block, stage1_commands
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.ui.commands import RunRequest, build_steps


def test_excluded_channels_from_spec_maps_casa_to_difmap() -> None:
    # 6 windows x 64 channels; spw 0 channels 0-1 -> DifMAP 1-2; spw 2 whole -> 129-192; plain 300 stays.
    out = excluded_channels_from_spec("spw0:0-1; spw2; 300", total_channels=384, channels_per_if=64)
    assert {1, 2, 300} <= out and set(range(129, 193)) <= out and len(out) == 67
    assert excluded_channels_from_spec(None, 384, 64) == set()
    with pytest.raises(ValueError, match="outside 0-5"):
        excluded_channels_from_spec("spw6", 384, 64)
    with pytest.raises(ValueError, match="outside 0-63"):
        excluded_channels_from_spec("spw1:64", 384, 64)
    with pytest.raises(ValueError, match="outside 1-384"):
        excluded_channels_from_spec("385", 384, 64)


def test_selections_apply_edge_default_then_exclusions() -> None:
    kept, skipped = selections_from_exclusions("spw1:0,2; spw3", if_count=4, channels_per_if=4, edge_channels=1)
    assert skipped == [4]
    assert [s.if_number for s in kept] == [1, 2, 3]
    assert kept[0].segments == [(2, 3)]  # edge default only
    assert kept[1].segments == [(6, 6)]  # IF 2 is 5-8; edge leaves 6-7; 'spw1:0,2' removes 5 and 7
    assert kept[2].segments == [(10, 11)]


def test_include_form_converts_to_exclusions() -> None:
    # IF 1 used channels 3-62 of 64 -> exclude 0-1 and 62-63; IF 2 used all -> nothing; unlisted IFs untouched.
    assert include_spec_to_exclusions("1:3-62; 2:1-64", 4, 64) == "spw0:0-1,62-63"
    assert include_spec_to_exclusions("2:5-60,62", 4, 64) == "spw1:0-3,60,62-63"
    assert include_spec_to_exclusions("1:1-64", 4, 64) is None
    # And the config migrates an old file transparently.
    old = FinalIfSelfcal.model_validate({"if_count": 4, "channels_per_if": 64, "channels": "1:3-62"})
    assert old.exclude_channels == "spw0:0-1,62-63"
    assert not hasattr(old, "channels")


def test_config_validates_and_round_trips_the_spec() -> None:
    final = FinalIfSelfcal(enabled=True, if_count=2, channels_per_if=4, exclude_channels="spw1:0,3")
    kept, skipped = final.selections()
    assert [s.segments for s in kept] == [[(1, 4)], [(6, 7)]] and skipped == []
    assert FinalIfSelfcal(exclude_channels="   ").exclude_channels is None
    with pytest.raises(ValueError, match="outside 0-1"):
        FinalIfSelfcal(if_count=2, channels_per_if=4, exclude_channels="spw2")
    cfg = LenspipeConfig().with_overrides(
        stage1={"final_if_selfcal": {"if_count": 2, "channels_per_if": 4, "exclude_channels": "spw0:1-2"}}
    )
    reloaded = LenspipeConfig.model_validate(tomllib.loads(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True))))
    assert reloaded.stage1.final_if_selfcal.exclude_channels == "spw0:1-2"


def test_script_selects_each_if_minus_its_exclusions_and_skips_empty_ifs() -> None:
    block, selections = if_selfcal_block(3, 4, 1, exclude_channels="spw1:1; spw2")
    assert block.split("\n\n") == [
        "! IF(s) skipped (no channels left after exclusions): 3",
        "! IF 1: channels 2-3\nselect i,2,3\nselfcal true,true,3600\nselfcal false,false,0.5",
        "! IF 2: channels 7\nselect i,7,7\nselfcal true,true,3600\nselfcal false,false,0.5",
    ]
    assert [len(s.channels) for s in selections] == [2, 1]
    cfg = LenspipeConfig().with_overrides(
        stage1={"final_if_selfcal": {"enabled": True, "if_count": 3, "channels_per_if": 4, "edge_channels": 0, "exclude_channels": "spw2:1"}}
    ).stage1
    text, ranges = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"), config=cfg,
    )
    assert "select i,9,9,11,12\n" in text  # IF 3 = channels 9-12 minus 0-based 1 (= 10): two runs on one select
    assert text.index("select i,9,9,11,12") < text.rindex("\nselect i\n") < text.index("wobs ")
    assert [(s.first, s.last) for s in ranges] == [(1, 4), (5, 8), (9, 12)]


def test_run_records_the_exclusions_in_metadata(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}},
        stage1={"final_if_selfcal": {"enabled": True, "if_count": 2, "channels_per_if": 4, "exclude_channels": "spw1:2"}},
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert all(r.ok for r in results), [r.message for r in results]
    meta = json.loads(results[0].paths.stage_metadata.read_text())["final_if_selfcal"]
    assert meta["enabled"] is True and meta["n_ifs"] == 2 and meta["n_ifs_calibrated"] == 2 and meta["skipped_ifs"] == []
    assert meta["exclude_channels_spec"] == "spw1:2"
    assert meta["selected_ranges_one_based"][1] == {
        "if_number": 2, "first_channel": 5, "last_channel": 8, "channels": [[5, 6], [8, 8]], "n_channels": 3,
    }
    assert "select i,5,6,8,8" in results[0].commands


def test_cli_and_run_page_pass_the_spec(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["stage1", str(tmp_path), "--final-if-selfcal", "--final-if-exclude-channels", "spw0:1", "--dry-run"])
    assert "No such option" not in result.output
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_final_if_selfcal=True, stage1_final_if_exclude="spw0:1; spw3"))
    assert steps[0].argv[-3:] == ["--final-if-selfcal", "--final-if-exclude-channels", "spw0:1; spw3"]
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_final_if_exclude=""))
    assert steps[0].argv[-2:] == ["--final-if-exclude-channels", ""]


def test_stage1_exclusions_accept_window_ranges() -> None:
    # The user's case, corrected for CASA numbering: 48 windows are spw0-47, drop channels 0-7 of each.
    kept, skipped = selections_from_exclusions("spw0-47:0-7", if_count=48, channels_per_if=64)
    assert skipped == [] and all(s.first == (s.if_number - 1) * 64 + 9 for s in kept)
    final = FinalIfSelfcal(enabled=True, if_count=48, channels_per_if=64, exclude_channels="spw0-47:0-7")
    assert len(final.selections()[0]) == 48
    with pytest.raises(ValueError, match=r"spw 48 is outside 0-47 .*48 windows are spw0 to spw47"):
        FinalIfSelfcal(if_count=48, channels_per_if=64, exclude_channels="spw1-48:1-8")
