"""The final IF-by-IF self-cal can use a different channel selection for each IF."""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest
import tomli_w
from typer.testing import CliRunner

from lenspipe.channels import parse_if_channel_spec, segments
from lenspipe.cli import app
from lenspipe.config import FinalIfSelfcal, LenspipeConfig
from lenspipe.difmap.scripts import if_selfcal_block, stage1_commands
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.ui.commands import RunRequest, build_steps


def test_spec_parsing_per_if_with_edge_default() -> None:
    sel = parse_if_channel_spec("1:3-62; 3:5-60,62; 5-6:1-64", if_count=6, channels_per_if=64, edge_channels=2)
    assert [s.if_number for s in sel] == [1, 2, 3, 4, 5, 6]
    assert sel[0].segments == [(3, 62)]  # within-IF 3-62 of IF 1
    assert sel[1].segments == [(67, 126)]  # unlisted IF 2: edge 2 -> within 3-62 -> absolute 67-126
    assert sel[2].segments == [(133, 188), (190, 190)]  # IF 3: 5-60 and 62 -> two runs
    assert sel[2].select_arguments == "133,188,190,190" and sel[2].label == "133-188 190"
    assert sel[4].segments == [(257, 320)] and sel[5].segments == [(321, 384)]
    assert len(sel[2].channels) == 57
    assert segments([1, 2, 3, 7, 9, 10]) == [(1, 3), (7, 7), (9, 10)]
    assert [s.segments for s in parse_if_channel_spec(None, 2, 4, 1)] == [[(2, 3)], [(6, 7)]]
    assert [s.segments for s in parse_if_channel_spec("  ;  ", 2, 4, 0)] == [[(1, 4)], [(5, 8)]]


@pytest.mark.parametrize(
    "spec, message",
    [
        ("3-62", "IF:channels"),
        ("9:1-10", "outside 1-8"),
        ("2:0-10", "within the IF"),
        ("2:1-70", "within the IF"),
        ("2:10-5", "descending"),
        ("1:1-10; 1:20-30", "more than once"),
        ("1-2:1-10; 2:20-30", "more than once"),
        ("x:1-10", "bad IF"),
        ("2:", "no channels"),
    ],
)
def test_spec_errors(spec: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_if_channel_spec(spec, if_count=8, channels_per_if=64)


def test_config_validates_and_round_trips_the_spec() -> None:
    final = FinalIfSelfcal(enabled=True, if_count=2, channels_per_if=4, channels="2:1-2,4")
    assert [s.segments for s in final.selections()] == [[(1, 4)], [(5, 6), (8, 8)]]
    assert FinalIfSelfcal(channels="   ").channels is None
    with pytest.raises(ValueError, match="outside 1-2"):
        FinalIfSelfcal(if_count=2, channels_per_if=4, channels="3:1-2")
    cfg = LenspipeConfig().with_overrides(stage1={"final_if_selfcal": {"if_count": 2, "channels_per_if": 4, "channels": "1:2-3"}})
    reloaded = LenspipeConfig.model_validate(tomllib.loads(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True))))
    assert reloaded.stage1.final_if_selfcal.channels == "1:2-3"
    parsed = LenspipeConfig.model_validate(
        tomllib.loads('[stage1.final_if_selfcal]\nenabled = true\nif_count = 2\nchannels_per_if = 4\nchannels = "2:1-2,4"\n')
    )
    assert parsed.stage1.final_if_selfcal.channels == "2:1-2,4"


def test_script_selects_each_if_its_own_channels() -> None:
    block, selections = if_selfcal_block(3, 4, 1, channels="1:1-4; 3:1,3-4")
    assert block.split("\n\n") == [
        "! IF 1: channels 1-4\nselect i,1,4\nselfcal true,true,3600\nselfcal false,false,0.5",
        "! IF 2: channels 6-7\nselect i,6,7\nselfcal true,true,3600\nselfcal false,false,0.5",
        "! IF 3: channels 9 11-12\nselect i,9,9,11,12\nselfcal true,true,3600\nselfcal false,false,0.5",
    ]
    assert [len(s.channels) for s in selections] == [4, 2, 3]
    cfg = LenspipeConfig().with_overrides(
        stage1={"final_if_selfcal": {"enabled": True, "if_count": 3, "channels_per_if": 4, "edge_channels": 1, "channels": "3:1,3-4"}}
    ).stage1
    text, ranges = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"), config=cfg,
    )
    assert "select i,9,9,11,12\n" in text and "select i,2,3\n" in text
    assert text.index("select i,9,9,11,12") < text.rindex("\nselect i\n") < text.index("wobs ")  # full selection restored before wobs
    assert [(s.first, s.last) for s in ranges] == [(2, 3), (6, 7), (9, 12)]


def test_run_records_per_if_selection_in_metadata(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}},
        stage1={"final_if_selfcal": {"enabled": True, "if_count": 2, "channels_per_if": 4, "channels": "2:1-2,4"}},
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert all(r.ok for r in results), [r.message for r in results]
    meta = json.loads(results[0].paths.stage_metadata.read_text())["final_if_selfcal"]
    assert meta["enabled"] is True and meta["n_ifs"] == 2 and meta["channels_spec"] == "2:1-2,4"
    assert meta["selected_ranges_one_based"] == [
        {"if_number": 1, "first_channel": 1, "last_channel": 4, "channels": [[1, 4]], "n_channels": 4},
        {"if_number": 2, "first_channel": 5, "last_channel": 8, "channels": [[5, 6], [8, 8]], "n_channels": 3},
    ]
    assert "select i,5,6,8,8" in results[0].commands


def test_cli_and_run_page_pass_the_spec(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["stage1", str(tmp_path), "--final-if-selfcal", "--final-if-channels", "1:2-3", "--dry-run"])
    assert "No such option" not in result.output
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_final_if_selfcal=True, stage1_final_if_channels="1:2-3; 2:1-4"))
    assert steps[0].argv[-3:] == ["--final-if-selfcal", "--final-if-channels", "1:2-3; 2:1-4"]
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_final_if_channels=""))
    assert steps[0].argv[-2:] == ["--final-if-channels", ""]  # clears a configured spec for this run
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1]))
    assert "--final-if-channels" not in steps[0].argv
