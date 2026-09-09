#!/usr/bin/env python3
"""
===============================================================================
DifMAP Spectral Pipeline — Stage 2
Per-channel or per-IF spectral extraction and quick-look diagnostics
Version: 1.4.0
===============================================================================

Purpose
-------
Read the hierarchy-aware products from Stage 1, fit the fixed-structure model
independently by channel or by contiguous IF-sized channel blocks, and write
component spectra, grouped-image spectra, and flux ratios relative to the first
model group.

Required project layout
-----------------------
<project>/
    inputs/
        <source>.gmod
        <source>.<epoch>.uvfits
    stage1/
        <source>.<epoch>/
            <source>.<epoch>.cal.uvf
            <source>.<epoch>.gmod
            <source>.<epoch>.model.json

Outputs
-------
For each epoch, Stage 2 creates:

<project>/stage2/<source>.<epoch>/
    <source>.<epoch>.stage2.mod
    <source>.<epoch>.<product>.spectrum.csv
    <source>.<epoch>.<product>.spectrum.png
    <source>.<epoch>.<product>.grouped_spectrum.png
    <source>.<epoch>.<product>.flux_ratios.png
    <source>.<epoch>.<product>.stage2.difmap.log
    <source>.<epoch>.<product>.stage2.metadata.json
    <source>.<epoch>.<product>.stage2.manifest.json

where <product> records the extraction configuration, for example
``channel``, ``channel_1-10_15``, or ``if64_edge2``.

Optional retained fitted models are written to:

    <project>/stage2/<source>.<epoch>/models/

Typical usage
-------------
Run every available Stage 1 epoch in IF mode:

    python run_difmap_stage2.py /data/MG0414

Run epoch A only:

    python run_difmap_stage2.py /data/MG0414 --epoch A

Fit individual channels:

    python run_difmap_stage2.py /data/MG0414 --mode channel

Fit selected channels:

    python run_difmap_stage2.py /data/MG0414 --mode channel --channels 1-10,15

Retain every fitted model:

    python run_difmap_stage2.py /data/MG0414 --keep-channel-models

Replace existing Stage 2 products:

    python run_difmap_stage2.py /data/MG0414 --overwrite

Show examples:

    python run_difmap_stage2.py --examples

Notes
-----
* Stage 2 discovers calibrated data recursively under stage1/.
* The first group in Stage 1 hierarchy metadata is the flux-ratio reference.
* The established DifMAP fitting behaviour is unchanged.
* v1.3 parses DifMAP per-component Flux Stdev values and writes formal errors.
* v1.4 adds hierarchy-driven recovery from an existing DifMAP log without rerunning fits.
* Grouped-image formal errors are quadrature sums; residual RMS is retained
  independently as a channel-quality diagnostic.
===============================================================================
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ==============================================================
# User-adjustable Stage 2 parameters
# ==============================================================

# Image geometry
MAP_PIXELS = 1024
CELL_SIZE_MAS = 25       # None = let DifMAP choose automatically

# Visibility weighting
UVW_COMMAND = "uvw 0,-1,false"

# Model fitting
MODELFIT_ITERATIONS = 20

# Default spectral fitting mode
DEFAULT_MODE = "if"  # "channel" or "if"
CHANNELS_PER_IF = 64

# Output model suffix
STAGE2_MODEL_SUFFIX = "stage2.mod"

# Output format
OUTPUT_GROUPED_COMPONENTS = True  # retained for backwards compatibility; v9 always outputs both levels
STAGE2_VERSION = "1.4.0"

# Quick-look plotting
PLOT_SPECTRUM = True
PLOT_ERROR_BARS = True
PLOT_DPI = 200

# Machine-readable DifMAP log marker used for one RMS measurement per fit.
RMS_LOG_MARKER = "STAGE2_RMS"

try:
    from astropy.io import fits
    from astropy.time import Time
except ImportError as exc:
    raise SystemExit(
        "This script requires Astropy. Install it with:\n"
        "    python3 -m pip install astropy"
    ) from exc


FLOAT_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?$"
)

FINAL_VARIABLE_FIELD = re.compile(
    r"""
    (?P<value>
        [+-]?
        (?:
            (?:\d+(?:\.\d*)?)
            |
            (?:\.\d+)
        )
        (?:[EeDd][+-]?\d+)?
    )
    [vV]
    (?P<trailing>[ \t]*)
    (?P<newline>\r?\n)?
    $
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Dataset:
    calibrated_uvfits: Path
    source: str
    epoch: str
    source_model: Path
    stage1_model: Path
    stage1_metadata: Path
    stage2_model: Path
    spectrum_csv: Path
    plot_file: Path
    grouped_plot_file: Path
    ratio_plot_file: Path
    log_file: Path
    metadata_file: Path
    manifest_file: Path


def parse_calibrated_name(path: Path) -> tuple[str, str]:
    """Parse '<source>.<epoch>.cal.uvf'."""
    suffix = ".cal.uvf"
    if not path.name.endswith(suffix):
        raise ValueError(
            f"Expected '<source>.<epoch>.cal.uvf', received '{path.name}'"
        )

    stem = path.name[: -len(suffix)]

    try:
        source, epoch = stem.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError(
            f"Expected '<source>.<epoch>.cal.uvf', received '{path.name}'"
        ) from exc

    if not source or not epoch:
        raise ValueError(
            f"Expected '<source>.<epoch>.cal.uvf', received '{path.name}'"
        )

    return source, epoch


def make_dataset(
    project_root: Path,
    calibrated_uvfits: Path,
    product_tag: str,
) -> Dataset:
    source, epoch = parse_calibrated_name(calibrated_uvfits)
    prefix = f"{source}.{epoch}"
    product_prefix = f"{prefix}.{product_tag}"
    stage1_directory = calibrated_uvfits.parent
    output_directory = project_root / "stage2" / prefix

    return Dataset(
        calibrated_uvfits=calibrated_uvfits.resolve(),
        source=source,
        epoch=epoch,
        source_model=(project_root / "inputs" / f"{source}.gmod").resolve(),
        stage1_model=(stage1_directory / f"{prefix}.gmod").resolve(),
        stage1_metadata=(stage1_directory / f"{prefix}.model.json").resolve(),
        stage2_model=(output_directory / f"{prefix}.{STAGE2_MODEL_SUFFIX}").resolve(),
        spectrum_csv=(output_directory / f"{product_prefix}.spectrum.csv").resolve(),
        plot_file=(output_directory / f"{product_prefix}.spectrum.png").resolve(),
        grouped_plot_file=(output_directory / f"{product_prefix}.grouped_spectrum.png").resolve(),
        ratio_plot_file=(output_directory / f"{product_prefix}.flux_ratios.png").resolve(),
        log_file=(output_directory / f"{product_prefix}.stage2.difmap.log").resolve(),
        metadata_file=(output_directory / f"{product_prefix}.stage2.metadata.json").resolve(),
        manifest_file=(output_directory / f"{product_prefix}.stage2.manifest.json").resolve(),
    )


def _strip_variable_suffix(token: str) -> str:
    """Remove a trailing DifMAP v/V variability suffix from one token."""
    return token[:-1] if token.lower().endswith("v") else token


def _make_variable(token: str) -> str:
    """Mark a numeric DifMAP parameter as variable exactly once."""
    return _strip_variable_suffix(token) + "v"


def transform_model_line(line: str) -> tuple[str, bool]:
    """
    Convert one DifMAP model row into a flux-only variable component.

    DifMAP model rows contain up to nine standard fields:
        Flux Radius Theta Major Ratio Phi T [Freq SpecIndex]

    The first field is made variable, all structural fields are fixed, the
    component type is preserved, and an explicitly present spectral index is
    fixed to zero. Short-form delta-component rows are supported without
    treating their final field as a spectral index.
    """
    if not line.strip() or line.lstrip().startswith("!"):
        return line, False

    newline = ""
    body = line
    if body.endswith("\r\n"):
        newline = "\r\n"
        body = body[:-2]
    elif body.endswith("\n"):
        newline = "\n"
        body = body[:-1]

    matches = list(re.finditer(r"\S+", body))
    if not matches:
        return line, False

    tokens = [match.group(0) for match in matches]

    # Validate that the model fields we modify are numeric (apart from v/V).
    for index, token in enumerate(tokens[:9]):
        cleaned = _strip_variable_suffix(token).replace("D", "E").replace("d", "e")
        try:
            float(cleaned)
        except ValueError as exc:
            raise ValueError(
                f"Non-numeric field {index + 1} in DifMAP model row: {line.rstrip()}"
            ) from exc

    replacements: dict[int, str] = {0: _make_variable(tokens[0])}

    # Radius, theta, major axis, axial ratio and major-axis PA are structural.
    for index in range(1, min(6, len(tokens))):
        replacements[index] = _strip_variable_suffix(tokens[index])

    # T is a fixed component-type code; Freq is also fixed when present.
    if len(tokens) >= 7:
        replacements[6] = _strip_variable_suffix(tokens[6])
    if len(tokens) >= 8:
        replacements[7] = _strip_variable_suffix(tokens[7])

    # Only the ninth standard field is SpecIndex. Seven-field rows end at T.
    if len(tokens) >= 9:
        replacements[8] = "0"

    output = body
    for index in sorted(replacements, reverse=True):
        match = matches[index]
        output = output[:match.start()] + replacements[index] + output[match.end():]

    transformed = output + newline
    return transformed, transformed != line


