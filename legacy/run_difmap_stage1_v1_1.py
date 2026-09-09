#!/usr/bin/env python3
"""
===============================================================================
DifMAP Spectral Pipeline — Stage 1
Calibration, self-calibration, imaging, and hierarchy-aware model fitting
Version: 1.1.0
===============================================================================

Purpose
-------
Process one or more raw UV-FITS epochs within a standard lens-project directory.
The script validates the labelled master model, runs the established DifMAP
self-calibration sequence, restores GROUP/COMPONENT labels to the fitted model,
and writes reproducibility metadata for Stage 2.

Required project layout
-----------------------
<project>/
    inputs/
        <source>.gmod
        <source>.<epoch>.uvfits
        <source>.<epoch>.uvfits
        ...

The master model must use the hierarchy syntax:

    ! GROUP A1
    ! COMPONENT A1a
    <DifMAP model row>

Outputs
-------
For each epoch, Stage 1 creates:

<project>/stage1/<source>.<epoch>/
    <source>.<epoch>.cal.uvf
    <source>.<epoch>.gmod
    <source>.<epoch>.cln.fits
    <source>.<epoch>.resid.fits
    <source>.<epoch>.stage1.difmap.log
    <source>.<epoch>.model.json
    <source>.<epoch>.stage1.metadata.json
    <source>.<epoch>.stage1.manifest.json

Typical usage
-------------
Run all epochs in a project:

    python run_difmap_stage1.py /data/MG0414

Run only epoch A:

    python run_difmap_stage1.py /data/MG0414 --epoch A

Run several selected epochs:

    python run_difmap_stage1.py /data/MG0414 --epoch A --epoch C

Validate inputs and print DifMAP commands without running:

    python run_difmap_stage1.py /data/MG0414 --dry-run

Replace existing products:

    python run_difmap_stage1.py /data/MG0414 --overwrite

Show examples:

    python run_difmap_stage1.py --examples

Pipeline
--------
Raw UV-FITS + labelled master model
    -> phase and amplitude self-calibration
    -> model fitting
    -> residual and restored images
    -> calibrated UV data
    -> labelled fitted model and hierarchy metadata

Notes
-----
* Input filenames must be <source>.<epoch>.uvfits.
* The master model must be inputs/<source>.gmod.
* Source names may contain periods; parsing is performed from the right.
* Stage 1 products are isolated by epoch and are never written into inputs/.
===============================================================================
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PIPELINE_VERSION = "1.1.0"
STAGE_NUMBER = 1

EXAMPLES = """Examples
--------
Run every epoch:
  python run_difmap_stage1.py /data/MG0414

Run one epoch:
  python run_difmap_stage1.py /data/MG0414 --epoch A

Run selected epochs:
  python run_difmap_stage1.py /data/MG0414 --epoch A --epoch C

Validate and display commands:
  python run_difmap_stage1.py /data/MG0414 --dry-run

Overwrite existing products:
  python run_difmap_stage1.py /data/MG0414 --overwrite
