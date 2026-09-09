"""DifMAP command scripts reproduce the legacy scripts for the same inputs."""

from __future__ import annotations

from pathlib import Path

import pytest

from lenspipe.config import LenspipeConfig, Stage1Config, Stage2Config
from lenspipe.difmap.scripts import fit_model_path, stage1_commands, stage2_commands
from lenspipe.stage1 import make_paths as make_stage1_paths
from lenspipe.stage2 import make_fit_ranges, plan_shards, product_tag
from lenspipe.stage2 import make_paths as make_stage2_paths


def _legacy_stage1_dataset(legacy_stage1, root: Path, uvfits: Path):
    return legacy_stage1.make_dataset(root, uvfits)


@pytest.mark.parametrize("final_if", [False, True])
def test_stage1_commands_match_legacy(project: Path, legacy_stage1, final_if: bool) -> None:
    uvfits = project / "inputs" / "MG0414.A.uvfits"
    legacy_dataset = _legacy_stage1_dataset(legacy_stage1, project, uvfits)
    legacy_text, legacy_ranges = legacy_stage1.difmap_commands(
        legacy_dataset, final_if_selfcal=final_if, if_count=2, channels_per_if=4, edge_channels=1
    )

    config = Stage1Config()
    config.final_if_selfcal.enabled = final_if
    config.final_if_selfcal.if_count = 2
    config.final_if_selfcal.channels_per_if = 4
    config.final_if_selfcal.edge_channels = 1
    paths = make_stage1_paths(project, uvfits)
    ours, ranges = stage1_commands(
        uvfits=paths.uvfits,
        starting_model=paths.starting_model,
        residual_image=paths.residual_image,
        clean_image=paths.clean_image,
        calibrated_uvfits=paths.calibrated_uvfits,
        final_model=paths.final_model,
        config=config,
    )

    # The only intended difference: the residual RMS is now printed with a tag.
    expected = legacy_text.replace("print imstat(rms)", 'print "STAGE1_RMS", imstat(rms)')
    assert ours == expected
    assert ranges == legacy_ranges


def test_stage1_custom_schedule_renders_like_legacy_numbers() -> None:
    config = Stage1Config.model_validate(
        {"selfcal": [[False, False, 10, 5], [True, False, 0.25, 7]], "cell_mas": 25}
    )
    text, _ = stage1_commands(
        uvfits=Path("/d/x.uvfits"),
        starting_model=Path("/d/x.gmod"),
        residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"),
        calibrated_uvfits=Path("/o/x.cal.uvf"),
        final_model=Path("/o/x.gmod"),
        config=config,
    )
    assert "mapsize 1024,25\n" in text
    assert "selfcal false,false,10\nmodelfit 5\n\nselfcal true,false,0.25\nmodelfit 7\n" in text


@pytest.mark.parametrize(
    "mode, channels_per_if, edge, spec",
    [("channel", 64, 0, None), ("channel", 64, 0, "1-3,7"), ("if", 4, 0, None), ("if", 4, 1, None)],
)
def test_stage2_commands_match_legacy(
    project: Path, legacy_stage2, mode: str, channels_per_if: int, edge: int, spec: str | None
) -> None:
    total_channels = 8
    cal = project / "stage1" / "MG0414.A" / "MG0414.A.cal.uvf"
    config = Stage2Config(
        mode=mode, channels_per_if=channels_per_if, exclude_edge_channels=edge, channels=spec
    )
    tag = product_tag(config)
    assert tag == legacy_stage2.product_tag(mode, channels_per_if, edge, spec)

    fit_ranges = make_fit_ranges(total_channels, mode, channels_per_if, spec, edge)
    legacy_ranges = legacy_stage2.make_fit_ranges(total_channels, mode, channels_per_if, spec, edge)
    assert fit_ranges == legacy_ranges

    paths = make_stage2_paths(project, cal, tag)
    legacy_dataset = legacy_stage2.make_dataset(project, cal, tag)
    work = project / "work"
    ours = stage2_commands(
        calibrated_uvfits=paths.calibrated_uvfits,
        stage2_model=paths.stage2_model,
        fit_ranges=fit_ranges,
        work_directory=work,
        mode=mode,
        config=config,
    )
    theirs = legacy_stage2.build_difmap_commands(legacy_dataset, fit_ranges, work, mode)
    assert ours == theirs
    assert fit_model_path(work, mode, 3) == legacy_stage2.fit_model_path(work, mode, 3)


def test_stage2_paths_match_legacy(project: Path, legacy_stage2) -> None:
    cal = project / "stage1" / "MG0414.A" / "MG0414.A.cal.uvf"
    ours = make_stage2_paths(project, cal, "channel")
    theirs = legacy_stage2.make_dataset(project, cal, "channel")
    for name in (
        "stage2_model", "spectrum_csv", "plot_file", "grouped_plot_file", "ratio_plot_file",
        "log_file", "metadata_file", "manifest_file", "stage1_model", "stage1_metadata",
    ):
        assert getattr(ours, name) == getattr(theirs, name), name


def test_plan_shards_is_contiguous_and_complete() -> None:
    ranges = [(i, i, i) for i in range(1, 11)]
    blocks = plan_shards(ranges, 3)
    assert [len(b) for b in blocks] == [4, 3, 3]
    assert [r for block in blocks for r in block] == ranges
    assert plan_shards(ranges, 50) == [[r] for r in ranges]
    assert plan_shards(ranges, 1) == [ranges]


def test_make_fit_ranges_rejects_partial_if() -> None:
    with pytest.raises(ValueError, match="Final IF contains"):
        make_fit_ranges(10, "if", 4, None, 0)


def test_config_roundtrip_defaults() -> None:
    import tomllib

    from lenspipe.config import default_toml

    parsed = LenspipeConfig.model_validate(tomllib.loads(default_toml()))
    assert parsed == LenspipeConfig()