def create_stage2_model(
    stage1_model: Path,
    stage2_model: Path,
    overwrite: bool,
) -> tuple[int, int]:
    """
    Create the fixed-spectral-index model.

    Returns (component_line_count, modified_line_count).
    """
    if not stage1_model.is_file():
        raise FileNotFoundError(f"Stage 1 model not found: {stage1_model}")

    if stage1_model.resolve() == stage2_model.resolve():
        raise ValueError("Stage 1 and Stage 2 model paths must differ.")

    if stage2_model.exists() and not overwrite:
        raise FileExistsError(
            f"Stage 2 model already exists: {stage2_model}. "
            "Use --overwrite to replace it."
        )

    text = stage1_model.read_text(encoding="utf-8")
    transformed: list[str] = []
    component_lines = 0
    modified_lines = 0

    for line in text.splitlines(keepends=True):
        stripped = line.strip()

        if stripped and not line.lstrip().startswith("!"):
            component_lines += 1

        new_line, changed = transform_model_line(line)
        transformed.append(new_line)
        modified_lines += int(changed)

    # Preserve a final line in unusual files where splitlines returned nothing.
    if text and not transformed:
        new_line, changed = transform_model_line(text)
        transformed = [new_line]
        component_lines = int(bool(text.strip()) and not text.lstrip().startswith("!"))
        modified_lines = int(changed)

    stage2_model.parent.mkdir(parents=True, exist_ok=True)
    stage2_model.write_text("".join(transformed), encoding="utf-8")

    return component_lines, modified_lines


def header_axis_number(header: fits.Header, ctype_fragment: str) -> int | None:
    """Find a FITS axis whose CTYPE contains the requested fragment."""
    naxis = int(header.get("NAXIS", 0))
    for axis in range(1, naxis + 1):
        ctype = str(header.get(f"CTYPE{axis}", "")).upper()
        if ctype_fragment.upper() in ctype:
            return axis
    return None


def get_uvfits_frequencies(path: Path) -> list[float]:
    """
    Return concatenated channel frequencies in Hz.

    Supports the common UV-FITS arrangement where the random-groups primary
    header describes the intra-IF frequency axis and an AIPS FQ table supplies
    IF frequency offsets.
    """
    with fits.open(path, memmap=True) as hdul:
        primary = hdul[0]
        header = primary.header

        freq_axis = header_axis_number(header, "FREQ")
        if freq_axis is None:
            raise ValueError("Could not locate a FREQ axis in the UV-FITS header.")

        nchan = int(header.get(f"NAXIS{freq_axis}", 1))
        crval = float(header.get(f"CRVAL{freq_axis}", 0.0))
        crpix = float(header.get(f"CRPIX{freq_axis}", 1.0))
        cdelt = float(header.get(f"CDELT{freq_axis}", 0.0))

        channel_offsets = [
            ((index + 1) - crpix) * cdelt
            for index in range(nchan)
        ]

        fq_hdu = None
        for hdu in hdul[1:]:
            extname = str(hdu.header.get("EXTNAME", "")).strip().upper()
            if extname in {"AIPS FQ", "FQ"}:
                fq_hdu = hdu
                break

        if fq_hdu is None or fq_hdu.data is None:
            # No separate IF table: return the primary-header frequency axis.
            return [crval + offset for offset in channel_offsets]

        names = {name.upper(): name for name in fq_hdu.columns.names}
        if_name = names.get("IF FREQ")
        if if_name is None:
            raise ValueError("The AIPS FQ table has no 'IF FREQ' column.")

        # Use the first frequency setup row. This is the normal case for a
        # single-source exported UV-FITS dataset.
        raw_if_freq = fq_hdu.data[if_name][0]
        try:
            if_offsets = [float(value) for value in raw_if_freq]
        except TypeError:
            if_offsets = [float(raw_if_freq)]

        frequencies: list[float] = []
        for if_offset in if_offsets:
            for channel_offset in channel_offsets:
                frequencies.append(crval + if_offset + channel_offset)

        return frequencies


def get_observation_mjd(path: Path) -> float | None:
    """Read MJD-OBS or convert DATE-OBS from the UV-FITS primary header."""
    with fits.open(path, memmap=True) as hdul:
        header = hdul[0].header
        if header.get("MJD-OBS") is not None:
            return float(header["MJD-OBS"])
        date_obs = header.get("DATE-OBS")
        if date_obs:
            try:
                return float(Time(str(date_obs), format="isot", scale="utc").mjd)
            except ValueError:
                return float(Time(str(date_obs), scale="utc").mjd)
    return None


