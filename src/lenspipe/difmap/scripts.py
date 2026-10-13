"""Build DifMAP command scripts for Stage 1 and Stage 2.

The Stage 1 script reproduces the legacy ``run_difmap_stage1`` sequence exactly,
apart from the residual RMS now being printed with a ``STAGE1_RMS`` tag. The
Stage 2 script is byte-identical to the legacy one for the same fit ranges.
"""

from __future__ import annotations

from pathlib import Path

from lenspipe.config import Stage1Config, Stage2Config
from lenspipe.difmap.logparse import STAGE1_RMS_MARKER, STAGE2_RMS_MARKER

__all__ = [
    "fit_model_path",
    "if_selfcal_block",
    "imaging_commands",
    "per_if_schedule_block",
    "mapsize_command",
    "stage1_commands",
    "stage2_commands",
]


def _num(value: float) -> str:
    """Format like the legacy scripts: 3600 -> '3600', 0.5 -> '0.5'."""
    return f"{value:g}"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def mapsize_command(pixels: int, cell_mas: float | None) -> str:
    if cell_mas is None:
        return f"mapsize {pixels}"
    return f"mapsize {pixels},{cell_mas:.12g}"


def if_selfcal_block(
    if_count: int,
    channels_per_if: int,
    edge_channels: int,
) -> tuple[str, list[tuple[int, int]]]:
    """Sequential IF-by-IF self-calibration against the finished spectral model."""
    if if_count < 1:
        raise ValueError("if_count must be at least 1.")
    if channels_per_if < 1:
        raise ValueError("channels_per_if must be at least 1.")
    if edge_channels < 0:
        raise ValueError("edge_channels cannot be negative.")
    if 2 * edge_channels >= channels_per_if:
        raise ValueError("Twice edge_channels must be smaller than channels_per_if.")

    ranges: list[tuple[int, int]] = []
    blocks: list[str] = []
    for if_number in range(1, if_count + 1):
        offset = (if_number - 1) * channels_per_if
        first_channel = offset + edge_channels + 1
        last_channel = offset + channels_per_if - edge_channels
        ranges.append((first_channel, last_channel))
        blocks.extend(
            [
                f"! IF {if_number}: channels {first_channel}-{last_channel}",
                f"select i,{first_channel},{last_channel}",
                "selfcal true,true,3600",
                "selfcal false,false,0.5",
                "",
            ]
        )
    return "\n".join(blocks).rstrip(), ranges


def per_if_schedule_block(
    config: Stage1Config,
    starting_model: Path,
    fixed_index_model: Path,
    per_if_models: list[Path],
) -> str:
    """The per-IF calibration mode (legacy Stage 1 v1.2.2 ``--calibration-mode per-if``).

    For each IF: select its inner channels, load the master model with the
    spectral-index column fixed at 0, run the schedule with amplitude self-cal
    steps replaced by ``gscale``, and save that IF's model. Every IF starts from
    the same fixed-index model, so the per-IF models are independent starting
    fits, not chained ones. Afterwards the full band is selected, the original
    (free-index) master model reloaded and the final model fitted once with the
    last step's iterations, with no further self-cal.
    """
    layout = config.per_if
    if len(per_if_models) != layout.if_count:
        raise ValueError(f"expected {layout.if_count} per-IF model paths, got {len(per_if_models)}")
    lines: list[str] = []
    for if_number, (first, last) in enumerate(layout.channel_ranges(), start=1):
        lines.append(f"! IF {if_number}: channels {first}-{last}")
        lines.append(f"select i,{first},{last}")
        lines.append(f"rmodel {fixed_index_model}")
        for step in config.selfcal:
            lines.extend(step.difmap_commands(per_if=True))
        lines.append(f"wmodel {per_if_models[if_number - 1]}")
    lines.append("! Full band: fit the final model to the per-IF calibrated data (no further self-cal).")
    lines.append(f"select i,1,{layout.total_channels}")
    lines.append(f"rmodel {starting_model}")
    lines.append(f"modelfit {config.selfcal[-1].modelfit_iterations}")
    return "\n".join(lines)