"""


@dataclass(frozen=True)
class Dataset:
    project_root: Path
    uvfits: Path
    source: str
    epoch: str
    starting_model: Path
    output_directory: Path
    calibrated_uvfits: Path
    final_model: Path
    clean_image: Path
    residual_image: Path
    log_file: Path
    model_metadata: Path
    stage_metadata: Path
    manifest: Path


@dataclass(frozen=True)
class ModelComponent:
    group: str
    name: str
    row_index: int


@dataclass(frozen=True)
class ModelHierarchy:
    components: tuple[ModelComponent, ...]

    @property
    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for component in self.components:
            grouped.setdefault(component.group, []).append(component.name)
        return grouped


class ModelFormatError(ValueError):
    """Raised when a master model does not follow the labelled format."""


def is_model_data_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("!")


def parse_master_model(path: Path) -> ModelHierarchy:
    """Parse and validate GROUP/COMPONENT labels in a master model."""
    lines = path.read_text(encoding="utf-8").splitlines()
    group_pattern = re.compile(r"^!\s*GROUP\s+(\S+)\s*$", re.IGNORECASE)
    component_pattern = re.compile(r"^!\s*COMPONENT\s+(\S+)\s*$", re.IGNORECASE)

    current_group: str | None = None
    pending_component: str | None = None
    components: list[ModelComponent] = []
    group_names: set[str] = set()
    component_names: set[str] = set()
    row_index = 0

    for line_number, line in enumerate(lines, start=1):
        group_match = group_pattern.match(line.strip())
        if group_match:
            if pending_component is not None:
                raise ModelFormatError(
                    f"Line {line_number}: COMPONENT '{pending_component}' has no model row."
                )
            current_group = group_match.group(1)
            if current_group in group_names:
                raise ModelFormatError(
                    f"Line {line_number}: duplicate GROUP '{current_group}'."
                )
            group_names.add(current_group)
            continue

        component_match = component_pattern.match(line.strip())
        if component_match:
            if current_group is None:
                raise ModelFormatError(
                    f"Line {line_number}: COMPONENT appears before any GROUP."
                )
            if pending_component is not None:
                raise ModelFormatError(
                    f"Line {line_number}: COMPONENT '{pending_component}' has no model row."
                )
            pending_component = component_match.group(1)
            if pending_component in component_names:
                raise ModelFormatError(
                    f"Line {line_number}: duplicate COMPONENT '{pending_component}'."
                )
            component_names.add(pending_component)
            continue

        if is_model_data_line(line):
            if current_group is None or pending_component is None:
                raise ModelFormatError(
                    f"Line {line_number}: every model row must follow GROUP and COMPONENT labels."
                )
            components.append(
                ModelComponent(current_group, pending_component, row_index)
            )
            row_index += 1
            pending_component = None

    if pending_component is not None:
        raise ModelFormatError(f"COMPONENT '{pending_component}' has no model row.")
    if not components:
        raise ModelFormatError("No labelled model components were found.")

    empty_groups = group_names - {component.group for component in components}
    if empty_groups:
        raise ModelFormatError(
            "GROUP(s) without components: " + ", ".join(sorted(empty_groups))
        )

    return ModelHierarchy(tuple(components))


def label_fitted_model(path: Path, hierarchy: ModelHierarchy) -> None:
    """Reinsert GROUP/COMPONENT labels into a DifMAP-written model."""
    lines = path.read_text(encoding="utf-8").splitlines()
    data_indices = [i for i, line in enumerate(lines) if is_model_data_line(line)]
    if len(data_indices) != len(hierarchy.components):
        raise ModelFormatError(
            f"DifMAP output contains {len(data_indices)} rows, but the master "
            f"model defines {len(hierarchy.components)} components."
        )

    data_index_set = set(data_indices)
    output_lines: list[str] = []
    component_number = 0
    previous_group: str | None = None
    for index, line in enumerate(lines):
        if index not in data_index_set:
            if re.match(r"^!\s*(GROUP|COMPONENT)\b", line.strip(), re.I):
                continue
            output_lines.append(line)
            continue

        component = hierarchy.components[component_number]
        if component.group != previous_group:
            output_lines.append(f"! GROUP {component.group}")
            previous_group = component.group
        output_lines.append(f"! COMPONENT {component.name}")
        output_lines.append(line)
        component_number += 1

    path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


def parse_dataset_name(path: Path) -> tuple[str, str]:
    suffix = ".uvfits"
    if not path.name.endswith(suffix):
        raise ValueError(f"Filename does not end in '{suffix}': {path.name}")
    stem = path.name[: -len(suffix)]
    try:
        source, epoch = stem.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError(
            f"Expected '<source>.<epoch>.uvfits', received '{path.name}'"
        ) from exc
    if not source or not epoch:
        raise ValueError(
            f"Expected '<source>.<epoch>.uvfits', received '{path.name}'"
        )
    return source, epoch


def make_dataset(project_root: Path, uvfits: Path) -> Dataset:
    source, epoch = parse_dataset_name(uvfits)
    prefix = f"{source}.{epoch}"
    output_directory = project_root / "stage1" / prefix
    return Dataset(
        project_root=project_root,
        uvfits=uvfits.resolve(),
        source=source,
        epoch=epoch,
        starting_model=(project_root / "inputs" / f"{source}.gmod").resolve(),
        output_directory=output_directory.resolve(),
        calibrated_uvfits=(output_directory / f"{prefix}.cal.uvf").resolve(),
        final_model=(output_directory / f"{prefix}.gmod").resolve(),
        clean_image=(output_directory / f"{prefix}.cln.fits").resolve(),
        residual_image=(output_directory / f"{prefix}.resid.fits").resolve(),
        log_file=(output_directory / f"{prefix}.stage1.difmap.log").resolve(),
        model_metadata=(output_directory / f"{prefix}.model.json").resolve(),
        stage_metadata=(output_directory / f"{prefix}.stage1.metadata.json").resolve(),
        manifest=(output_directory / f"{prefix}.stage1.manifest.json").resolve(),
    )


def make_if_selfcal_commands(
    if_count: int,
    channels_per_if: int,
    edge_channels: int,
) -> tuple[str, list[tuple[int, int]]]:
    """
    Build a sequential IF-by-IF self-calibration block.

    Each IF is selected independently using only its interior channels. The
    same final full-band spectral model remains in memory throughout. No
    per-IF modelfit is performed.

    For 64 channels per IF and four excluded channels on each side:
        IF 1  -> channels 5-60
        IF 2  -> channels 69-124
        ...
    """
    if if_count < 1:
        raise ValueError("--if-count must be at least 1.")
    if channels_per_if < 1:
        raise ValueError("--channels-per-if must be at least 1.")
    if edge_channels < 0:
        raise ValueError("--edge-channels cannot be negative.")
    if 2 * edge_channels >= channels_per_if:
        raise ValueError(
            "Twice --edge-channels must be smaller than --channels-per-if."
        )

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


def difmap_commands(
    dataset: Dataset,
    final_if_selfcal: bool = False,
    if_count: int = 48,
    channels_per_if: int = 64,
    edge_channels: int = 0,
) -> tuple[str, list[tuple[int, int]]]:
    selected_ranges: list[tuple[int, int]] = []
    if final_if_selfcal:
        if_selfcal_block, selected_ranges = make_if_selfcal_commands(
            if_count=if_count,
            channels_per_if=channels_per_if,
            edge_channels=edge_channels,
        )
        final_if_section = f"""
