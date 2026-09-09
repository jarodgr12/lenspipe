"""Smoke tests for the console's browser-free logic: argv building, TOML rendering, chains."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

nicegui = pytest.importorskip("nicegui")

from lenspipe.config import LenspipeConfig  # noqa: E402
from lenspipe.ui.commands import RunRequest, build_steps, calibrate_step, command_line  # noqa: E402
from lenspipe.ui.forms import classify  # noqa: E402
from lenspipe.ui.pages_parameters import render_toml, unified_diff  # noqa: E402
from lenspipe.ui.pages_results import product_options, resolve_target  # noqa: E402
from lenspipe.ui.state import ChainStep, Console  # noqa: E402


def test_import_app() -> None:
    import lenspipe.ui.app as app

    assert callable(app.serve)


def test_build_steps_one_job_per_stage_in_order(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[3, 1, 2], epochs=["A"], overwrite=True,
        stage2_mode="if", stage2_channels_per_if=4, stage2_shards="auto",
        stage3_error_source="difmap", stage3_product="if4_edge0", stage3_workers=3,
    )
    steps, notes = build_steps(request)
    assert [s.stage for s in steps] == ["stage1", "stage2", "stage3"]
    assert steps[0].argv == ["stage1", str(tmp_path), "--epoch", "A", "--overwrite"]
    assert steps[1].argv == [
        "stage2", str(tmp_path), "--epoch", "A", "--mode", "if", "--channels-per-if", "4",
        "--shards", "auto", "--overwrite",
    ]
    assert steps[2].argv == [
        "stage3", str(tmp_path), "--epoch", "A", "--product", "if4_edge0",
        "--error-source", "difmap", "--workers", "3", "--overwrite",
    ]
    assert any("previous job completes" in note for note in notes)
    assert command_line(steps[0].argv).startswith("lenspipe stage1 ")


def test_build_steps_dry_run_skips_stage3_and_channel_mode(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[2, 3], dry_run=True, stage2_mode="channel",
        stage2_channels="1-4", stage2_channels_per_if=64,
    )
    steps, notes = build_steps(request)
    assert [s.stage for s in steps] == ["stage2"]
    assert "--channels" in steps[0].argv and "--channels-per-if" not in steps[0].argv
    assert steps[0].argv[-1] == "--dry-run"
    assert any("stage3" in note for note in notes)


def test_calibrate_step(tmp_path: Path) -> None:
    assert calibrate_step(tmp_path, "flag,bandpass", False).argv == [
        "calibrate", str(tmp_path), "--steps", "flag,bandpass",
    ]
    assert calibrate_step(tmp_path, None, True).argv[-1] == "--list-steps"


def test_render_toml_round_trips_defaults_without_none() -> None:
    text = render_toml(LenspipeConfig())
    data = tomllib.loads(text)
    assert "cell_mas" not in data["stage1"]  # None cannot be written to TOML
    assert LenspipeConfig.model_validate(data) == LenspipeConfig()
    assert unified_diff(text, text, "lenspipe.toml") == ""
    assert "+shards" in unified_diff(text, text.replace('shards = "auto"', "shards = 4"), "lenspipe.toml")


def test_classify_field_kinds() -> None:
    fields = LenspipeConfig.model_fields["stage2"].annotation.model_fields
    assert classify(fields["mode"].annotation).name == "literal"
    assert classify(fields["shards"].annotation).name == "int_or_literal"
    assert classify(fields["cell_mas"].annotation).optional
    stage3 = LenspipeConfig.model_fields["stage3"].annotation.model_fields
    assert classify(stage3["frequency_frame_ghz"].annotation).name == "tuple"
    assert classify(stage3["frequency_ticks_ghz"].annotation).item is float
    stage1 = LenspipeConfig.model_fields["stage1"].annotation.model_fields
    assert classify(stage1["selfcal"].annotation).name == "table"


def test_results_targets(tmp_path: Path) -> None:
    summary = {
        "epochs": [{"source": "S", "epoch": "A", "stage2": [{"product": "channel"}],
                    "stage3": [{"product": "channel", "analysis_tag": "x1"}]}],
        "combined": [{"source": "S", "product": "channel", "analysis_tag": None}],
    }
    assert product_options(summary, "S.A", 2) == {"channel": "channel"}
    assert list(product_options(summary, "S.A", 3)) == ["channel/x1"]
    assert list(product_options(summary, "combined:S", 3)) == ["channel"]
    assert resolve_target(tmp_path, "S.A", 2, "channel").prefix == "S.A.channel."
    assert resolve_target(tmp_path, "S.A", 3, "channel/x1").directory == tmp_path / "stage3/S.A/channel/x1"
    assert resolve_target(tmp_path, "combined:S", 3, "channel").directory == tmp_path / "stage3/combined/S/channel"


def test_chain_releases_next_stage_only_after_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lenspipe.ui.state.RECENT_PATH", tmp_path / "recent.json")
    console = Console()
    console.open_root(tmp_path)
    ok = ChainStep(["--version"], "first", "stage1")
    second = ChainStep(["--version"], "second", "stage2")
    first = console.submit_chain([ok, second])
    assert first is not None and console.pending_chain_titles() == ["second"]
    console.manager.wait(first.id, timeout_s=60)
    console.tick()
    assert console.pending_chain_titles() == []
    records = console.manager.list()
    assert [r.title for r in records] == ["second", "first"]
    console.manager.wait(records[0].id, timeout_s=60)

    bad = ChainStep(["no-such-command"], "bad", "stage1")
    skipped = ChainStep(["--version"], "skipped", "stage2")
    failed = console.submit_chain([bad, skipped])
    console.manager.wait(failed.id, timeout_s=60)
    console.tick()
    assert console.pending_chain_titles() == []
    assert not any(r.title == "skipped" for r in console.manager.list())
    assert console.notices and "skipped" in console.notices[-1]
    assert (tmp_path / "recent.json").is_file()