def stage1_commands(
    *,
    uvfits: Path,
    starting_model: Path,
    residual_image: Path,
    clean_image: Path,
    calibrated_uvfits: Path,
    final_model: Path,
    config: Stage1Config,
    fixed_index_model: Path | None = None,
    per_if_models: list[Path] | None = None,
) -> tuple[str, list[tuple[int, int]]]:
    """Return the Stage 1 calibration script and the IF ranges used by the final-IF loop.

    The maps written here are the *native* ones (``mapsize`` without a cell, so
    DifMAP picks its own pixel size); ``imaging_commands`` makes the final maps on
    the common grid afterwards. In per-IF mode ``fixed_index_model`` and
    ``per_if_models`` are required.
    """
    selected_ranges: list[tuple[int, int]] = []
    if config.final_if_selfcal.enabled:
        block, selected_ranges = if_selfcal_block(
            if_count=config.final_if_selfcal.if_count,
            channels_per_if=config.final_if_selfcal.channels_per_if,
            edge_channels=config.final_if_selfcal.edge_channels,
        )
        final_if_section = f"""
! Optional final IF-by-IF calibration against the completed spectral model.
! No modelfit is performed in this block.
{block}

! Restore the full Stokes-I selection before imaging and writing products.
select i
"""
    else:
        final_if_section = ""

    if config.calibration_mode == "per-if":
        if fixed_index_model is None or per_if_models is None:
            raise ValueError("per-IF calibration needs fixed_index_model and per_if_models")
        schedule_block = per_if_schedule_block(config, starting_model, fixed_index_model, per_if_models)
    else:
        step_lines: list[str] = []
        for step in config.selfcal:
            step_lines.extend(step.difmap_commands())
            step_lines.append("")
        schedule_block = f"rmodel {starting_model}\n\n" + "\n".join(step_lines).rstrip("\n")

    unflag_line = "unflag *\n" if config.unflag else ""

    commands = f"""observe {uvfits}
select i
{unflag_line}
{mapsize_command(config.map_pixels, None)}
{config.weighting}

{schedule_block}

{final_if_section}
wobs {calibrated_uvfits}
wmodel {final_model}

invert
print "{STAGE1_RMS_MARKER}", imstat(rms)
wdmap {residual_image}

restore
wmap {clean_image}

quit
"""
    return commands, selected_ranges


def imaging_commands(
    *,
    calibrated_uvfits: Path,
    model: Path,
    residual_image: Path,
    clean_image: Path,
    cell_mas: float,
    config: Stage1Config,
) -> str:
    """Second imaging pass: the calibrated data and final model on a common pixel grid.

    Every epoch is imaged with the same ``mapsize`` and cell so the maps share a
    pixel grid; the restoring beams still differ per epoch and are recorded.
    """
    return f"""observe {calibrated_uvfits}
select i
{mapsize_command(config.map_pixels, cell_mas)}
{config.weighting}

rmodel {model}
invert
print "{STAGE1_RMS_MARKER}", imstat(rms)
wdmap {residual_image}

restore
wmap {clean_image}

quit
"""


def fit_model_path(work_directory: Path, mode: str, fit_index: int) -> Path:
    label = "channel" if mode == "channel" else "if"
    return work_directory / f"{label}_{fit_index:05d}.mod"


def stage2_commands(
    *,
    calibrated_uvfits: Path,
    stage2_model: Path,
    fit_ranges: list[tuple[int, int, int]],
    work_directory: Path,
    mode: str,
    config: Stage2Config,
) -> str:
    """One DifMAP command stream fitting every ``(fit_index, first, last)`` range."""
    lines = [
        f"observe {calibrated_uvfits}",
        "select i",
        # ", true" = all channels and IFs, independent of what 'select' currently covers.
        *(["unflag *, true"] if config.unflag else []),
        mapsize_command(config.map_pixels, config.cell_mas),
        config.weighting,
        "",
    ]
    for fit_index, first_channel, last_channel in fit_ranges:
        fitted_model = fit_model_path(work_directory, mode, fit_index)
        lines.extend(
            [
                f"select i,{first_channel},{last_channel}",
                f"rmodel {stage2_model}",
                f"modelfit {config.modelfit_iterations}",
                "invert",
                f'print "{STAGE2_RMS_MARKER}", {fit_index}, imstat(rms)',
                f"wmodel {fitted_model}",
                "",
            ]
        )
    lines.append("quit")
    lines.append("")
    return "\n".join(lines)
