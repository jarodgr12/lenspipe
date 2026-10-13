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
    "if_channel_ranges",
    "if_selfcal_block",
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
    return f"mapsize {pixels},{_num(cell_mas)}"


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

    ranges = if_channel_ranges(if_count, channels_per_if, edge_channels)
    blocks: list[str] = []
    for if_number, (first_channel, last_channel) in enumerate(ranges, start=1):
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


def if_channel_ranges(if_count: int, channels_per_if: int, edge_channels: int) -> list[tuple[int, int]]:
    """1-based inclusive DifMAP channel range of each IF's inner channels."""
    return [
        ((n - 1) * channels_per_if + edge_channels + 1, n * channels_per_if - edge_channels)
        for n in range(1, if_count + 1)
    ]


def per_if_schedule_block(step_lines: list[str], starting_model: Path, config: Stage1Config) -> str:
    """The selfcal schedule repeated for every IF, then one full-band fit of the final model.

    Each IF starts from the starting model again, so the IFs are independent of
    one another and of their order. DifMAP applies the corrections solved on an
    IF's inner channels to all channels of that IF. After the loop the full band
    is selected and the model refitted once, so ``wmodel`` writes a model fitted
    to all of the per-IF calibrated data rather than the last IF's model.
    """
    layout = config.final_if_selfcal
    lines: list[str] = []
    for if_number, (first, last) in enumerate(
        if_channel_ranges(layout.if_count, layout.channels_per_if, layout.edge_channels), start=1
    ):
        lines.append(f"! IF {if_number}: channels {first}-{last}")
        lines.append(f"select i,{first},{last}")
        lines.append(f"rmodel {starting_model}")
        lines.extend(step_lines)
    lines.append("! Full band: fit the final model to the per-IF calibrated data.")
    lines.append("select i")
    lines.append(f"rmodel {starting_model}")
    lines.append(f"modelfit {config.selfcal[-1].modelfit_iterations}")
    return "\n".join(lines).rstrip("\n")


def stage1_commands(
    *,
    uvfits: Path,
    starting_model: Path,
    residual_image: Path,
    clean_image: Path,
    calibrated_uvfits: Path,
    final_model: Path,
    config: Stage1Config,
) -> tuple[str, list[tuple[int, int]]]:
    """Return the Stage 1 command script and the IF ranges used by the final loop."""
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

    step_lines: list[str] = []
    for step in config.selfcal:
        step_lines.extend(step.difmap_commands())
        step_lines.append("")
    if config.selfcal_per_if:
        selfcal_block = per_if_schedule_block(step_lines, starting_model, config)
    else:
        selfcal_block = "\n".join(step_lines).rstrip("\n")

    unflag_line = "unflag *\n" if config.unflag else ""

    commands = f"""observe {uvfits}
select i
{unflag_line}
{mapsize_command(config.map_pixels, config.cell_mas)}
{config.weighting}

rmodel {starting_model}

{selfcal_block}

{final_if_section}
invert
print "{STAGE1_RMS_MARKER}", imstat(rms)
wdmap {residual_image}

restore
wmap {clean_image}

wobs {calibrated_uvfits}
wmodel {final_model}

quit
"""
    return commands, selected_ranges


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