! Optional final IF-by-IF calibration against the completed spectral model.
! No modelfit is performed in this block.
{if_selfcal_block}

! Restore the full Stokes-I selection before imaging and writing products.
select i
"""
    else:
        final_if_section = ""

    commands = f"""observe {dataset.uvfits}
select i
unflag *

mapsize 1024
uvw 0,-1,false

rmodel {dataset.starting_model}

selfcal false,false,3600
modelfit 50

selfcal false,false,1
modelfit 50

selfcal false,false,0.5
modelfit 50

selfcal true,true,3600
modelfit 50

selfcal false,false,0.5
modelfit 50

{final_if_section}
invert
print imstat(rms)
wdmap {dataset.residual_image}

restore
wmap {dataset.clean_image}

wobs {dataset.calibrated_uvfits}
wmodel {dataset.final_model}

quit
"""
    return commands, selected_ranges


def output_paths(dataset: Dataset) -> tuple[Path, ...]:
    return (
        dataset.calibrated_uvfits,
        dataset.final_model,
        dataset.clean_image,
        dataset.residual_image,
        dataset.log_file,
        dataset.model_metadata,
        dataset.stage_metadata,
        dataset.manifest,
    )


def extract_rms(log_text: str) -> float | None:
    numeric_pattern = re.compile(
        r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$"
    )
    for line in reversed([line.strip() for line in log_text.splitlines()]):
        if numeric_pattern.fullmatch(line):
            return float(line)
    return None


def write_metadata(
    dataset: Dataset,
    hierarchy: ModelHierarchy,
    rms: float | None,
    selected_ranges: list[tuple[int, int]],
    channels_per_if: int,
    edge_channels: int,
) -> None:
    created = datetime.now(timezone.utc).isoformat()
    model_payload = {
        "schema_version": "1.0",
        "stage": STAGE_NUMBER,
        "stage1_version": PIPELINE_VERSION,
        "source": dataset.source,
        "epoch": dataset.epoch,
        "master_model": dataset.starting_model.name,
        "fitted_model": dataset.final_model.name,
        "n_groups": len(hierarchy.groups),
        "n_components": len(hierarchy.components),
        "groups": hierarchy.groups,
        "components": [
            {"name": c.name, "group": c.group, "row_index": c.row_index}
            for c in hierarchy.components
        ],
        "created_utc": created,
    }
    dataset.model_metadata.write_text(
        json.dumps(model_payload, indent=2) + "\n", encoding="utf-8"
    )

    stage_payload = {
        "schema_version": "1.0",
        "pipeline": "DifMAP Spectral Pipeline",
        "stage": STAGE_NUMBER,
        "version": PIPELINE_VERSION,
        "source": dataset.source,
        "epoch": dataset.epoch,
        "project_root": str(dataset.project_root),
        "input_uvfits": str(dataset.uvfits.relative_to(dataset.project_root)),
        "master_model": str(dataset.starting_model.relative_to(dataset.project_root)),
        "final_residual_rms_jy_per_beam": rms,
        "final_if_selfcal": {
            "enabled": bool(selected_ranges),
            "n_ifs": len(selected_ranges),
            "channels_per_if": channels_per_if,
            "edge_channels_excluded_per_side": edge_channels,
            "retained_channels_per_if": channels_per_if - 2 * edge_channels,
            "amplitude_phase_interval_minutes": 3600.0,
            "phase_only_interval_minutes": 0.5,
            "modelfit_within_if_loop": False,
            "selected_ranges_one_based": [
                {
                    "if_number": index,
                    "first_channel": first,
                    "last_channel": last,
                }
                for index, (first, last) in enumerate(selected_ranges, start=1)
            ],
        },
        "created_utc": created,
    }
    dataset.stage_metadata.write_text(
        json.dumps(stage_payload, indent=2) + "\n", encoding="utf-8"
    )

    manifest_payload = {
        "schema_version": "1.0",
        "stage": STAGE_NUMBER,
        "source": dataset.source,
        "epoch": dataset.epoch,
        "metadata": dataset.stage_metadata.name,
        "model_metadata": dataset.model_metadata.name,
        "products": [path.name for path in output_paths(dataset) if path != dataset.manifest],
    }
    dataset.manifest.write_text(
        json.dumps(manifest_payload, indent=2) + "\n", encoding="utf-8"
    )


def run_dataset(
    dataset: Dataset,
    difmap_executable: str,
    overwrite: bool,
    dry_run: bool,
    final_if_selfcal: bool,
    if_count: int,
    channels_per_if: int,
    edge_channels: int,
) -> bool:
    print(f"\nDataset:         {dataset.uvfits.name}")
    print(f"Master model:    {dataset.starting_model}")
    print(f"Output directory:{dataset.output_directory}")

    if not dataset.starting_model.is_file():
        print(f"ERROR: master model not found: {dataset.starting_model}", file=sys.stderr)
        return False

    try:
        hierarchy = parse_master_model(dataset.starting_model)
    except (OSError, ModelFormatError) as exc:
        print(f"ERROR: invalid master model: {exc}", file=sys.stderr)
        return False

    existing = [path for path in output_paths(dataset) if path.exists()]
    if existing and not overwrite:
        print("SKIPPED: one or more Stage 1 products already exist.")
        print("Use --overwrite to replace this epoch directory.")
        return False

    print(
        f"Model hierarchy: {len(hierarchy.groups)} group(s), "
        f"{len(hierarchy.components)} component(s)"
    )
    for group, components in hierarchy.groups.items():
        print(f"  {group}: {', '.join(components)}")

    try:
        commands, selected_ranges = difmap_commands(
            dataset,
            final_if_selfcal=final_if_selfcal,
            if_count=if_count,
            channels_per_if=channels_per_if,
            edge_channels=edge_channels,
        )
    except ValueError as exc:
        print(f"ERROR: invalid IF self-calibration setup: {exc}", file=sys.stderr)
        return False

    if final_if_selfcal:
        print(
            f"Final IF self-calibration: enabled; {if_count} IFs; "
            f"{channels_per_if - 2 * edge_channels} retained channels per IF"
        )
        print(
            f"  IF 1:  channels {selected_ranges[0][0]}-{selected_ranges[0][1]}"
        )
        print(
            f"  IF {if_count}: channels "
            f"{selected_ranges[-1][0]}-{selected_ranges[-1][1]}"
        )
    else:
        print("Final IF self-calibration: disabled")

    if dry_run:
        print("\n--- DifMAP commands ---")
        print(commands, end="")
        print("--- End commands ---")
        return True

    dataset.output_directory.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for path in output_paths(dataset):
            if path.exists():
                path.unlink()

    try:
        process = subprocess.run(
            [difmap_executable], input=commands, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"ERROR: could not run DifMAP: {exc}", file=sys.stderr)
        return False

    dataset.log_file.write_text(process.stdout, encoding="utf-8")
    if process.returncode != 0:
        print(f"FAILED: DifMAP returned status {process.returncode}.", file=sys.stderr)
        print(f"See log: {dataset.log_file}")
        return False

    required = (
        dataset.calibrated_uvfits, dataset.final_model,
        dataset.clean_image, dataset.residual_image,
    )
    missing = [path for path in required if not path.exists()]
    if missing:
        print("FAILED: DifMAP exited normally, but outputs are missing:", file=sys.stderr)
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        return False

    try:
        label_fitted_model(dataset.final_model, hierarchy)
        write_metadata(
            dataset,
            hierarchy,
            extract_rms(process.stdout),
            selected_ranges=selected_ranges,
            channels_per_if=channels_per_if,
            edge_channels=edge_channels,
        )
    except (OSError, ModelFormatError, ValueError) as exc:
        print(f"FAILED: post-processing error: {exc}", file=sys.stderr)
        return False

    print("Completed successfully.")
    for label, path in (
        ("Calibrated data", dataset.calibrated_uvfits),
        ("Final model", dataset.final_model),
        ("CLEAN image", dataset.clean_image),
        ("Residual image", dataset.residual_image),
        ("Model metadata", dataset.model_metadata),
        ("Stage metadata", dataset.stage_metadata),
        ("Manifest", dataset.manifest),
        ("Log", dataset.log_file),
    ):
        print(f"{label + ':':18s} {path}")
    return True


def discover_uvfits(inputs_directory: Path, pattern: str, epochs: set[str]) -> list[Path]:
    files: list[Path] = []
    for path in sorted(inputs_directory.glob(pattern)):
        if not path.is_file():
            continue
        try:
            _, epoch = parse_dataset_name(path)
        except ValueError:
            continue
        if epochs and epoch not in epochs:
            continue
        files.append(path)
    return files


def describe_configuration(args: argparse.Namespace) -> None:
    print(f"Stage 1 Pipeline (version {PIPELINE_VERSION})")
    print()
    print("Full-band calibration")
    print("---------------------")
    print("Phase self-cal:        3600 min")
    print("Phase self-cal:        1 min")
    print("Phase self-cal:        0.5 min")
    print("Amplitude+phase:       3600 min")
    print("Final phase self-cal:  0.5 min")
    print("Model fitting:         50 iterations after each full-band step")
    print()
    print("Final IF calibration")
    print("--------------------")
    print(f"Enabled:               {'yes' if args.final_if_selfcal else 'no'}")
    if args.final_if_selfcal:
        print(f"Number of IFs:         {args.if_count}")
        print(f"Channels per IF:       {args.channels_per_if}")
        print(f"Excluded per edge:     {args.edge_channels}")
        print(f"Retained per IF:       {args.channels_per_if - 2 * args.edge_channels}")
        print("Amplitude+phase:       3600 min")
        print("Phase-only:            0.5 min")
        print("Per-IF model fitting:  no")
    print()
    print("Outputs")
    print("-------")
    print("Calibrated UVFITS, final model, residual map, CLEAN map, metadata")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run hierarchy-aware Stage 1 for a standard project directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use --examples for complete command examples.",
    )
    parser.add_argument(
        "project_root", nargs="?", type=Path, default=Path("."),
        help="Project root containing inputs/. Default: current directory.",
    )
    parser.add_argument(
        "--epoch", action="append", default=[],
        help="Process only this epoch. Repeat to select multiple epochs.",
    )
    parser.add_argument(
        "--pattern", default="*.uvfits",
        help="Input pattern inside inputs/. Default: '*.uvfits'.",
    )
    parser.add_argument(
        "--final-if-selfcal",
        action="store_true",
        help=(
            "Enable the optional final IF-by-IF amplitude+phase and phase-only "
            "self-calibration loop. Default: disabled."
        ),
    )
    parser.add_argument(
        "--if-count",
        type=int,
        default=48,
        help="Number of IFs in the final sequential self-calibration loop. Default: 48.",
    )
    parser.add_argument(
        "--channels-per-if",
        type=int,
        default=64,
        help="Number of channels per IF. Default: 64.",
    )
    parser.add_argument(
        "--edge-channels",
        type=int,
        default=0,
        help=(
            "Channels excluded from each side of every IF during the final "
            "IF-by-IF self-calibration. Default: 0."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PIPELINE_VERSION}",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print the configured Stage 1 workflow and exit.",
    )
    parser.add_argument("--difmap", default="difmap", help="DifMAP executable.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing products.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print commands only.")
    parser.add_argument("--examples", action="store_true", help="Print usage examples and exit.")
    args = parser.parse_args()

    if args.examples:
        print(EXAMPLES)
        return 0
    if args.describe:
        describe_configuration(args)
        return 0

    project_root = args.project_root.expanduser().resolve()
    inputs_directory = project_root / "inputs"
    if not inputs_directory.is_dir():
        print(f"ERROR: inputs directory not found: {inputs_directory}", file=sys.stderr)
        return 1

    if not args.dry_run and shutil.which(args.difmap) is None:
        explicit = Path(args.difmap).expanduser()
        if not explicit.is_file():
            print(f"ERROR: DifMAP executable not found: {args.difmap}", file=sys.stderr)
            return 1

    epochs = {str(epoch) for epoch in args.epoch}
    uvfits_files = discover_uvfits(inputs_directory, args.pattern, epochs)
    if not uvfits_files:
        selection = f" for epoch(s) {sorted(epochs)}" if epochs else ""
        print(f"ERROR: no matching UV-FITS files found{selection} in {inputs_directory}", file=sys.stderr)
        return 1

    print(f"Project: {project_root}")
    print(f"Found {len(uvfits_files)} UV-FITS dataset(s).")

    successful = 0
    failed_or_skipped = 0
    for uvfits in uvfits_files:
        try:
            dataset = make_dataset(project_root, uvfits)
        except ValueError as exc:
            print(f"SKIPPED: {uvfits.name}: {exc}", file=sys.stderr)
            failed_or_skipped += 1
            continue
        if run_dataset(
            dataset,
            args.difmap,
            args.overwrite,
            args.dry_run,
            final_if_selfcal=args.final_if_selfcal,
            if_count=args.if_count,
            channels_per_if=args.channels_per_if,
            edge_channels=args.edge_channels,
        ):
            successful += 1
        else:
            failed_or_skipped += 1

    print("\nSummary")
    print("-------")
    print(f"Successful:        {successful}")
    print(f"Failed or skipped: {failed_or_skipped}")
    return 0 if failed_or_skipped == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