def representative_channel_width(frequencies: list[float]) -> float | None:
    """Estimate one channel width from adjacent concatenated frequencies."""
    spacings = [
        abs(b - a) for a, b in zip(frequencies, frequencies[1:])
        if b != a
    ]
    if not spacings:
        return None
    spacings.sort()
    return spacings[len(spacings) // 2]


def parse_numeric_token(token: str) -> float:
    """Parse a DifMAP numeric token, stripping a trailing v/V."""
    cleaned = token.strip()
    if cleaned.lower().endswith("v"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.replace("D", "E").replace("d", "e")
    return float(cleaned)


def model_component_fluxes(model_path: Path) -> list[float]:
    """Return first-column flux densities for all non-comment model rows."""
    fluxes: list[float] = []

    for line in model_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or line.lstrip().startswith("!"):
            continue

        fields = stripped.split()
        if not fields:
            continue

        try:
            fluxes.append(parse_numeric_token(fields[0]))
        except ValueError as exc:
            raise ValueError(
                f"Could not parse the first field as flux density in "
                f"{model_path}: {line}"
            ) from exc

    if not fluxes:
        raise ValueError(f"No model components found in {model_path}")

    return fluxes


def safe_label(name: str) -> str:
    """Convert a model/group label to a safe CSV field fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_")
    if not cleaned:
        raise ValueError(f"Invalid empty label derived from: {name!r}")
    return cleaned


def validate_output_labels(
    component_names: list[str],
    groups: list[tuple[str, list[int]]],
) -> None:
    """Validate group membership and all generated CSV column names."""
    membership = [0] * len(component_names)
    for group_name, indices in groups:
        if len(indices) != len(set(indices)):
            raise ValueError(f"Group {group_name!r} contains duplicate components.")
        for index in indices:
            membership[index] += 1

    missing = [component_names[i] for i, count in enumerate(membership) if count == 0]
    repeated = [component_names[i] for i, count in enumerate(membership) if count > 1]
    if missing:
        raise ValueError(f"Components omitted from all groups: {missing}")
    if repeated:
        raise ValueError(f"Components assigned to multiple groups: {repeated}")

    labels = [f"{safe_label(name)}_jy" for name in component_names]
    labels.extend(f"{safe_label(name)}_jy" for name, _ in groups)
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(
            "Component/group names collapse to duplicate CSV labels: "
            + ", ".join(duplicates)
        )


def load_model_metadata(
    metadata_path: Path,
    stage1_model: Path,
    expected_source: str,
    expected_epoch: str,
) -> tuple[list[tuple[str, list[int]]], list[str], list[dict[str, object]], dict[str, object]]:
    """Load and validate the Stage 1 component hierarchy metadata."""
    if not metadata_path.is_file():
        raise FileNotFoundError(f"Stage 1 model metadata not found: {metadata_path}")

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    if payload.get("source") != expected_source or payload.get("epoch") != expected_epoch:
        raise ValueError(
            "Stage 1 metadata source/epoch does not match the calibrated dataset."
        )

    raw_components = payload.get("components")
    raw_groups = payload.get("groups")
    if not isinstance(raw_components, list) or not isinstance(raw_groups, dict):
        raise ValueError("Stage 1 metadata lacks valid 'components' or 'groups'.")

    components = sorted(raw_components, key=lambda item: int(item["row_index"]))
    expected_indices = list(range(len(components)))
    actual_indices = [int(item["row_index"]) for item in components]
    if actual_indices != expected_indices:
        raise ValueError("Stage 1 component row indices are not contiguous from zero.")

    component_names = [str(item["name"]) for item in components]
    if len(component_names) != len(set(component_names)):
        raise ValueError("Duplicate component names in Stage 1 metadata.")

    name_to_index = {name: index for index, name in enumerate(component_names)}
    groups: list[tuple[str, list[int]]] = []
    for group_name, names in raw_groups.items():
        if not isinstance(names, list) or not names:
            raise ValueError(f"Group {group_name!r} has no components.")
        missing = [name for name in names if name not in name_to_index]
        if missing:
            raise ValueError(
                f"Group {group_name!r} references unknown components: {missing}"
            )
        groups.append((str(group_name), [name_to_index[name] for name in names]))

    validate_output_labels(component_names, groups)

    model_rows = model_component_fluxes(stage1_model)
    if len(model_rows) != len(components):
        raise ValueError(
            f"Stage 1 model has {len(model_rows)} rows but metadata defines "
            f"{len(components)} components."
        )

    row_labels = [f"{safe_label(name)}_jy" for name in component_names]
    return groups, row_labels, components, payload


def relabel_fitted_model(
    model_path: Path,
    components: list[dict[str, object]],
) -> None:
    """Reattach GROUP/COMPONENT labels to a DifMAP-written fitted model."""
    lines = model_path.read_text(encoding="utf-8").splitlines()
    data_indices = [
        i for i, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("!")
    ]
    if len(data_indices) != len(components):
        raise ValueError(
            f"Fitted model has {len(data_indices)} rows, but metadata defines "
            f"{len(components)} components."
        )

    data_set = set(data_indices)
    output: list[str] = []
    component_number = 0
    previous_group: str | None = None
    for index, line in enumerate(lines):
        if index not in data_set:
            if re.match(r"^!\s*(GROUP|COMPONENT)\b", line.strip(), re.I):
                continue
            output.append(line)
            continue
        component = components[component_number]
        group = str(component["group"])
        name = str(component["name"])
        if group != previous_group:
            output.append(f"! GROUP {group}")
            previous_group = group
        output.append(f"! COMPONENT {name}")
        output.append(line)
        component_number += 1

    model_path.write_text("\n".join(output) + "\n", encoding="utf-8")


def labelled_model_row_fluxes(
    fitted_model: Path,
    row_labels: list[str],
) -> dict[str, float]:
    """Return one fitted flux-density value for each labelled model row."""
    row_fluxes = model_component_fluxes(fitted_model)

    if len(row_fluxes) != len(row_labels):
        raise ValueError(
            f"Fitted model has {len(row_fluxes)} rows, but the original "
            f"model defines {len(row_labels)} labelled rows."
        )

    return dict(zip(row_labels, row_fluxes))


def grouped_model_fluxes(
    fitted_model: Path,
    groups: list[tuple[str, list[int]]],
) -> dict[str, float]:
    """Sum fitted model rows within each named physical component."""
    row_fluxes = model_component_fluxes(fitted_model)

    required_rows = 1 + max(
        row_index for _, rows in groups for row_index in rows
    )
    if len(row_fluxes) != required_rows:
        raise ValueError(
            f"Fitted model has {len(row_fluxes)} rows, but the original "
            f"model defines {required_rows} rows."
        )

    return {
        f"{safe_label(name)}_jy": sum(row_fluxes[index] for index in rows)
        for name, rows in groups
    }


def fit_model_path(
    work_directory: Path,
    mode: str,
    fit_index: int,
) -> Path:
    """Return the temporary fitted-model path for one channel or IF."""
    label = "channel" if mode == "channel" else "if"
    return work_directory / f"{label}_{fit_index:05d}.mod"


def build_difmap_commands(
    dataset: Dataset,
    fit_ranges: list[tuple[int, int, int]],
    work_directory: Path,
    mode: str,
) -> str:
    """
    Construct one DifMAP command stream for all requested channels or IFs.

    Each tuple is:
        (fit_index, first_channel, last_channel)
    """
    lines = [
        f"observe {dataset.calibrated_uvfits}",
        "select i",
    ]

    if CELL_SIZE_MAS is None:
        lines.append(f"mapsize {MAP_PIXELS}")
    else:
        lines.append(f"mapsize {MAP_PIXELS},{CELL_SIZE_MAS}")

    lines.extend(
        [
            UVW_COMMAND,
            "",
        ]
    )

    for fit_index, first_channel, last_channel in fit_ranges:
        fitted_model = fit_model_path(work_directory, mode, fit_index)

        lines.extend(
            [
                f"select i,{first_channel},{last_channel}",
                f"rmodel {dataset.stage2_model}",
                f"modelfit {MODELFIT_ITERATIONS}",
                "invert",
                f'print "{RMS_LOG_MARKER}", {fit_index}, imstat(rms)',
                f"wmodel {fitted_model}",
                "",
            ]
        )

    lines.append("quit")
    lines.append("")
    return "\n".join(lines)

def tagged_rms_values(log_text: str) -> dict[int, float]:
    """Extract explicitly tagged ``fit_index -> RMS`` values from a log."""
    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
    pattern = re.compile(
        rf"^\s*!?\s*{re.escape(RMS_LOG_MARKER)}\s+(?P<index>\d+)\s+"
        rf"(?P<value>{number})\s*$"
    )
    values: dict[int, float] = {}

    for raw_line in log_text.splitlines():
        match = pattern.fullmatch(raw_line)
        if match is None:
            continue
        fit_index = int(match.group("index"))
        if fit_index in values:
            raise ValueError(f"Duplicate tagged RMS for fit {fit_index}.")
        values[fit_index] = float(
            match.group("value").replace("D", "E").replace("d", "e")
        )

    return values


def modelfit_flux_measurements_by_fit(
    log_text: str,
    expected_component_count: int,
) -> dict[int, list[tuple[float, float]]]:
    """Extract final per-component ``(flux, Stdev)`` values for each fit.

    The number of component rows is supplied by the Stage 1 hierarchy metadata;
    no lens-specific component or image count is assumed. Only DifMAP's final
    post-``modelfit`` ``Flux (Jy) / Value Stdev`` tables are parsed. Iteration
    model dumps are deliberately ignored.

    DifMAP can occasionally interleave asynchronous map-size warnings in the
    middle of a printed component row. The parser therefore assembles logical
    numeric rows while inside the final table and tolerates such interruptions.
    The following ``STAGE2_RMS`` marker associates the completed table with its
    fit index.
    """
    if expected_component_count < 1:
        raise ValueError("expected_component_count must be positive.")

    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
    component_pattern = re.compile(
        rf"^\s*!?\s*(?P<flux>{number})\s+(?P<stdev>{number})\s+"
        rf"(?P<east>{number})\s+(?P<east_stdev>{number})\s+"
        rf"(?P<north>{number})\s+(?P<north_stdev>{number})\s+"
        rf"(?P<shape>[A-Za-z][A-Za-z0-9_-]*)\s+"
    )
    marker_pattern = re.compile(
        rf"^\s*!?\s*{re.escape(RMS_LOG_MARKER)}\s+(?P<index>\d+)\s+"
        rf"(?P<value>{number})\s*$"
    )
    final_table_rule = re.compile(r"^\s*#-+")
    numeric_start = re.compile(r"^\s*[+-]?(?:\d|\.)")

    values: dict[int, list[tuple[float, float]]] = {}
    in_final_table = False
    table_rows: list[tuple[float, float]] = []
    pending_numeric = ""

    def parse_component_row(text: str) -> tuple[float, float] | None:
        match = component_pattern.match(text)
        if match is None:
            return None
        flux = float(match.group("flux").replace("D", "E").replace("d", "e"))
        stdev = float(match.group("stdev").replace("D", "E").replace("d", "e"))
        return flux, stdev

    for raw_line in log_text.splitlines():
        # DifMAP may print map-size warnings asynchronously in the middle of a
        # component row. Remove the warning text while preserving any numeric
        # fragment that preceded it; the continuation is handled below.
        if "Your choice of large map pixels excluded" in raw_line:
            raw_line = raw_line.split("Your choice of large map pixels excluded", 1)[0]
        if raw_line.lstrip().startswith("The y-axis pixel size should ideally"):
            continue

        marker_match = marker_pattern.fullmatch(raw_line)
        if marker_match is not None:
            fit_index = int(marker_match.group("index"))
            if fit_index in values:
                raise ValueError(f"Duplicate final Flux/Stdev table for fit {fit_index}.")
            if not in_final_table:
                raise ValueError(
                    f"Fit {fit_index}: RMS marker encountered without a preceding "
                    "final Flux/Stdev table."
                )
            if pending_numeric:
                raise ValueError(
                    f"Fit {fit_index}: incomplete component row remained before RMS marker: "
                    f"{pending_numeric!r}"
                )
            if len(table_rows) != expected_component_count:
                raise ValueError(
                    f"Fit {fit_index}: final Flux/Stdev table contains {len(table_rows)} "
                    f"component rows; Stage 1 hierarchy defines {expected_component_count}."
                )
            values[fit_index] = list(table_rows)
            in_final_table = False
            table_rows = []
            pending_numeric = ""
            continue

        if final_table_rule.match(raw_line):
            in_final_table = True
            table_rows = []
            pending_numeric = ""
            continue

        if not in_final_table:
            continue

        # Ignore normal prose/warning lines inside the table. If DifMAP split a
        # numeric row, a later numeric-looking continuation will be joined below.
        if not numeric_start.match(raw_line):
            continue

        if pending_numeric:
            # First try direct concatenation (needed when an exponent such as
            # ``0.0e+00`` was split after the ``e``), then token-boundary join.
            attempts = [
                pending_numeric + raw_line.lstrip(),
                pending_numeric + " " + raw_line.strip(),
            ]
            parsed = None
            for attempt in attempts:
                parsed = parse_component_row(attempt)
                if parsed is not None:
                    break
            if parsed is not None:
                table_rows.append(parsed)
                pending_numeric = ""
                continue

            # If the new physical line is independently a complete component
            # row, the buffered text was merely the trailing continuation of a
            # previous row whose useful prefix had already been parsed. Drop it.
            parsed_new = parse_component_row(raw_line)
            if parsed_new is not None:
                table_rows.append(parsed_new)
                pending_numeric = ""
                continue

            pending_numeric += raw_line.strip()
            continue

        parsed = parse_component_row(raw_line)
        if parsed is not None:
            table_rows.append(parsed)
        else:
            pending_numeric = raw_line.rstrip()

    return values

def modelfit_flux_stdevs_by_fit(
    log_text: str,
    expected_component_count: int,
) -> dict[int, list[float]]:
    """Backwards-compatible view returning only formal flux Stdev values."""
    measurements = modelfit_flux_measurements_by_fit(
        log_text, expected_component_count=expected_component_count
    )
    return {
        fit_index: [stdev for _, stdev in rows]
        for fit_index, rows in measurements.items()
    }

def error_label(flux_label: str) -> str:
    """Return the CSV formal-error column paired with one ``*_jy`` flux column."""
    if not flux_label.endswith("_jy"):
        raise ValueError(f"Flux label does not end in '_jy': {flux_label}")
    return flux_label[:-3] + "_error_jy"


def write_spectrum(
    output_path: Path,
    rows: list[dict[str, object]],
    output_labels: list[str],
    error_labels: list[str],
) -> None:
    fieldnames = [
        "source",
        "epoch",
        "mjd",
        "mode",
        "fit_index",
        "first_channel",
        "last_channel",
        "channels_averaged",
        "frequency_hz",
        "frequency_ghz",
        "bandwidth_hz",
        *output_labels,
        *error_labels,
        "rms_jy_per_beam",
        "fit_status",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

def write_stage2_metadata(
    dataset: Dataset,
    mode: str,
    channels_per_if: int,
    exclude_edge_channels: int,
    total_channels: int,
    fit_count: int,
    observation_mjd: float | None,
    component_groups: list[tuple[str, list[int]]],
    components: list[dict[str, object]],
    stage1_metadata: dict[str, object],
) -> None:
    payload = {
        "schema_version": "1.0",
        "stage": 2,
        "stage2_version": STAGE2_VERSION,
        "source": dataset.source,
        "epoch": dataset.epoch,
        "mjd": observation_mjd,
        "mode": mode,
        "channels_per_if": channels_per_if if mode == "if" else 1,
        "excluded_edge_channels_per_side": (
            exclude_edge_channels if mode == "if" else 0
        ),
        "fitted_channels_per_if": (
            channels_per_if - 2 * exclude_edge_channels
            if mode == "if" else 1
        ),
        "total_channels": total_channels,
        "n_fits": fit_count,
        "stage1_model": dataset.stage1_model.name,
        "stage1_metadata": dataset.stage1_metadata.name,
        "stage1_version": stage1_metadata.get("stage1_version"),
        "stage2_model": dataset.stage2_model.name,
        "n_groups": len(component_groups),
        "n_components": len(components),
        "groups": {
            name: [str(components[index]["name"]) for index in indices]
            for name, indices in component_groups
        },
        "components": components,
        "uncertainty_model": {
            "component_flux_errors": "DifMAP modelfit Flux Stdev parsed from stdout",
            "grouped_flux_errors": "quadrature sum of member-component formal errors; covariance unavailable",
            "rms_jy_per_beam": "residual-image RMS retained as a channel-quality diagnostic",
        },
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }
    dataset.metadata_file.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def write_stage2_manifest(
    dataset: Dataset,
    retained_models_directory: Path | None,
) -> None:
    payload = {
        "schema_version": "1.0",
        "stage": 2,
        "source": dataset.source,
        "epoch": dataset.epoch,
        "metadata": dataset.metadata_file.name,
        "spectrum": dataset.spectrum_csv.name,
        "plot": dataset.plot_file.name if dataset.plot_file.is_file() else None,
        "grouped_spectrum_plot": (
            dataset.grouped_plot_file.name
            if dataset.grouped_plot_file.is_file() else None
        ),
        "flux_ratio_plot": (
            dataset.ratio_plot_file.name
            if dataset.ratio_plot_file.is_file() else None
        ),
        "log": dataset.log_file.name,
        "stage2_model": dataset.stage2_model.name,
        "stage1_model_metadata": dataset.stage1_metadata.name,
        "fitted_models_directory": (
            retained_models_directory.name
            if retained_models_directory is not None else None
        ),
    }
    dataset.manifest_file.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def observation_plot_title(
    data: object,
    source: str,
    epoch: str,
    description: str,
) -> str:
    """Build a plot title and include the observation MJD when available."""
    title = f"{source}.{epoch} {description}"
    try:
        import pandas as pd

        if "mjd" in data.columns:
            mjd_values = pd.to_numeric(data["mjd"], errors="coerce").dropna()
            if not mjd_values.empty:
                title += f" (MJD {float(mjd_values.iloc[0]):.2f})"
    except (AttributeError, TypeError, ValueError):
        pass
    return title


def plot_spectrum(
    csv_path: Path,
    plot_path: Path,
    output_labels: list[str],
    source: str,
    epoch: str,
) -> None:
    """Create a quick-look flux-density spectrum from the Stage 2 CSV."""
    import pandas as pd

    data = pd.read_csv(csv_path)

    required = {"frequency_ghz", "rms_jy_per_beam", "fit_status"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(
            "Missing required plotting column(s): "
            + ", ".join(sorted(missing))
        )

    good = data.loc[data["fit_status"].eq("ok")].copy()
    good["frequency_ghz"] = pd.to_numeric(
        good["frequency_ghz"],
        errors="coerce",
    )
    good["rms_jy_per_beam"] = pd.to_numeric(
        good["rms_jy_per_beam"],
        errors="coerce",
    )

    for label in output_labels:
        good[label] = pd.to_numeric(good[label], errors="coerce")

    good = good.dropna(subset=["frequency_ghz"])
    good = good.sort_values("frequency_ghz")

    if good.empty:
        raise ValueError("No successful finite measurements are available to plot.")

    fig, ax = plt.subplots(figsize=(8, 5))

    for label in output_labels:
        finite = good[["frequency_ghz", label, "rms_jy_per_beam"]].dropna(
            subset=["frequency_ghz", label]
        )
        if finite.empty:
            continue

        display_label = label.removesuffix("_jy")

        if PLOT_ERROR_BARS and finite["rms_jy_per_beam"].notna().all():
            ax.errorbar(
                finite["frequency_ghz"],
                finite[label],
                yerr=finite["rms_jy_per_beam"],
                fmt="o-",
                linewidth=1,
                markersize=4,
                capsize=2,
                label=display_label,
            )
        else:
            ax.plot(
                finite["frequency_ghz"],
                finite[label],
                "o-",
                linewidth=1,
                markersize=4,
                label=display_label,
            )

    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel("Flux density (Jy)")
    ax.set_title(observation_plot_title(data, source, epoch, "spectrum"))
    ax.grid(True, alpha=0.3)
    ax.legend()

    fig.tight_layout()
    fig.savefig(plot_path, dpi=PLOT_DPI, bbox_inches="tight")
    plt.close(fig)


def plot_grouped_spectrum(
    csv_path: Path,
    plot_path: Path,
    grouped_labels: list[str],
    source: str,
    epoch: str,
) -> None:
    """Plot only the summed physical-component spectra."""
    import pandas as pd

    data = pd.read_csv(csv_path)
    good = data.loc[data["fit_status"].eq("ok")].copy()
    good["frequency_ghz"] = pd.to_numeric(
        good["frequency_ghz"], errors="coerce"
    )
    good["rms_jy_per_beam"] = pd.to_numeric(
        good["rms_jy_per_beam"], errors="coerce"
    )
    for label in grouped_labels:
        good[label] = pd.to_numeric(good[label], errors="coerce")
    good = good.dropna(subset=["frequency_ghz"]).sort_values("frequency_ghz")
    if good.empty:
        raise ValueError("No successful measurements are available to plot.")

    fig, ax = plt.subplots(figsize=(8, 5))
    for label in grouped_labels:
        finite = good[["frequency_ghz", label, "rms_jy_per_beam"]].dropna(
            subset=["frequency_ghz", label]
        )
        if finite.empty:
            continue
        display_label = label.removesuffix("_jy")
        if PLOT_ERROR_BARS and finite["rms_jy_per_beam"].notna().all():
            ax.errorbar(
                finite["frequency_ghz"], finite[label],
                yerr=finite["rms_jy_per_beam"], fmt="o-",
                linewidth=1, markersize=4, capsize=2, label=display_label,
            )
        else:
            ax.plot(
                finite["frequency_ghz"], finite[label], "o-",
                linewidth=1, markersize=4, label=display_label,
            )

    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel("Combined flux density (Jy)")
    ax.set_title(observation_plot_title(data, source, epoch, "grouped-component spectra"))
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=PLOT_DPI, bbox_inches="tight")
    plt.close(fig)


def plot_flux_ratios(
    csv_path: Path,
    plot_path: Path,
    grouped_labels: list[str],
    source: str,
    epoch: str,
) -> None:
    """Plot grouped-component flux ratios relative to the first group."""
    import pandas as pd

    if not grouped_labels:
        raise ValueError("No grouped components are available for ratio plotting.")

    reference_label = grouped_labels[0]
    reference_name = reference_label.removesuffix("_jy")
    data = pd.read_csv(csv_path)
    good = data.loc[data["fit_status"].eq("ok")].copy()
    good["frequency_ghz"] = pd.to_numeric(
        good["frequency_ghz"], errors="coerce"
    )
    for label in grouped_labels:
        good[label] = pd.to_numeric(good[label], errors="coerce")
    good = good.dropna(subset=["frequency_ghz", reference_label])
    good = good.loc[good[reference_label] != 0].sort_values("frequency_ghz")
    if good.empty:
        raise ValueError("No valid reference-component measurements are available.")

    fig, ax = plt.subplots(figsize=(8, 5))
    plotted = 0
    for label in grouped_labels[1:]:
        finite = good[["frequency_ghz", reference_label, label]].dropna()
        if finite.empty:
            continue
        ratio = finite[label] / finite[reference_label]
        ax.plot(
            finite["frequency_ghz"], ratio, "o-",
            linewidth=1, markersize=4,
            label=f"{label.removesuffix('_jy')}/{reference_name}",
        )
        plotted += 1

    if plotted == 0:
        plt.close(fig)
        raise ValueError("No non-reference grouped components are available.")

    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel(f"Flux-density ratio relative to {reference_name}")
    ax.set_title(observation_plot_title(data, source, epoch, "grouped-component flux ratios"))
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=PLOT_DPI, bbox_inches="tight")
    plt.close(fig)


def parse_channel_spec(spec: str | None, total_channels: int) -> list[int]:
    """
    Parse one-based channel selections such as:
        1-10,15,20-25
    """
    if spec is None:
        return list(range(1, total_channels + 1))

    selected: set[int] = set()

    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue

        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"Invalid descending channel range: {item}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(item))

    if not selected:
        raise ValueError("No channels were selected.")

    invalid = sorted(
        channel for channel in selected
        if channel < 1 or channel > total_channels
    )
    if invalid:
        raise ValueError(
            f"Channel(s) outside valid range 1-{total_channels}: {invalid}"
        )

    return sorted(selected)


def make_fit_ranges(
    total_channels: int,
    mode: str,
    channels_per_if: int,
    channel_spec: str | None,
    exclude_edge_channels: int = 0,
) -> list[tuple[int, int, int]]:
    """
    Return (fit_index, first_channel, last_channel) tuples.

    In channel mode, each selected channel is fitted separately.
    In IF mode, each physical IF retains its original numbering, but the
    requested number of channels is excluded from both edges before fitting.
    """
    if mode == "channel":
        channels = parse_channel_spec(channel_spec, total_channels)
        return [(channel, channel, channel) for channel in channels]

    if channel_spec is not None:
        raise ValueError("--channels is only valid with --mode channel.")

    if channels_per_if < 1:
        raise ValueError("--channels-per-if must be at least 1.")
    if exclude_edge_channels < 0:
        raise ValueError("--exclude-edge-channels cannot be negative.")
    if 2 * exclude_edge_channels >= channels_per_if:
        raise ValueError(
            "Twice --exclude-edge-channels must be smaller than "
            "--channels-per-if."
        )

    ranges: list[tuple[int, int, int]] = []
    if_number = 1

    for if_first_channel in range(1, total_channels + 1, channels_per_if):
        if_last_channel = min(
            if_first_channel + channels_per_if - 1,
            total_channels,
        )
        actual_if_size = if_last_channel - if_first_channel + 1

        if actual_if_size != channels_per_if:
            raise ValueError(
                f"Final IF contains {actual_if_size} channels rather than "
                f"{channels_per_if}; refusing to apply symmetric edge exclusion."
            )

        first_channel = if_first_channel + exclude_edge_channels
        last_channel = if_last_channel - exclude_edge_channels
        ranges.append((if_number, first_channel, last_channel))
        if_number += 1

    return ranges


def run_dataset(
    dataset: Dataset,
    difmap_executable: str,
    mode: str,
    channels_per_if: int,
    exclude_edge_channels: int,
    channel_spec: str | None,
    overwrite: bool,
    keep_channel_models: bool,
    dry_run: bool,
    recover_from_log: bool = False,
) -> bool:
    print(f"\nDataset:         {dataset.calibrated_uvfits.name}")
    print(f"Stage 1 model:   {dataset.stage1_model.name}")

    if not dataset.calibrated_uvfits.is_file():
        print(
            f"ERROR: calibrated UV data not found: {dataset.calibrated_uvfits}",
            file=sys.stderr,
        )
        return False

    if not dataset.stage1_model.is_file():
        print(
            f"ERROR: Stage 1 model not found: {dataset.stage1_model}",
            file=sys.stderr,
        )
        return False

    try:
        component_groups, row_labels, components, stage1_metadata = load_model_metadata(
            metadata_path=dataset.stage1_metadata,
            stage1_model=dataset.stage1_model,
            expected_source=dataset.source,
            expected_epoch=dataset.epoch,
        )
    except Exception as exc:
        print(f"ERROR loading Stage 1 model metadata: {exc}", file=sys.stderr)
        return False

    grouped_labels = [
        f"{safe_label(name)}_jy" for name, _ in component_groups
    ]
    output_labels = [*row_labels, *grouped_labels]
    row_error_labels = [error_label(label) for label in row_labels]
    grouped_error_labels = [error_label(label) for label in grouped_labels]
    error_labels = [*row_error_labels, *grouped_error_labels]

    try:
        frequencies = get_uvfits_frequencies(dataset.calibrated_uvfits)
    except Exception as exc:
        print(f"ERROR reading frequencies: {exc}", file=sys.stderr)
        return False

    total_channels = len(frequencies)
    channel_width_hz = representative_channel_width(frequencies)
    try:
        observation_mjd = get_observation_mjd(dataset.calibrated_uvfits)
    except Exception as exc:
        print(f"WARNING: could not determine observation MJD: {exc}", file=sys.stderr)
        observation_mjd = None

    try:
        fit_ranges = make_fit_ranges(
            total_channels=total_channels,
            mode=mode,
            channels_per_if=channels_per_if,
            channel_spec=channel_spec,
            exclude_edge_channels=(
                exclude_edge_channels if mode == "if" else 0
            ),
        )
    except (ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return False

    if dry_run:
        dry_run_directory = dataset.spectrum_csv.parent / "_dry_run_models"
        commands = build_difmap_commands(
            dataset=dataset,
            fit_ranges=fit_ranges,
            work_directory=dry_run_directory,
            mode=mode,
        )
        print(f"Model metadata:  {dataset.stage1_metadata.name}")
        print(f"Stage 2 model:   {dataset.stage2_model.name}")
        print(f"UV-FITS channels:{total_channels:>5}")
        print(f"Fitting mode:    {mode:>8}")
        print(f"Fits to perform: {len(fit_ranges):>5}")
        print("\n--- DifMAP commands ---")
        print(commands)
        print("--- End DifMAP commands ---")
        return True

    if dataset.spectrum_csv.exists() and not overwrite:
        print(
            f"SKIPPED: output exists: {dataset.spectrum_csv.name}. "
            "Use --overwrite to replace it."
        )
        return False

    try:
        component_lines, modified_lines = create_stage2_model(
            stage1_model=dataset.stage1_model,
            stage2_model=dataset.stage2_model,
            overwrite=overwrite or dataset.stage2_model.exists(),
        )
    except Exception as exc:
        print(f"ERROR creating Stage 2 model: {exc}", file=sys.stderr)
        return False

    print(f"Model metadata:  {dataset.stage1_metadata.name}")
    print(f"Stage 2 model:   {dataset.stage2_model.name}")
    print(
        "Flux columns:     "
        + ", ".join(output_labels)
    )
    print(
        "Formal errors:    "
        + ", ".join(error_labels)
    )
    print(f"Model components:{component_lines:>5}")
    print(f"Normalised rows: {modified_lines:>5}")
    print(f"UV-FITS channels:{total_channels:>5}")
    print(f"Fitting mode:    {mode:>8}")
    if mode == "if":
        print(f"Channels per IF: {channels_per_if:>8}")
        print(f"Edge exclusion:  {exclude_edge_channels:>8} per side")
        print(
            f"Channels fitted: {channels_per_if - 2 * exclude_edge_channels:>8} per IF"
        )
        print(
            f"First IF range:  {fit_ranges[0][1]}-{fit_ranges[0][2]}"
        )
        print(
            f"Last IF range:   {fit_ranges[-1][1]}-{fit_ranges[-1][2]}"
        )
    print(f"Fits to perform: {len(fit_ranges):>5}")

    if recover_from_log:
        log_path = dataset.log_file
        if not log_path.is_file():
            print(f"ERROR: recovery log not found: {log_path}", file=sys.stderr)
            return False
        if keep_channel_models:
            print(
                "WARNING: --keep-channel-models has no effect in log-recovery mode; "
                "temporary fitted models are not recreated.",
                file=sys.stderr,
            )

        print(f"Recovery log:    {log_path.name}")
        print("Recovering Stage 2 measurements without running DifMAP...")
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            rms_by_fit = tagged_rms_values(log_text)
            measurements_by_fit = modelfit_flux_measurements_by_fit(
                log_text, expected_component_count=len(row_labels)
            )
        except Exception as exc:
            print(f"ERROR parsing recovery log: {exc}", file=sys.stderr)
            return False

        expected_fit_indices = {fit_index for fit_index, _, _ in fit_ranges}
        rms_indices = set(rms_by_fit)
        measurement_indices = set(measurements_by_fit)
        missing_rms = sorted(expected_fit_indices - rms_indices)
        extra_rms = sorted(rms_indices - expected_fit_indices)
        missing_measurements = sorted(expected_fit_indices - measurement_indices)
        extra_measurements = sorted(measurement_indices - expected_fit_indices)
        if missing_rms or extra_rms or missing_measurements or extra_measurements:
            print("ERROR: recovery-log fit indices do not match the requested extraction.", file=sys.stderr)
            print(
                f"Requested={len(expected_fit_indices)}, RMS={len(rms_indices)}, "
                f"Flux/Stdev={len(measurement_indices)}",
                file=sys.stderr,
            )
            if missing_rms:
                print(f"Missing RMS fits: {missing_rms[:20]}", file=sys.stderr)
            if extra_rms:
                print(f"Unexpected RMS fits: {extra_rms[:20]}", file=sys.stderr)
            if missing_measurements:
                print(f"Missing Flux/Stdev fits: {missing_measurements[:20]}", file=sys.stderr)
            if extra_measurements:
                print(f"Unexpected Flux/Stdev fits: {extra_measurements[:20]}", file=sys.stderr)
            return False

        if overwrite:
            for path in (
                dataset.spectrum_csv,
                dataset.plot_file,
                dataset.grouped_plot_file,
                dataset.ratio_plot_file,
                dataset.metadata_file,
                dataset.manifest_file,
            ):
                if path.exists():
                    path.unlink()

        rows: list[dict[str, object]] = []
        for fit_index, first_channel, last_channel in fit_ranges:
            selected_frequencies = frequencies[first_channel - 1:last_channel]
            frequency_hz = sum(selected_frequencies) / len(selected_frequencies)
            row_base: dict[str, object] = {
                "source": dataset.source,
                "epoch": dataset.epoch,
                "mjd": ("" if observation_mjd is None else f"{observation_mjd:.9f}"),
                "mode": mode,
                "fit_index": fit_index,
                "first_channel": first_channel,
                "last_channel": last_channel,
                "channels_averaged": last_channel - first_channel + 1,
                "frequency_hz": f"{frequency_hz:.9f}",
                "frequency_ghz": f"{frequency_hz / 1e9:.12f}",
                "bandwidth_hz": (
                    "" if channel_width_hz is None
                    else f"{channel_width_hz * (last_channel - first_channel + 1):.9f}"
                ),
            }

            measurements = measurements_by_fit[fit_index]
            if len(measurements) != len(row_labels):
                print(
                    f"ERROR: fit {fit_index} has {len(measurements)} component rows; "
                    f"hierarchy defines {len(row_labels)}.",
                    file=sys.stderr,
                )
                return False

            component_fluxes = [value for value, _ in measurements]
            component_stdevs = [stdev for _, stdev in measurements]
            row_fluxes = {
                label: component_fluxes[index]
                for index, label in enumerate(row_labels)
            }
            grouped_fluxes = {
                f"{safe_label(group_name)}_jy": sum(component_fluxes[index] for index in indices)
                for group_name, indices in component_groups
            }
            output_fluxes = {**row_fluxes, **grouped_fluxes}
            row_errors = {
                error_label(label): component_stdevs[index]
                for index, label in enumerate(row_labels)
            }
            grouped_errors = {
                error_label(f"{safe_label(group_name)}_jy"): math.sqrt(
                    sum(component_stdevs[index] ** 2 for index in indices)
                )
                for group_name, indices in component_groups
            }
            output_errors = {**row_errors, **grouped_errors}

            rows.append(
                {
                    **row_base,
                    **{label: f"{output_fluxes[label]:.12g}" for label in output_labels},
                    **{label: f"{output_errors[label]:.12g}" for label in error_labels},
                    "rms_jy_per_beam": f"{rms_by_fit[fit_index]:.12g}",
                    "fit_status": "ok",
                }
            )

        write_spectrum(dataset.spectrum_csv, rows, output_labels, error_labels)

        if PLOT_SPECTRUM:
            plotting_jobs = [
                (
                    "spectrum", plot_spectrum,
                    {
                        "csv_path": dataset.spectrum_csv,
                        "plot_path": dataset.plot_file,
                        "output_labels": output_labels,
                        "source": dataset.source,
                        "epoch": dataset.epoch,
                    },
                ),
                (
                    "grouped spectrum", plot_grouped_spectrum,
                    {
                        "csv_path": dataset.spectrum_csv,
                        "plot_path": dataset.grouped_plot_file,
                        "grouped_labels": grouped_labels,
                        "source": dataset.source,
                        "epoch": dataset.epoch,
                    },
                ),
            ]
            if len(grouped_labels) > 1:
                plotting_jobs.append(
                    (
                        "flux ratios", plot_flux_ratios,
                        {
                            "csv_path": dataset.spectrum_csv,
                            "plot_path": dataset.ratio_plot_file,
                            "grouped_labels": grouped_labels,
                            "source": dataset.source,
                            "epoch": dataset.epoch,
                        },
                    )
                )
            for plot_name, plot_function, plot_kwargs in plotting_jobs:
                try:
                    plot_function(**plot_kwargs)
                except Exception as exc:
                    print(f"WARNING: could not create {plot_name} plot: {exc}", file=sys.stderr)

        write_stage2_metadata(
            dataset=dataset, mode=mode, channels_per_if=channels_per_if,
            exclude_edge_channels=exclude_edge_channels, total_channels=total_channels,
            fit_count=len(fit_ranges), observation_mjd=observation_mjd,
            component_groups=component_groups, components=components,
            stage1_metadata=stage1_metadata,
        )
        write_stage2_manifest(dataset, None)
        print("Completed Stage 2 recovery from existing DifMAP log.")
        print(f"Spectrum CSV:    {dataset.spectrum_csv.name}")
        if dataset.plot_file.is_file():
            print(f"Spectrum plot:   {dataset.plot_file.name}")
        if dataset.grouped_plot_file.is_file():
            print(f"Grouped plot:    {dataset.grouped_plot_file.name}")
        if dataset.ratio_plot_file.is_file():
            print(f"Ratio plot:      {dataset.ratio_plot_file.name}")
        print(f"DifMAP log:      {dataset.log_file.name}")
        print(f"Metadata:        {dataset.metadata_file.name}")
        print(f"Manifest:        {dataset.manifest_file.name}")
        print(f"Recovered fits:  {len(rows)}/{len(fit_ranges)}")
        return True

    if overwrite:
        for path in (
            dataset.spectrum_csv,
            dataset.plot_file,
            dataset.grouped_plot_file,
            dataset.ratio_plot_file,
            dataset.log_file,
            dataset.metadata_file,
            dataset.manifest_file,
        ):
            if path.exists():
                path.unlink()

        stale_models_directory = dataset.spectrum_csv.parent / f"models_{mode}"
        if stale_models_directory.exists():
            shutil.rmtree(stale_models_directory)

    retained_models_directory: Path | None = None
    temporary_parent = dataset.spectrum_csv.parent
    temp_context = tempfile.TemporaryDirectory(
        prefix=f"{dataset.source}.{dataset.epoch}.stage2_",
        dir=temporary_parent,
    )

    with temp_context as temp_name:
        work_directory = Path(temp_name)
        commands = build_difmap_commands(
            dataset=dataset,
            fit_ranges=fit_ranges,
            work_directory=work_directory,
            mode=mode,
        )

        print(f"Running DifMAP {mode} fits...")

        try:
            process = subprocess.run(
                [difmap_executable],
                input=commands,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except FileNotFoundError:
            print(
                f"ERROR: DifMAP executable not found: {difmap_executable}",
                file=sys.stderr,
            )
            return False
        except OSError as exc:
            print(f"ERROR starting DifMAP: {exc}", file=sys.stderr)
            return False

        dataset.log_file.write_text(process.stdout, encoding="utf-8")

        if process.returncode != 0:
            print(
                f"FAILED: DifMAP returned status {process.returncode}.",
                file=sys.stderr,
            )
            print(f"See log: {dataset.log_file}")
            return False

        try:
            rms_by_fit = tagged_rms_values(process.stdout)
        except ValueError as exc:
            print(f"ERROR parsing tagged RMS values: {exc}", file=sys.stderr)
            print(f"See log: {dataset.log_file}", file=sys.stderr)
            return False

        expected_fit_indices = {fit_index for fit_index, _, _ in fit_ranges}
        recovered_fit_indices = set(rms_by_fit)
        missing_rms = sorted(expected_fit_indices - recovered_fit_indices)
        unexpected_rms = sorted(recovered_fit_indices - expected_fit_indices)
        if missing_rms or unexpected_rms:
            print(
                "ERROR: tagged RMS measurements do not match the requested fits.",
                file=sys.stderr,
            )
            print(
                f"Requested fits: {len(expected_fit_indices)}; "
                f"tagged RMS values: {len(recovered_fit_indices)}",
                file=sys.stderr,
            )
            if missing_rms:
                preview = missing_rms[:20]
                suffix = " ..." if len(missing_rms) > len(preview) else ""
                print(f"Missing fit indices: {preview}{suffix}", file=sys.stderr)
            if unexpected_rms:
                preview = unexpected_rms[:20]
                suffix = " ..." if len(unexpected_rms) > len(preview) else ""
                print(f"Unexpected fit indices: {preview}{suffix}", file=sys.stderr)
            print(f"See log: {dataset.log_file}", file=sys.stderr)
            return False

        try:
            stdevs_by_fit = modelfit_flux_stdevs_by_fit(
                process.stdout, expected_component_count=len(row_labels)
            )
        except ValueError as exc:
            print(f"ERROR parsing DifMAP formal flux errors: {exc}", file=sys.stderr)
            print(f"See log: {dataset.log_file}", file=sys.stderr)
            return False

        recovered_stdev_indices = set(stdevs_by_fit)
        missing_stdevs = sorted(expected_fit_indices - recovered_stdev_indices)
        unexpected_stdevs = sorted(recovered_stdev_indices - expected_fit_indices)
        if missing_stdevs or unexpected_stdevs:
            print(
                "ERROR: DifMAP formal-error tables do not match the requested fits.",
                file=sys.stderr,
            )
            if missing_stdevs:
                print(f"Missing formal errors for fits: {missing_stdevs[:20]}", file=sys.stderr)
            if unexpected_stdevs:
                print(f"Unexpected formal errors for fits: {unexpected_stdevs[:20]}", file=sys.stderr)
            print(f"See log: {dataset.log_file}", file=sys.stderr)
            return False

        rows: list[dict[str, object]] = []

        for result_index, (fit_index, first_channel, last_channel) in enumerate(
            fit_ranges
        ):
            fitted_model = fit_model_path(
                work_directory,
                mode,
                fit_index,
            )

            selected_frequencies = frequencies[
                first_channel - 1:last_channel
            ]
            frequency_hz = sum(selected_frequencies) / len(selected_frequencies)

            row_base: dict[str, object] = {
                "source": dataset.source,
                "epoch": dataset.epoch,
                "mjd": ("" if observation_mjd is None else f"{observation_mjd:.9f}"),
                "mode": mode,
                "fit_index": fit_index,
                "first_channel": first_channel,
                "last_channel": last_channel,
                "channels_averaged": last_channel - first_channel + 1,
                "frequency_hz": f"{frequency_hz:.9f}",
                "frequency_ghz": f"{frequency_hz / 1e9:.12f}",
                "bandwidth_hz": (
                    "" if channel_width_hz is None
                    else f"{channel_width_hz * (last_channel - first_channel + 1):.9f}"
                ),
            }

            if not fitted_model.is_file():
                rows.append(
                    {
                        **row_base,
                        **{label: "" for label in output_labels},
                        **{label: "" for label in error_labels},
                        "rms_jy_per_beam": f"{rms_by_fit[fit_index]:.12g}",
                        "fit_status": "missing_model",
                    }
                )
                continue

            try:
                relabel_fitted_model(fitted_model, components)
                row_fluxes = labelled_model_row_fluxes(
                    fitted_model=fitted_model,
                    row_labels=row_labels,
                )
                grouped_fluxes = grouped_model_fluxes(
                    fitted_model=fitted_model,
                    groups=component_groups,
                )
                output_fluxes = {**row_fluxes, **grouped_fluxes}

                component_stdevs = stdevs_by_fit[fit_index]
                if len(component_stdevs) != len(row_labels):
                    raise ValueError(
                        f"Formal-error count {len(component_stdevs)} does not match "
                        f"component count {len(row_labels)} for fit {fit_index}."
                    )
                row_errors = {
                    error_label(label): component_stdevs[index]
                    for index, label in enumerate(row_labels)
                }
                grouped_errors = {
                    error_label(f"{safe_label(group_name)}_jy"): math.sqrt(
                        sum(component_stdevs[index] ** 2 for index in indices)
                    )
                    for group_name, indices in component_groups
                }
                output_errors = {**row_errors, **grouped_errors}

                # Explicit integrity check: each grouped total must equal the
                # sum of its named subcomponents to floating-point precision.
                for group_name, indices in component_groups:
                    group_label = f"{safe_label(group_name)}_jy"
                    expected = sum(
                        row_fluxes[row_labels[index]] for index in indices
                    )
                    if not math.isclose(
                        output_fluxes[group_label], expected,
                        rel_tol=1e-12, abs_tol=1e-15,
                    ):
                        raise ValueError(
                            f"Grouped flux integrity failure for {group_name}."
                        )
                status = "ok"
            except Exception as exc:
                print(
                    f"WARNING: {mode} {fit_index} model parse failed: {exc}",
                    file=sys.stderr,
                )
                output_fluxes = {
                    label: math.nan for label in output_labels
                }
                output_errors = {
                    label: math.nan for label in error_labels
                }
                status = "model_parse_failed"

            rows.append(
                {
                    **row_base,
                    **{
                        label: (
                            ""
                            if math.isnan(output_fluxes[label])
                            else f"{output_fluxes[label]:.12g}"
                        )
                        for label in output_labels
                    },
                    **{
                        label: (
                            ""
                            if math.isnan(output_errors[label])
                            else f"{output_errors[label]:.12g}"
                        )
                        for label in error_labels
                    },
                    "rms_jy_per_beam": f"{rms_by_fit[fit_index]:.12g}",
                    "fit_status": status,
                }
            )

        write_spectrum(dataset.spectrum_csv, rows, output_labels, error_labels)

        if PLOT_SPECTRUM:
            plotting_jobs = [
                (
                    "spectrum",
                    plot_spectrum,
                    {
                        "csv_path": dataset.spectrum_csv,
                        "plot_path": dataset.plot_file,
                        "output_labels": output_labels,
                        "source": dataset.source,
                        "epoch": dataset.epoch,
                    },
                ),
                (
                    "grouped spectrum",
                    plot_grouped_spectrum,
                    {
                        "csv_path": dataset.spectrum_csv,
                        "plot_path": dataset.grouped_plot_file,
                        "grouped_labels": grouped_labels,
                        "source": dataset.source,
                        "epoch": dataset.epoch,
                    },
                ),
            ]
            if len(grouped_labels) > 1:
                plotting_jobs.append(
                    (
                        "flux ratios",
                        plot_flux_ratios,
                        {
                            "csv_path": dataset.spectrum_csv,
                            "plot_path": dataset.ratio_plot_file,
                            "grouped_labels": grouped_labels,
                            "source": dataset.source,
                            "epoch": dataset.epoch,
                        },
                    )
                )

            for plot_name, plot_function, plot_kwargs in plotting_jobs:
                try:
                    plot_function(**plot_kwargs)
                except Exception as exc:
                    print(
                        f"WARNING: could not create {plot_name} plot: {exc}",
                        file=sys.stderr,
                    )

        if keep_channel_models:
            model_directory = (
                dataset.spectrum_csv.parent / f"models_{mode}"
            )
            retained_models_directory = model_directory
            if model_directory.exists() and overwrite:
                shutil.rmtree(model_directory)
            model_directory.mkdir(parents=True, exist_ok=True)

            for fit_index, _, _ in fit_ranges:
                source_model = fit_model_path(
                    work_directory,
                    mode,
                    fit_index,
                )
                if source_model.exists():
                    shutil.copy2(source_model, model_directory / source_model.name)

    write_stage2_metadata(
        dataset=dataset,
        mode=mode,
        channels_per_if=channels_per_if,
        exclude_edge_channels=exclude_edge_channels,
        total_channels=total_channels,
        fit_count=len(fit_ranges),
        observation_mjd=observation_mjd,
        component_groups=component_groups,
        components=components,
        stage1_metadata=stage1_metadata,
    )
    write_stage2_manifest(dataset, retained_models_directory)

    successful_rows = sum(row["fit_status"] == "ok" for row in rows)

    print("Completed Stage 2.")
    print(f"Spectrum CSV:    {dataset.spectrum_csv.name}")
    if dataset.plot_file.is_file():
        print(f"Spectrum plot:   {dataset.plot_file.name}")
    if dataset.grouped_plot_file.is_file():
        print(f"Grouped plot:    {dataset.grouped_plot_file.name}")
    if dataset.ratio_plot_file.is_file():
        print(f"Ratio plot:      {dataset.ratio_plot_file.name}")
    print(f"DifMAP log:      {dataset.log_file.name}")
    print(f"Metadata:        {dataset.metadata_file.name}")
    print(f"Manifest:        {dataset.manifest_file.name}")
    print(f"Successful fits: {successful_rows}/{len(rows)}")

    return successful_rows == len(rows)


def discover_datasets(
    stage1_directory: Path,
    pattern: str,
    epochs: set[str],
) -> list[Path]:
    datasets: list[Path] = []
    for path in sorted(stage1_directory.glob(f"*/{pattern}")):
        if not path.is_file():
            continue
        try:
            _, epoch = parse_calibrated_name(path)
        except ValueError:
            continue
        if epochs and epoch not in epochs:
            continue
        datasets.append(path)
    return datasets


EXAMPLES = """Examples
--------
Run every Stage 1 epoch in IF mode using all channels by default:
  python run_difmap_stage2_if_edge_test.py /data/MG0414

Inspect the generated IF selections:
  python run_difmap_stage2_if_edge_test.py /data/MG0414 --epoch A --dry-run

Run one epoch:
  python run_difmap_stage2.py /data/MG0414 --epoch A

Fit individual channels:
  python run_difmap_stage2.py /data/MG0414 --mode channel

Fit selected channels:
  python run_difmap_stage2.py /data/MG0414 --mode channel --channels 1-10,15

Retain fitted models:
  python run_difmap_stage2.py /data/MG0414 --keep-channel-models

Recover products from an existing standard Stage 2 DifMAP log without rerunning fits:
  python run_difmap_stage2.py /data/MG0414 --epoch A --mode channel --recover-from-log --overwrite

Overwrite existing products:
  python run_difmap_stage2.py /data/MG0414 --overwrite
"""


def describe_configuration(args: argparse.Namespace) -> None:
    print(f"Stage 2 Pipeline (version {STAGE2_VERSION})")
    print()
    print("Extraction")
    print("----------")
    print(f"Mode:                  {args.mode}")
    if args.mode == "if":
        print(f"Channels per IF:       {args.channels_per_if}")
        print(f"Excluded per edge:     {args.exclude_edge_channels}")
        print(f"Channels fitted/IF:    {args.channels_per_if - 2 * args.exclude_edge_channels}")
    else:
        print("Channel selection:     individual channels")
        print(f"Requested channels:    {args.channels or 'all'}")
    print()
    print(f"Recovery from log:     {args.recover_from_log}")
    print()
    print("Outputs")
    print("-------")
    print("Per-fit CSV measurements, plots, metadata")

def product_tag(
    mode: str,
    channels_per_if: int,
    exclude_edge_channels: int,
    channel_spec: str | None,
) -> str:
    """Return a filename-safe extraction configuration label."""
    if mode == "channel":
        if channel_spec is None:
            return "channel"
        selection = re.sub(r"[^0-9,-]+", "", channel_spec).replace(",", "_")
        return f"channel_{selection or 'selected'}"
    return f"if{channels_per_if}_edge{exclude_edge_channels}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Stage 2 spectral extraction for a standard project directory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Use --examples for complete command examples.",
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        type=Path,
        default=Path("."),
        help="Project root containing stage1/. Default: current directory.",
    )
    parser.add_argument(
        "--epoch",
        action="append",
        default=[],
        help="Process only this epoch. Repeat to select multiple epochs.",
    )
    parser.add_argument(
        "--pattern",
        default="*.cal.uvf",
        help="Calibrated-data pattern within each Stage 1 epoch directory.",
    )
    parser.add_argument(
        "--mode", choices=("channel", "if"), default=DEFAULT_MODE,
        help=f"Spectral extraction mode. Default: '{DEFAULT_MODE}'.",
    )
    parser.add_argument(
        "--channels-per-if", type=int, default=CHANNELS_PER_IF,
        help=f"Channels per IF block. Default: {CHANNELS_PER_IF}.",
    )
    parser.add_argument(
        "--exclude-edge-channels",
        type=int,
        default=0,
        help=(
            "Channels excluded from each side of every IF in IF mode. "
            "Default: 0."
        ),
    )
    parser.add_argument(
        "--channels", default=None,
        help="One-based channel selection in channel mode, e.g. '1-10,15'.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {STAGE2_VERSION}",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="Print the configured Stage 2 workflow and exit.",
    )
    parser.add_argument("--difmap", default="difmap", help="DifMAP executable.")
    parser.add_argument(
        "--recover-from-log",
        action="store_true",
        help=(
            "Rebuild Stage 2 CSV/plots/metadata/manifest from the existing standard "
            "Stage 2 DifMAP log for each selected dataset, without running DifMAP."
        ),
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing products.")
    parser.add_argument(
        "--keep-channel-models", action="store_true",
        help="Retain every fitted model in the epoch's models/ directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running DifMAP.")
    parser.add_argument("--examples", action="store_true", help="Print usage examples and exit.")
    args = parser.parse_args()

    if args.examples:
        print(EXAMPLES)
        return 0
    if args.describe:
        describe_configuration(args)
        return 0

    project_root = args.project_root.expanduser().resolve()
    stage1_directory = project_root / "stage1"
    inputs_directory = project_root / "inputs"
    if not stage1_directory.is_dir():
        print(f"ERROR: Stage 1 directory not found: {stage1_directory}", file=sys.stderr)
        return 1
    if not inputs_directory.is_dir():
        print(f"ERROR: inputs directory not found: {inputs_directory}", file=sys.stderr)
        return 1

    if not args.dry_run and not args.recover_from_log and shutil.which(args.difmap) is None:
        explicit_path = Path(args.difmap).expanduser()
        if not explicit_path.is_file():
            print(f"ERROR: DifMAP executable not found: {args.difmap}", file=sys.stderr)
            return 1

    epochs = {str(epoch) for epoch in args.epoch}
    calibrated_files = discover_datasets(stage1_directory, args.pattern, epochs)
    if not calibrated_files:
        selection = f" for epoch(s) {sorted(epochs)}" if epochs else ""
        print(
            f"ERROR: no calibrated datasets found{selection} under {stage1_directory}",
            file=sys.stderr,
        )
        return 1

    print(f"Project: {project_root}")
    print(f"Found {len(calibrated_files)} calibrated dataset(s).")

    successful = 0
    failed_or_skipped = 0
    for calibrated_uvfits in calibrated_files:
        try:
            dataset = make_dataset(
                project_root,
                calibrated_uvfits,
                product_tag(
                    args.mode,
                    args.channels_per_if,
                    args.exclude_edge_channels,
                    args.channels,
                ),
            )
        except ValueError as exc:
            print(f"\nSKIPPED: {calibrated_uvfits.name}: {exc}", file=sys.stderr)
            failed_or_skipped += 1
            continue

        try:
            ok = run_dataset(
                dataset=dataset,
                difmap_executable=args.difmap,
                mode=args.mode,
                channels_per_if=args.channels_per_if,
                exclude_edge_channels=args.exclude_edge_channels,
                channel_spec=args.channels,
                overwrite=args.overwrite,
                keep_channel_models=args.keep_channel_models,
                dry_run=args.dry_run,
                recover_from_log=args.recover_from_log,
            )
        except Exception as exc:
            print(
                f"ERROR: unexpected failure for {calibrated_uvfits.name}: {exc}",
                file=sys.stderr,
            )
            ok = False
        if ok:
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
