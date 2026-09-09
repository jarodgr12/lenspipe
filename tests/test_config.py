"""Configuration loading, validation and overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lenspipe.config import LenspipeConfig, load_config


def test_defaults_match_legacy_constants() -> None:
    cfg = LenspipeConfig()
    assert cfg.stage1.map_pixels == 1024 and cfg.stage1.cell_mas is None
    assert [s.solint_min for s in cfg.stage1.selfcal] == [3600, 1, 0.5, 3600, 0.5]
    assert [s.amplitude for s in cfg.stage1.selfcal] == [False, False, False, True, False]
    assert cfg.stage2.cell_mas == 25 and cfg.stage2.modelfit_iterations == 20
    assert cfg.stage2.mode == "if" and cfg.stage2.channels_per_if == 64
    assert cfg.stage3.reference_frequency_ghz == 15.0
    assert cfg.stage3.frequency_frame_ghz == (11.7, 18.3)
    assert cfg.stage3.rcusp_images == ["A1", "A2", "B"]


def test_load_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path) == LenspipeConfig()


def test_load_partial_file(tmp_path: Path) -> None:
    (tmp_path / "lenspipe.toml").write_text(
        '[stage2]\nmode = "channel"\nshards = 3\n[stage3]\nrcusp_images = []\n'
    )
    cfg = load_config(tmp_path)
    assert cfg.stage2.mode == "channel" and cfg.stage2.shards == 3
    assert cfg.stage3.rcusp_images == []
    assert cfg.stage1 == LenspipeConfig().stage1


@pytest.mark.parametrize(
    "section, payload, message",
    [
        ("stage2", {"mode": "if", "channels": "1-3"}, "only valid with mode"),
        ("stage2", {"channels_per_if": 4, "exclude_edge_channels": 2}, "Twice exclude_edge_channels"),
        ("stage2", {"shards": 0}, "shards"),
        ("stage3", {"rcusp_images": ["A", "B"]}, "exactly three"),
        ("stage3", {"frequency_frame_ghz": [18, 12]}, "low, high"),
        ("stage3", {"emcee": {"walkers": 9}}, "even"),
        ("stage1", {"final_if_selfcal": {"channels_per_if": 4, "edge_channels": 2}}, "edge_channels"),
        ("stage1", {"unknown_option": 1}, "Extra inputs"),
    ],
)
def test_validation_errors(section: str, payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        LenspipeConfig.model_validate({section: payload})


def test_with_overrides_ignores_none() -> None:
    cfg = LenspipeConfig().with_overrides(stage2={"shards": 4, "mode": None})
    assert cfg.stage2.shards == 4 and cfg.stage2.mode == "if"
    with pytest.raises(KeyError):
        LenspipeConfig().with_overrides(nope={"a": 1})
