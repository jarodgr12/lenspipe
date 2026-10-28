"""Stage 3 channel exclusions by spectral window, and the Stage 3 dry run."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.stage3.analysis import _analysis_tag_from_settings
from lenspipe.stage3.exclusions import (
    SpwLayout,
    describe_fit_indices,
    parse_epoch_exclusion_specs,
    parse_exclusion_spec,
    resolve_exclusions,
    spw_layout_from_metadata,
)
from lenspipe.ui.commands import RunRequest, build_steps
from lenspipe.ui.pages_run import _epoch_exclusions

CHANNEL_64 = SpwLayout(channels_per_spw=64, n_spw=48, mode="channel", source="test")
IF_64 = SpwLayout(channels_per_spw=64, n_spw=48, mode="if", source="test")


def test_parse_plain_spw_and_mixed() -> None:
    plain = parse_exclusion_spec("1-4,61-64")
    assert plain.fit_indices == (1, 2, 3, 4, 61, 62, 63, 64) and plain.spw_items == ()
    whole = parse_exclusion_spec("spw7")
    assert whole.fit_indices == () and whole.spw_items[0].spw == 7 and whole.spw_items[0].channels is None
    assert parse_exclusion_spec("SPW7:*").spw_items[0].channels is None
    channels = parse_exclusion_spec("spw3:0-3,60-63")
    assert channels.spw_items[0].channels == (0, 1, 2, 3, 60, 61, 62, 63)
    mixed = parse_exclusion_spec("spw3:0-3,60-63; spw7; 1-4")
    assert mixed.fit_indices == (1, 2, 3, 4) and [i.spw for i in mixed.spw_items] == [3, 7]
    assert mixed.describe() == ["spw 3 ch 0-3,60-63", "spw 7 (whole window)", "fit index 1-4"]
    assert parse_exclusion_spec(None).empty and parse_exclusion_spec("  ").empty


def test_parse_per_epoch() -> None:
    specs = parse_epoch_exclusion_specs(["E:spw14:0-63; spw15", "F:1-4", "E:spw2"])
    assert sorted(specs) == ["E", "F"]
    assert [i.spw for i in specs["E"].spw_items] == [14, 15, 2]
    assert specs["F"].fit_indices == (1, 2, 3, 4)
    with pytest.raises(ValueError, match="EPOCH:SPEC"):
        parse_epoch_exclusion_specs(["897-960"])


@pytest.mark.parametrize(
    "spec, message",
    [("spwx:1", "spectral-window item"), ("spw3:5-2", "Invalid range"), ("0", "Invalid index"), ("spw3:1,x", "Invalid index")],
)
def test_parse_errors(spec: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_exclusion_spec(spec)


def test_resolve_channel_and_if_products() -> None:
    spec = parse_exclusion_spec("spw3:0-3,60-63; spw7; 1-4")
    resolved = resolve_exclusions(spec, CHANNEL_64)
    assert resolved[:4] == [1, 2, 3, 4]
    assert [193, 194, 195, 196, 253, 254, 255, 256] == [i for i in resolved if 193 <= i <= 256]
    assert list(range(449, 513)) == [i for i in resolved if 449 <= i <= 512]  # spw7 = channels 449-512
    assert describe_fit_indices(resolved, CHANNEL_64) == ["spw 0 ch 0-3", "spw 3 ch 0-3,60-63", "spw 7 (whole window)"]
    assert resolve_exclusions(parse_exclusion_spec("spw7; spw0"), IF_64) == [1, 8]
    assert describe_fit_indices([8], IF_64) == ["spw 7 (whole window, fit index 8)"]
    with pytest.raises(ValueError, match="IF product"):
        resolve_exclusions(parse_exclusion_spec("spw3:0-3"), IF_64)
    with pytest.raises(ValueError, match="outside 0-47"):
        resolve_exclusions(parse_exclusion_spec("spw48"), CHANNEL_64)
    with pytest.raises(ValueError, match="outside 0-63"):
        resolve_exclusions(parse_exclusion_spec("spw3:64"), CHANNEL_64)
    with pytest.raises(ValueError, match="need the Stage 2 metadata"):
        resolve_exclusions(parse_exclusion_spec("spw3"), None)
    assert resolve_exclusions(parse_exclusion_spec("5-6"), None) == [5, 6]  # plain indices need no layout


def test_layout_from_metadata() -> None:
    s2 = {"mode": "channel", "spectral_windows": {"n_spw": 2, "channels_per_spw": 4}}
    assert spw_layout_from_metadata(s2) == SpwLayout(4, 2, "channel", "stage2 metadata")
    assert spw_layout_from_metadata({"mode": "if", "channels_per_if": 64, "n_fits": 48}) == SpwLayout(64, 48, "if", "stage2 if mode")
    assert spw_layout_from_metadata({"mode": "channel"}, {"per_if": {"channels_per_if": 32, "if_count": 3}}).channels_per_spw == 32
    assumed = spw_layout_from_metadata({"mode": "channel", "total_channels": 3072})
    assert assumed.source == "assumed" and assumed.n_spw == 48


def test_analysis_tag_is_stable_for_plain_indices_and_names_spw_items() -> None:
    assert _analysis_tag_from_settings([1, 2, 3, 4], {}, "rms") == "exclude_1-4"  # unchanged from before
    assert _analysis_tag_from_settings([], {}, "rms", global_spw_text=["spw3:0-3,60-63", "spw7"]) == "exclude_spw3_0-3_60-63_spw7"
    assert _analysis_tag_from_settings([], {"E": []}, "rms", epoch_spw_text={"E": ["spw14:0-63"]}) == "exclude_E_spw14_0-63"
    assert _analysis_tag_from_settings([], {}, "rms") is None


def test_run_page_epoch_entries_keep_spaces_inside_a_spec() -> None:
    assert _epoch_exclusions("E:spw14:0-63; spw15 F:1-4") == ["E:spw14:0-63; spw15", "F:1-4"]
    assert _epoch_exclusions("E:897-960 F:1-4") == ["E:897-960", "F:1-4"]
    assert _epoch_exclusions("") == []
    steps, notes = build_steps(RunRequest(root=Path("/p"), stages=[3], stage3_exclude_epoch_channels=["E:spw14:0-63; spw15"], dry_run=True))
    assert steps[0].argv[-3:] == ["E:spw14:0-63; spw15", "--dry-run"][-3:] or "--dry-run" in steps[0].argv
    assert "--exclude-epoch-channels" in steps[0].argv and "E:spw14:0-63; spw15" in steps[0].argv
    assert not any("no --dry-run" in note for note in notes)


def _staged(project: Path, fake_difmap: Path) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage2={"mode": "channel", "shards": 1}, stage3={"figure_formats": ["png"]},
    )
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    return cfg


def test_stage3_excludes_by_spw_on_a_channel_product(project: Path, fake_difmap: Path) -> None:
    cfg = _staged(project, fake_difmap)  # synthetic data: 2 spws x 4 channels, fit indices 1-8
    quiet = Reporter(stream=io.StringIO())
    summary = run_stage3(project, cfg.with_overrides(stage3={"exclude_channels": "spw1:0-1"}), reporter=quiet, workers=1)
    assert summary.ok and summary.analysis_tag == "exclude_spw1_0-1"
    meta_path = next((project / "stage3" / "full-band" / "MG0414.A" / "channel" / "exclude_spw1_0-1").glob("*.stage3.metadata.json"))
    meta = json.loads(meta_path.read_text())
    assert meta["n_measurements"] == 6
    block = meta["channel_exclusion"]
    assert block["effective_fit_indices_for_epoch"] == [5, 6]
    assert block["effective_exclusions_for_epoch"] == ["spw 1 ch 0-1"]
    assert block["global_spw_items"] == ["spw 1 ch 0-1"] and block["global_spec"] == "spw1:0-1"
    assert block["spw_layout"]["channels_per_spw"] == 4 and block["spw_layout"]["n_spw"] == 2
    combined = next((project / "stage3" / "full-band" / "combined" / "MG0414" / "channel" / "exclude_spw1_0-1").glob("*.combined.stage3.metadata.json"))
    assert json.loads(combined.read_text())["channel_exclusion"]["effective_exclusions_by_epoch"]["B"] == ["spw 1 ch 0-1"]

    # Per-epoch spw spec on one visit only, whole window on the other, plus a plain index for all.
    cfg2 = cfg.with_overrides(stage3={"exclude_channels": "1", "exclude_epoch_channels": ["A:spw1:2-3", "B:spw0"]})
    summary = run_stage3(project, cfg2, reporter=quiet, workers=1)
    assert summary.ok
    tag = summary.analysis_tag
    a = json.loads(next((project / "stage3" / "full-band" / "MG0414.A" / "channel" / tag).glob("*.stage3.metadata.json")).read_text())
    b = json.loads(next((project / "stage3" / "full-band" / "MG0414.B" / "channel" / tag).glob("*.stage3.metadata.json")).read_text())
    assert a["channel_exclusion"]["effective_fit_indices_for_epoch"] == [1, 7, 8]
    assert b["channel_exclusion"]["effective_fit_indices_for_epoch"] == [1, 2, 3, 4]
    assert b["channel_exclusion"]["effective_exclusions_for_epoch"] == ["spw 0 (whole window)"]

    # A bad spec fails before anything is written.
    with pytest.raises(ValueError, match="outside 0-1"):
        run_stage3(project, cfg.with_overrides(stage3={"exclude_channels": "spw5"}), reporter=quiet, workers=1)


def test_stage3_dry_run_writes_nothing(project: Path, fake_difmap: Path) -> None:
    cfg = _staged(project, fake_difmap)
    stream = io.StringIO()
    summary = run_stage3(project, cfg.with_overrides(stage3={"exclude_channels": "spw1:0-1"}), reporter=Reporter(stream=stream), dry_run=True)
    assert summary.ok and summary.analyses == 2 and summary.per_visit_written_or_skipped == 0
    assert not (project / "stage3").exists()
    text = stream.getvalue()
    assert "DRY RUN" in text and "nothing written" in text
    assert "MG0414.A channel [full-band] 6 rows after exclusions" in text and "excluded: spw 1 ch 0-1" in text
    assert "combined MG0414.channel: 2 visit(s)" in text
    assert any(p.endswith("combined/MG0414/channel/exclude_spw1_0-1") for p in summary.products)
    assert len(summary.products) == 3

    result = CliRunner().invoke(app, ["stage3", str(project), "--dry-run", "--exclude-channels", "spw0"])
    assert result.exit_code == 0, result.output
    assert not (project / "stage3").exists()


def test_spw_ranges_cover_every_window_in_the_run() -> None:
    from lenspipe.stage3.exclusions import SpwLayout, parse_exclusion_spec, resolve_exclusions

    spec = parse_exclusion_spec("spw1-3:0-1; spw5-6")
    assert [(i.spw, i.spw_last, i.spws) for i in spec.spw_items] == [(1, 3, (1, 2, 3)), (5, 6, (5, 6))]
    assert [i.describe() for i in spec.spw_items] == ["spw 1-3 ch 0-1", "spw 5-6 (whole windows)"]
    channel_layout = SpwLayout(channels_per_spw=4, n_spw=8, mode="channel", source="test")
    assert resolve_exclusions(spec, channel_layout) == [5, 6, 9, 10, 13, 14, 21, 22, 23, 24, 25, 26, 27, 28]
    if_layout = SpwLayout(channels_per_spw=4, n_spw=8, mode="if", source="test")
    assert resolve_exclusions(parse_exclusion_spec("spw5-6"), if_layout) == [6, 7]
    with pytest.raises(ValueError, match="runs backwards"):
        parse_exclusion_spec("spw3-1")
    with pytest.raises(ValueError, match="outside 0-7"):
        resolve_exclusions(parse_exclusion_spec("spw6-8"), channel_layout)
