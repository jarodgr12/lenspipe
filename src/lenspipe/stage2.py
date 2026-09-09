"""Stage 2: fit the frozen Stage 1 model per channel or per IF and write spectra.

Each fit is independent, so the requested fit ranges are split into contiguous
shards and each shard is run in its own DifMAP process. Every DifMAP script
prints a ``STAGE2_RMS <fit_index> <rms>`` line after each fit, and the shard
logs are concatenated into the single log the legacy pipeline produced, so
``--recover-from-log`` keeps working and Stage 3 sees no difference.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lenspipe import __version__
from lenspipe.config import LenspipeConfig, Stage2Config
from lenspipe.difmap import (
    STAGE2_RMS_MARKER,
    difmap_version,
    modelfit_flux_measurements_by_fit,
    run_difmap,
    tagged_values,
)
from lenspipe.difmap.runner import DifmapNotFound
from lenspipe.difmap.scripts import fit_model_path, stage2_commands
from lenspipe.models import (
    ModelFormatError,
    ModelHierarchy,
    label_fitted_model,
    model_component_fluxes,
    safe_label,
    write_flux_only_model,
)
from lenspipe.progress import Reporter
from lenspipe.project import Layout, decide_shards, parse_epoch_filename, utc_now, write_json
from lenspipe.provenance import fingerprint
from lenspipe.stage2_shards import (
    ShardPlan,
    completed_shards,
    mark_done,
    plan_fingerprint,
    shard_log_path,
    work_directory_for,
)
from lenspipe.uvfits import (
    get_observation_mjd,
    get_uvfits_frequencies,
    representative_channel_width,
)

__all__ = [
    "STAGE2_MODEL_SUFFIX",
    "Stage2Paths",
    "Stage2Result",
    "build_rows",
    "discover_inputs",
    "error_label",
    "load_hierarchy",
    "make_fit_ranges",
    "make_paths",
    "parse_channel_spec",
    "plan_shards",
    "product_tag",
    "run_epoch",
    "run_stage2",
    "spectrum_fieldnames",
    "write_spectrum",
]

STAGE_NUMBER = 2
STAGE2_MODEL_SUFFIX = "stage2.mod"
SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class Stage2Paths:
    project_root: Path
    calibrated_uvfits: Path
    source: str
    epoch: str
    product_tag: str
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

    @property
    def prefix(self) -> str:
        return f"{self.source}.{self.epoch}"

    @property
    def product_prefix(self) -> str:
        return f"{self.prefix}.{self.product_tag}"

    @property
    def output_directory(self) -> Path:
        return self.spectrum_csv.parent

    def products(self) -> tuple[Path, ...]:
        return (
            self.spectrum_csv,
            self.plot_file,
            self.grouped_plot_file,
            self.ratio_plot_file,
            self.log_file,
            self.metadata_file,
            self.manifest_file,
        )


@dataclass
class Stage2Result:
    paths: Stage2Paths
    ok: bool
    status: str  # completed | recovered | skipped | dry_run | failed
    message: str = ""
    n_fits: int = 0
    n_ok: int = 0
    commands: str | None = None


def product_tag(config: Stage2Config) -> str:
    """Filename-safe label for the extraction configuration."""
    if config.mode == "channel":
        if config.channels is None:
            return "channel"
        selection = re.sub(r"[^0-9,-]+", "", config.channels).replace(",", "_")
        return f"channel_{selection or 'selected'}"
    return f"if{config.channels_per_if}_edge{config.exclude_edge_channels}"


def make_paths(project_root: Path, calibrated_uvfits: Path, tag: str) -> Stage2Paths:
    source, epoch = parse_epoch_filename(calibrated_uvfits, ".cal.uvf")
    prefix = f"{source}.{epoch}"
    product_prefix = f"{prefix}.{tag}"
    layout = Layout.at(project_root)
    stage1_directory = calibrated_uvfits.parent
    out = (layout.stage2 / prefix).resolve()
    return Stage2Paths(
        project_root=layout.root,
        calibrated_uvfits=calibrated_uvfits.resolve(),
        source=source,
        epoch=epoch,
        product_tag=tag,
        source_model=(layout.inputs / f"{source}.gmod").resolve(),
        stage1_model=(stage1_directory / f"{prefix}.gmod").resolve(),
        stage1_metadata=(stage1_directory / f"{prefix}.model.json").resolve(),
        stage2_model=out / f"{prefix}.{STAGE2_MODEL_SUFFIX}",
        spectrum_csv=out / f"{product_prefix}.spectrum.csv",
        plot_file=out / f"{product_prefix}.spectrum.png",
        grouped_plot_file=out / f"{product_prefix}.grouped_spectrum.png",
        ratio_plot_file=out / f"{product_prefix}.flux_ratios.png",
        log_file=out / f"{product_prefix}.stage2.difmap.log",
        metadata_file=out / f"{product_prefix}.stage2.metadata.json",
        manifest_file=out / f"{product_prefix}.stage2.manifest.json",
    )


def discover_inputs(project_root: Path, pattern: str, epochs: set[str] | None) -> list[Path]:
    layout = Layout.at(project_root)
    datasets: list[Path] = []
    for path in sorted(layout.stage1.glob(f"*/{pattern}")):
        if not path.is_file():
            continue
        try:
            _, epoch = parse_epoch_filename(path, ".cal.uvf")
        except ValueError:
            continue
        if epochs and epoch not in epochs:
            continue
        datasets.append(path)
    return datasets


# ----------------------------------------------------------------------------
# Fit ranges


def parse_channel_spec(spec: str | None, total_channels: int) -> list[int]:
    """Parse one-based selections such as ``1-10,15,20-25``."""
    if spec is None:
        return list(range(1, total_channels + 1))
    selected: set[int] = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"Invalid descending channel range: {item}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(item))
    if not selected:
        raise ValueError("No channels were selected.")
    invalid = sorted(c for c in selected if c < 1 or c > total_channels)
    if invalid:
        raise ValueError(f"Channel(s) outside valid range 1-{total_channels}: {invalid}")
    return sorted(selected)


def make_fit_ranges(
    total_channels: int,
    mode: str,
    channels_per_if: int,
    channel_spec: str | None,
    exclude_edge_channels: int = 0,
) -> list[tuple[int, int, int]]:
    """Return ``(fit_index, first_channel, last_channel)`` tuples."""
    if mode == "channel":
        return [(c, c, c) for c in parse_channel_spec(channel_spec, total_channels)]
    if channel_spec is not None:
        raise ValueError("channels is only valid with mode 'channel'.")
    if channels_per_if < 1:
        raise ValueError("channels_per_if must be at least 1.")
    if exclude_edge_channels < 0:
        raise ValueError("exclude_edge_channels cannot be negative.")
    if 2 * exclude_edge_channels >= channels_per_if:
        raise ValueError("Twice exclude_edge_channels must be smaller than channels_per_if.")

    ranges: list[tuple[int, int, int]] = []
    if_number = 1
    for if_first in range(1, total_channels + 1, channels_per_if):
        if_last = min(if_first + channels_per_if - 1, total_channels)
        if if_last - if_first + 1 != channels_per_if:
            raise ValueError(
                f"Final IF contains {if_last - if_first + 1} channels rather than "
                f"{channels_per_if}; refusing to apply symmetric edge exclusion."
            )
        ranges.append((if_number, if_first + exclude_edge_channels, if_last - exclude_edge_channels))
        if_number += 1
    return ranges


def plan_shards(
    fit_ranges: list[tuple[int, int, int]], shards: int
) -> list[list[tuple[int, int, int]]]:
    """Split fit ranges into ``shards`` contiguous, near-equal blocks."""
    shards = max(1, min(shards, len(fit_ranges)))
    size, remainder = divmod(len(fit_ranges), shards)
    blocks: list[list[tuple[int, int, int]]] = []
    start = 0
    for index in range(shards):
        stop = start + size + (1 if index < remainder else 0)
        blocks.append(fit_ranges[start:stop])
        start = stop
    return [block for block in blocks if block]


# ----------------------------------------------------------------------------
# Hierarchy and labels


def error_label(flux_label: str) -> str:
    if not flux_label.endswith("_jy"):
        raise ValueError(f"Flux label does not end in '_jy': {flux_label}")
    return flux_label[:-3] + "_error_jy"


def load_hierarchy(paths: Stage2Paths) -> tuple[ModelHierarchy, dict[str, Any]]:
    """Load and validate Stage 1 hierarchy metadata against the Stage 1 model."""
    import json

    if not paths.stage1_metadata.is_file():
        raise FileNotFoundError(f"Stage 1 model metadata not found: {paths.stage1_metadata}")
    payload = json.loads(paths.stage1_metadata.read_text(encoding="utf-8"))
    if payload.get("source") != paths.source or payload.get("epoch") != paths.epoch:
        raise ValueError("Stage 1 metadata source/epoch does not match the calibrated dataset.")
    raw_components = payload.get("components")
    raw_groups = payload.get("groups")
    if not isinstance(raw_components, list) or not isinstance(raw_groups, dict):
        raise ValueError("Stage 1 metadata lacks valid 'components' or 'groups'.")

    hierarchy = ModelHierarchy.from_records(raw_components)
    name_to_index = {name: i for i, name in enumerate(hierarchy.component_names)}
    for group_name, names in raw_groups.items():
        if not isinstance(names, list) or not names:
            raise ValueError(f"Group {group_name!r} has no components.")
        missing = [n for n in names if n not in name_to_index]
        if missing:
            raise ValueError(f"Group {group_name!r} references unknown components: {missing}")

    _validate_labels(hierarchy)
    model_rows = model_component_fluxes(paths.stage1_model)
    if len(model_rows) != len(hierarchy.components):
        raise ValueError(
            f"Stage 1 model has {len(model_rows)} rows but metadata defines "
            f"{len(hierarchy.components)} components."
        )
    return hierarchy, payload


def _validate_labels(hierarchy: ModelHierarchy) -> None:
    labels = [f"{safe_label(n)}_jy" for n in hierarchy.component_names]
    labels.extend(f"{safe_label(g)}_jy" for g, _ in hierarchy.group_indices)
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(
            "Component/group names collapse to duplicate CSV labels: " + ", ".join(duplicates)
        )


def _labels(hierarchy: ModelHierarchy) -> tuple[list[str], list[str]]:
    row_labels = [f"{safe_label(n)}_jy" for n in hierarchy.component_names]
    grouped_labels = [f"{safe_label(g)}_jy" for g, _ in hierarchy.group_indices]
    return row_labels, grouped_labels


# ----------------------------------------------------------------------------
# Rows and CSV


def spectrum_fieldnames(output_labels: list[str], error_labels: list[str]) -> list[str]:
    return [
        "source", "epoch", "mjd", "mode", "fit_index", "first_channel", "last_channel",
        "channels_averaged", "frequency_hz", "frequency_ghz", "bandwidth_hz",
        *output_labels, *error_labels, "rms_jy_per_beam", "fit_status",
    ]


def write_spectrum(
    output_path: Path, rows: list[dict[str, object]], output_labels: list[str], error_labels: list[str]
) -> None:
    import csv

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=spectrum_fieldnames(output_labels, error_labels))
        writer.writeheader()
        writer.writerows(rows)


def build_rows(
    *,
    paths: Stage2Paths,
    mode: str,
    fit_ranges: list[tuple[int, int, int]],
    frequencies: list[float],
    channel_width_hz: float | None,
    observation_mjd: float | None,
    hierarchy: ModelHierarchy,
    rms_by_fit: dict[int, float],
    measurements_by_fit: dict[int, list[tuple[float, float]]],
    fluxes_by_fit: dict[int, list[float] | str] | None,
) -> list[dict[str, object]]:
    """Assemble spectrum rows.

    ``fluxes_by_fit`` maps a fit index to the fluxes read from its fitted model
    file, or to a status string (``missing_model`` / ``model_parse_failed``).
    When it is ``None`` the fluxes are taken from the parsed DifMAP tables
    instead, which is the log-recovery behaviour.
    """
    row_labels, grouped_labels = _labels(hierarchy)
    output_labels = [*row_labels, *grouped_labels]
    error_labels = [error_label(label) for label in output_labels]
    group_indices = hierarchy.group_indices

    rows: list[dict[str, object]] = []
    for fit_index, first_channel, last_channel in fit_ranges:
        selected = frequencies[first_channel - 1 : last_channel]
        frequency_hz = sum(selected) / len(selected)
        n_averaged = last_channel - first_channel + 1
        base: dict[str, object] = {
            "source": paths.source,
            "epoch": paths.epoch,
            "mjd": "" if observation_mjd is None else f"{observation_mjd:.9f}",
            "mode": mode,
            "fit_index": fit_index,
            "first_channel": first_channel,
            "last_channel": last_channel,
            "channels_averaged": n_averaged,
            "frequency_hz": f"{frequency_hz:.9f}",
            "frequency_ghz": f"{frequency_hz / 1e9:.12f}",
            "bandwidth_hz": (
                "" if channel_width_hz is None else f"{channel_width_hz * n_averaged:.9f}"
            ),
        }
        rms_text = f"{rms_by_fit[fit_index]:.12g}"

        if fluxes_by_fit is not None and isinstance(fluxes_by_fit.get(fit_index), str):
            rows.append(
                {
                    **base,
                    **{label: "" for label in output_labels},
                    **{label: "" for label in error_labels},
                    "rms_jy_per_beam": rms_text,
                    "fit_status": str(fluxes_by_fit[fit_index]),
                }
            )
            continue

        measurements = measurements_by_fit[fit_index]
        if len(measurements) != len(row_labels):
            raise ValueError(
                f"fit {fit_index} has {len(measurements)} component rows; "
                f"hierarchy defines {len(row_labels)}."
            )
        stdevs = [stdev for _, stdev in measurements]
        if fluxes_by_fit is None:
            component_fluxes = [flux for flux, _ in measurements]
        else:
            component_fluxes = list(fluxes_by_fit[fit_index])  # type: ignore[arg-type]
            if len(component_fluxes) != len(row_labels):
                raise ValueError(
                    f"fit {fit_index}: fitted model has {len(component_fluxes)} rows, "
                    f"hierarchy defines {len(row_labels)}."
                )

        fluxes = {label: component_fluxes[i] for i, label in enumerate(row_labels)}
        fluxes.update(
            {
                f"{safe_label(group)}_jy": sum(component_fluxes[i] for i in indices)
                for group, indices in group_indices
            }
        )
        errors = {error_label(label): stdevs[i] for i, label in enumerate(row_labels)}
        errors.update(
            {
                error_label(f"{safe_label(group)}_jy"): math.sqrt(
                    sum(stdevs[i] ** 2 for i in indices)
                )
                for group, indices in group_indices
            }
        )
        rows.append(
            {
                **base,
                **{label: f"{fluxes[label]:.12g}" for label in output_labels},
                **{label: f"{errors[label]:.12g}" for label in error_labels},
                "rms_jy_per_beam": rms_text,
                "fit_status": "ok",
            }
        )
    return rows


# ----------------------------------------------------------------------------
# Metadata


def _write_metadata(
    paths: Stage2Paths,
    config: LenspipeConfig,
    *,
    mode: str,
    total_channels: int,
    fit_count: int,
    observation_mjd: float | None,
    hierarchy: ModelHierarchy,
    stage1_metadata: dict[str, Any],
    shards: int,
    difmap_info: dict[str, str | None] | None,
    command_sha256: str | None,
    duration_s: float | None,
    recovered: bool,
    shard_decision: dict[str, Any] | None = None,
    resumed: bool = False,
) -> None:
    stage2 = config.stage2
    provenance = {
        "calibrated_uvfits": fingerprint(paths.calibrated_uvfits),
        "stage1_model": fingerprint(paths.stage1_model),
        "stage1_metadata": fingerprint(paths.stage1_metadata),
    }
    write_json(
        paths.metadata_file,
        {
            "schema_version": SCHEMA_VERSION,
            "stage": STAGE_NUMBER,
            "stage2_version": __version__,
            "lenspipe_version": __version__,
            "source": paths.source,
            "epoch": paths.epoch,
            "mjd": observation_mjd,
            "mode": mode,
            "channels_per_if": stage2.channels_per_if if mode == "if" else 1,
            "excluded_edge_channels_per_side": stage2.exclude_edge_channels if mode == "if" else 0,
            "fitted_channels_per_if": (
                stage2.channels_per_if - 2 * stage2.exclude_edge_channels if mode == "if" else 1
            ),
            "total_channels": total_channels,
            "n_fits": fit_count,
            "stage1_model": paths.stage1_model.name,
            "stage1_metadata": paths.stage1_metadata.name,
            "stage1_version": stage1_metadata.get("stage1_version"),
            "stage2_model": paths.stage2_model.name,
            "n_groups": len(hierarchy.groups),
            "n_components": len(hierarchy.components),
            "groups": hierarchy.groups,
            "components": hierarchy.to_records(),
            "uncertainty_model": {
                "component_flux_errors": "DifMAP modelfit Flux Stdev parsed from stdout",
                "grouped_flux_errors": (
                    "quadrature sum of member-component formal errors; covariance unavailable"
                ),
                "rms_jy_per_beam": "residual-image RMS retained as a channel-quality diagnostic",
            },
            "execution": {
                "recovered_from_log": recovered,
                "shards": shards,
                "difmap": difmap_info,
                "command_script_sha256": command_sha256,
                "input_sha256": provenance["calibrated_uvfits"]["sha256"],
                "duration_s": None if duration_s is None else round(duration_s, 3),
                "shard_decision": shard_decision,
                "resumed": resumed,
            },
            "provenance": provenance,
            "config": stage2.model_dump(mode="json"),
            "created_utc": utc_now(),
        },
    )


def _write_manifest(paths: Stage2Paths, retained_models_directory: Path | None) -> None:
    write_json(
        paths.manifest_file,
        {
            "schema_version": "1.0",
            "stage": STAGE_NUMBER,
            "source": paths.source,
            "epoch": paths.epoch,
            "metadata": paths.metadata_file.name,
            "spectrum": paths.spectrum_csv.name,
            "plot": paths.plot_file.name if paths.plot_file.is_file() else None,
            "grouped_spectrum_plot": (
                paths.grouped_plot_file.name if paths.grouped_plot_file.is_file() else None
            ),
            "flux_ratio_plot": paths.ratio_plot_file.name if paths.ratio_plot_file.is_file() else None,
            "log": paths.log_file.name,
            "stage2_model": paths.stage2_model.name,
            "stage1_model_metadata": paths.stage1_metadata.name,
            "fitted_models_directory": (
                retained_models_directory.name if retained_models_directory is not None else None
            ),
        },
    )


# ----------------------------------------------------------------------------
# Running one epoch


@dataclass
class _Prepared:
    hierarchy: ModelHierarchy
    stage1_metadata: dict[str, Any]
    frequencies: list[float]
    channel_width_hz: float | None
    observation_mjd: float | None
    fit_ranges: list[tuple[int, int, int]]
    row_labels: list[str]
    grouped_labels: list[str]

    @property
    def output_labels(self) -> list[str]:
        return [*self.row_labels, *self.grouped_labels]

    @property
    def error_labels(self) -> list[str]:
        return [error_label(label) for label in self.output_labels]


def _prepare(paths: Stage2Paths, config: Stage2Config, reporter: Reporter) -> _Prepared:
    if not paths.calibrated_uvfits.is_file():
        raise FileNotFoundError(f"calibrated UV data not found: {paths.calibrated_uvfits}")
    if not paths.stage1_model.is_file():
        raise FileNotFoundError(f"Stage 1 model not found: {paths.stage1_model}")
    hierarchy, stage1_metadata = load_hierarchy(paths)
    frequencies = get_uvfits_frequencies(paths.calibrated_uvfits)
    try:
        observation_mjd = get_observation_mjd(paths.calibrated_uvfits)
    except Exception as exc:  # noqa: BLE001 - header quirks should not stop the fit
        reporter.warn(f"could not determine observation MJD: {exc}")
        observation_mjd = None
    fit_ranges = make_fit_ranges(
        total_channels=len(frequencies),
        mode=config.mode,
        channels_per_if=config.channels_per_if,
        channel_spec=config.channels,
        exclude_edge_channels=config.exclude_edge_channels if config.mode == "if" else 0,
    )
    row_labels, grouped_labels = _labels(hierarchy)
    return _Prepared(
        hierarchy=hierarchy,
        stage1_metadata=stage1_metadata,
        frequencies=frequencies,
        channel_width_hz=representative_channel_width(frequencies),
        observation_mjd=observation_mjd,
        fit_ranges=fit_ranges,
        row_labels=row_labels,
        grouped_labels=grouped_labels,
    )


def _check_indices(
    expected: set[int], found: set[int], what: str
) -> str | None:
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    if not missing and not extra:
        return None
    parts = [f"{what} do not match the requested fits (requested {len(expected)}, found {len(found)})."]
    if missing:
        parts.append(f"missing: {missing[:20]}{' ...' if len(missing) > 20 else ''}")
    if extra:
        parts.append(f"unexpected: {extra[:20]}{' ...' if len(extra) > 20 else ''}")
    return " ".join(parts)


def _remove_products(paths: Stage2Paths, mode: str, include_log: bool) -> None:
    for path in paths.products():
        if path == paths.log_file and not include_log:
            continue
        if path.exists():
            path.unlink()
    stale = paths.output_directory / f"models_{mode}"
    if include_log and stale.exists():
        shutil.rmtree(stale)


def run_epoch(
    paths: Stage2Paths,
    config: LenspipeConfig,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    recover_from_log: bool = False,
    resume: bool = False,
    epoch_workers: int = 1,
    reporter: Reporter | None = None,
    cancel_event: threading.Event | None = None,
) -> Stage2Result:
    reporter = reporter or Reporter()
    stage2 = config.stage2
    mode = stage2.mode
    label = f"stage2 {paths.product_prefix}"

    try:
        prepared = _prepare(paths, stage2, reporter)
    except (OSError, ValueError, ModelFormatError) as exc:
        return Stage2Result(paths, False, "failed", str(exc))

    n_fits = len(prepared.fit_ranges)
    decision = decide_shards(
        stage2.shards,
        n_fits,
        input_bytes=paths.calibrated_uvfits.stat().st_size,
        epoch_workers=max(1, epoch_workers),
        memory_fraction=stage2.memory_fraction,
        memory_multiple=stage2.memory_multiple,
    )
    if (
        isinstance(stage2.shards, int)
        and decision.memory_cap is not None
        and stage2.shards > decision.memory_cap
    ):
        reporter.warn(
            f"[{label}] {stage2.shards} shards requested but the memory budget suggests at most "
            f"{decision.memory_cap}; proceeding as requested."
        )
    shards = decision.shards
    reporter.log(
        f"[{label}] {len(prepared.frequencies)} channels, mode={mode}, fits={n_fits}, "
        f"{decision.describe()}"
    )

    if dry_run:
        commands = stage2_commands(
            calibrated_uvfits=paths.calibrated_uvfits,
            stage2_model=paths.stage2_model,
            fit_ranges=prepared.fit_ranges,
            work_directory=paths.output_directory / "_dry_run_models",
            mode=mode,
            config=stage2,
        )
        return Stage2Result(paths, True, "dry_run", "commands generated", n_fits=n_fits, commands=commands)

    work_dir = work_directory_for(paths.output_directory, paths.product_prefix)
    if overwrite:
        _remove_products(paths, mode, include_log=not recover_from_log)
        if work_dir.exists():
            shutil.rmtree(work_dir)
    elif paths.spectrum_csv.exists():
        return Stage2Result(
            paths, False, "skipped", f"output exists: {paths.spectrum_csv.name}; use --overwrite."
        )
    elif work_dir.exists() and not resume and not recover_from_log:
        return Stage2Result(
            paths, False, "failed",
            f"an interrupted run was found in {work_dir.name}; use --resume to continue it "
            "or --overwrite to start again.",
        )

    try:
        component_lines, modified_lines = write_flux_only_model(paths.stage1_model, paths.stage2_model)
    except (OSError, ValueError) as exc:
        return Stage2Result(paths, False, "failed", f"creating Stage 2 model: {exc}")
    reporter.log(
        f"[{label}] flux-only model: {component_lines} components, {modified_lines} rows normalised"
    )

    if recover_from_log:
        return _recover(paths, config, prepared, overwrite, reporter, label)

    fingerprint = plan_fingerprint(paths.calibrated_uvfits, paths.stage2_model, stage2, prepared.fit_ranges)
    plan: ShardPlan | None = None
    done: dict[int, dict[str, Any]] = {}
    if resume and work_dir.exists():
        plan = ShardPlan.load(work_dir)
        if plan is None:
            return Stage2Result(
                paths, False, "failed",
                f"cannot resume: no shard plan in {work_dir.name}; use --overwrite to start again.",
            )
        if plan.fingerprint != fingerprint:
            return Stage2Result(
                paths, False, "failed",
                "cannot resume: the calibrated data, model or Stage 2 settings changed since the "
                "interrupted run; use --overwrite to start again.",
            )
        done = completed_shards(work_dir, plan)
        reporter.log(f"[{label}] resuming: {len(done)}/{len(plan.blocks)} shard(s) already complete")
    if plan is None:
        paths.output_directory.mkdir(parents=True, exist_ok=True)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        plan = ShardPlan(
            fingerprint=fingerprint,
            mode=mode,
            fit_ranges=list(prepared.fit_ranges),
            blocks=plan_shards(prepared.fit_ranges, shards),
            created_utc=utc_now(),
        )
        plan.save(work_dir)
    blocks = plan.blocks
    todo = [index for index in range(len(blocks)) if index not in done]
    resumed = bool(done)
    difmap_info = difmap_version(config.project.difmap.executable)

    progress_lock = threading.Lock()
    completed = {"n": sum(len(blocks[index]) for index in done)}
    reporter.progress(label, completed["n"], n_fits, f"{len(todo)} DifMAP process(es)")

    def on_line(line: str) -> None:
        if line.lstrip().startswith(STAGE2_RMS_MARKER):
            with progress_lock:
                completed["n"] += 1
                reporter.progress(label, completed["n"], n_fits, "fitting")

    def run_block(index: int):
        block = blocks[index]
        commands = stage2_commands(
            calibrated_uvfits=paths.calibrated_uvfits,
            stage2_model=paths.stage2_model,
            fit_ranges=block,
            work_directory=work_dir,
            mode=mode,
            config=stage2,
        )
        shard_log = shard_log_path(work_dir, index)
        if shard_log.exists():
            shard_log.unlink()
        result = run_difmap(
            config.project.difmap.executable,
            commands,
            shard_log,
            on_line=on_line,
            cancel_event=cancel_event,
            stream=config.project.difmap.stream,
        )
        if result.ok:
            mark_done(
                work_dir,
                index,
                {
                    "duration_s": round(result.duration_s, 3),
                    "n_fits": len(block),
                    "first_fit": block[0][0],
                    "last_fit": block[-1][0],
                    "command_script_sha256": hashlib.sha256(commands.encode("utf-8")).hexdigest(),
                    "finished_utc": utc_now(),
                },
            )
        return index, result

    outcomes: list[tuple[int, Any]] = []
    try:
        if len(todo) == 1:
            outcomes.append(run_block(todo[0]))
        elif todo:
            with ThreadPoolExecutor(max_workers=min(shards, len(todo)), thread_name_prefix="difmap") as pool:
                futures = [pool.submit(run_block, index) for index in todo]
                for future in as_completed(futures):
                    outcomes.append(future.result())
    except (DifmapNotFound, OSError) as exc:
        reporter.clear(label)
        return Stage2Result(paths, False, "failed", f"could not run DifMAP: {exc}")

    failed = sorted((index, result) for index, result in outcomes if not result.ok)
    if failed:
        reporter.clear(label)
        index, result = failed[0]
        reason = "cancelled" if result.cancelled else f"status {result.returncode}"
        kept = len(blocks) - len(failed)
        return Stage2Result(
            paths, False, "failed",
            f"DifMAP shard {index + 1}/{len(blocks)} ended with {reason} "
            f"(see {shard_log_path(work_dir, index)}); {kept} finished shard(s) are kept, "
            "re-run with --resume to continue.",
            n_fits=n_fits,
        )

    done = completed_shards(work_dir, plan)
    combined_lines: list[str] = []
    for index, block in enumerate(blocks):
        if len(blocks) > 1:
            combined_lines.append(
                f"! lenspipe stage2 shard {index + 1}/{len(blocks)}: fits {block[0][0]}-{block[-1][0]}"
            )
        combined_lines.append(shard_log_path(work_dir, index).read_text(encoding="utf-8").rstrip("\n"))
    log_text = "\n".join(combined_lines) + "\n"
    paths.log_file.write_text(log_text, encoding="utf-8")
    duration_s = sum(float(record.get("duration_s", 0.0)) for record in done.values())
    command_sha = hashlib.sha256(
        "".join(str(done[index].get("command_script_sha256", "")) for index in sorted(done)).encode("utf-8")
    ).hexdigest()

    expected = {fit_index for fit_index, _, _ in prepared.fit_ranges}
    try:
        rms_by_fit = tagged_values(log_text, STAGE2_RMS_MARKER)
        measurements = modelfit_flux_measurements_by_fit(
            log_text, expected_component_count=len(prepared.row_labels)
        )
    except ValueError as exc:
        reporter.clear(label)
        return Stage2Result(
            paths, False, "failed",
            f"parsing DifMAP log: {exc}; see {paths.log_file}. DifMAP's output is kept in "
            f"{work_dir.name}; once the parser handles it, re-run with --resume to build the "
            "products without fitting again.",
        )
    for problem in (
        _check_indices(expected, set(rms_by_fit), "tagged RMS measurements"),
        _check_indices(expected, set(measurements), "DifMAP formal-error tables"),
    ):
        if problem:
            reporter.clear(label)
            return Stage2Result(
                paths, False, "failed",
                f"{problem} See {paths.log_file}. DifMAP's output is kept in {work_dir.name}; "
                "re-run with --resume once the cause is fixed.",
            )

    fluxes_by_fit: dict[int, list[float] | str] = {}
    for fit_index, _, _ in prepared.fit_ranges:
        fitted_model = fit_model_path(work_dir, mode, fit_index)
        if not fitted_model.is_file():
            fluxes_by_fit[fit_index] = "missing_model"
            continue
        try:
            label_fitted_model(fitted_model, prepared.hierarchy)
            fluxes = model_component_fluxes(fitted_model)
            if len(fluxes) != len(prepared.row_labels):
                raise ValueError(
                    f"fitted model has {len(fluxes)} rows, hierarchy defines {len(prepared.row_labels)}"
                )
            fluxes_by_fit[fit_index] = fluxes
        except Exception as exc:  # noqa: BLE001 - one bad fit must not sink the epoch
            reporter.warn(f"{mode} {fit_index} model parse failed: {exc}")
            fluxes_by_fit[fit_index] = "model_parse_failed"

    try:
        rows = build_rows(
            paths=paths, mode=mode, fit_ranges=prepared.fit_ranges,
            frequencies=prepared.frequencies, channel_width_hz=prepared.channel_width_hz,
            observation_mjd=prepared.observation_mjd, hierarchy=prepared.hierarchy,
            rms_by_fit=rms_by_fit, measurements_by_fit=measurements, fluxes_by_fit=fluxes_by_fit,
        )
    except ValueError as exc:
        reporter.clear(label)
        return Stage2Result(paths, False, "failed", str(exc))

    write_spectrum(paths.spectrum_csv, rows, prepared.output_labels, prepared.error_labels)
    _quicklook(paths, prepared, reporter, config.stage2)

    retained_models_directory: Path | None = None
    if stage2.keep_models:
        retained_models_directory = paths.output_directory / f"models_{mode}"
        if retained_models_directory.exists():
            shutil.rmtree(retained_models_directory)
        retained_models_directory.mkdir(parents=True, exist_ok=True)
        for fit_index, _, _ in prepared.fit_ranges:
            model = fit_model_path(work_dir, mode, fit_index)
            if model.exists():
                shutil.copy2(model, retained_models_directory / model.name)
        for index in range(len(blocks)):
            shard_log = shard_log_path(work_dir, index)
            if shard_log.exists():
                shutil.copy2(shard_log, retained_models_directory / shard_log.name)

    _write_metadata(
        paths, config, mode=mode, total_channels=len(prepared.frequencies), fit_count=n_fits,
        observation_mjd=prepared.observation_mjd, hierarchy=prepared.hierarchy,
        stage1_metadata=prepared.stage1_metadata, shards=len(blocks), difmap_info=difmap_info,
        command_sha256=command_sha, duration_s=duration_s, recovered=False,
        shard_decision=decision.to_dict(), resumed=resumed,
    )
    _write_manifest(paths, retained_models_directory)
    shutil.rmtree(work_dir, ignore_errors=True)

    n_ok = sum(row["fit_status"] == "ok" for row in rows)
    reporter.progress(label, n_fits, n_fits, "completed")
    return Stage2Result(
        paths, n_ok == len(rows), "completed",
        f"{n_ok}/{len(rows)} fits ok -> {paths.spectrum_csv.name}", n_fits=n_fits, n_ok=n_ok,
    )


def _recover(
    paths: Stage2Paths,
    config: LenspipeConfig,
    prepared: _Prepared,
    overwrite: bool,
    reporter: Reporter,
    label: str,
) -> Stage2Result:
    if not paths.log_file.is_file():
        return Stage2Result(paths, False, "failed", f"recovery log not found: {paths.log_file}")
    if config.stage2.keep_models:
        reporter.warn("keep_models has no effect in log-recovery mode.")
    reporter.log(f"[{label}] recovering from {paths.log_file.name} without running DifMAP")

    log_text = paths.log_file.read_text(encoding="utf-8", errors="replace")
    expected = {fit_index for fit_index, _, _ in prepared.fit_ranges}
    try:
        rms_by_fit = tagged_values(log_text, STAGE2_RMS_MARKER)
        measurements = modelfit_flux_measurements_by_fit(
            log_text, expected_component_count=len(prepared.row_labels)
        )
    except ValueError as exc:
        return Stage2Result(paths, False, "failed", f"parsing recovery log: {exc}")
    for problem in (
        _check_indices(expected, set(rms_by_fit), "recovery-log RMS values"),
        _check_indices(expected, set(measurements), "recovery-log Flux/Stdev tables"),
    ):
        if problem:
            return Stage2Result(paths, False, "failed", problem)

    if overwrite:
        _remove_products(paths, config.stage2.mode, include_log=False)

    try:
        rows = build_rows(
            paths=paths, mode=config.stage2.mode, fit_ranges=prepared.fit_ranges,
            frequencies=prepared.frequencies, channel_width_hz=prepared.channel_width_hz,
            observation_mjd=prepared.observation_mjd, hierarchy=prepared.hierarchy,
            rms_by_fit=rms_by_fit, measurements_by_fit=measurements, fluxes_by_fit=None,
        )
    except ValueError as exc:
        return Stage2Result(paths, False, "failed", str(exc))

    write_spectrum(paths.spectrum_csv, rows, prepared.output_labels, prepared.error_labels)
    _quicklook(paths, prepared, reporter, config.stage2)
    _write_metadata(
        paths, config, mode=config.stage2.mode, total_channels=len(prepared.frequencies),
        fit_count=len(prepared.fit_ranges), observation_mjd=prepared.observation_mjd,
        hierarchy=prepared.hierarchy, stage1_metadata=prepared.stage1_metadata, shards=0,
        difmap_info=None, command_sha256=None, duration_s=None, recovered=True,
    )
    _write_manifest(paths, None)
    return Stage2Result(
        paths, True, "recovered", f"{len(rows)} fits recovered -> {paths.spectrum_csv.name}",
        n_fits=len(rows), n_ok=len(rows),
    )


def _quicklook(
    paths: Stage2Paths, prepared: _Prepared, reporter: Reporter, config: Stage2Config | None = None
) -> None:
    from lenspipe.stage2_quicklook import write_quicklook_plots

    if config is not None and not config.plot_spectrum:
        return
    try:
        write_quicklook_plots(
            error_bars=config.plot_error_bars if config is not None else True,
            csv_path=paths.spectrum_csv,
            spectrum_png=paths.plot_file,
            grouped_png=paths.grouped_plot_file,
            ratio_png=paths.ratio_plot_file,
            output_labels=prepared.output_labels,
            grouped_labels=prepared.grouped_labels,
            source=paths.source,
            epoch=paths.epoch,
            warn=reporter.warn,
        )
    except Exception as exc:  # noqa: BLE001 - plots are advisory
        reporter.warn(f"quick-look plotting failed: {exc}")


# ----------------------------------------------------------------------------
# Running many epochs


def run_stage2(
    project_root: Path,
    config: LenspipeConfig,
    *,
    epochs: set[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    recover_from_log: bool = False,
    resume: bool = False,
    workers: int | None = None,
    reporter: Reporter | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Stage2Result]:
    reporter = reporter or Reporter()
    layout = Layout.at(project_root)
    if not layout.stage1.is_dir():
        raise FileNotFoundError(f"Stage 1 directory not found: {layout.stage1}")
    if not layout.inputs.is_dir():
        raise FileNotFoundError(f"inputs directory not found: {layout.inputs}")

    files = discover_inputs(layout.root, config.stage2.input_pattern, epochs)
    if not files:
        selection = f" for epoch(s) {sorted(epochs)}" if epochs else ""
        raise FileNotFoundError(f"no calibrated datasets found{selection} under {layout.stage1}")

    tag = product_tag(config.stage2)
    datasets: list[Stage2Paths] = []
    for path in files:
        try:
            datasets.append(make_paths(layout.root, path, tag))
        except ValueError as exc:
            reporter.warn(f"skipped {path.name}: {exc}")

    workers = max(1, workers or config.run.epoch_workers)
    concurrent_epochs = 1 if (dry_run or recover_from_log) else min(workers, max(1, len(datasets)))

    def one(paths: Stage2Paths) -> Stage2Result:
        try:
            return run_epoch(
                paths, config, overwrite=overwrite, dry_run=dry_run,
                recover_from_log=recover_from_log, resume=resume, epoch_workers=concurrent_epochs,
                reporter=reporter, cancel_event=cancel_event,
            )
        except Exception as exc:  # noqa: BLE001 - keep other epochs running
            return Stage2Result(paths, False, "failed", f"unexpected failure: {exc!r}")

    results: list[Stage2Result] = []
    if concurrent_epochs == 1:
        for paths in datasets:
            result = one(paths)
            _report(reporter, result)
            results.append(result)
        return results

    with ThreadPoolExecutor(max_workers=concurrent_epochs, thread_name_prefix="stage2") as pool:
        futures = [pool.submit(one, paths) for paths in datasets]
        for future in as_completed(futures):
            result = future.result()
            _report(reporter, result)
            results.append(result)
    results.sort(key=lambda item: (item.paths.source, item.paths.epoch))
    return results


def _report(reporter: Reporter, result: Stage2Result) -> None:
    reporter.log(f"[stage2 {result.paths.product_prefix}] {result.status.upper()}: {result.message}")
    if result.status == "dry_run" and result.commands:
        reporter.log("--- DifMAP commands ---")
        reporter.log(result.commands.rstrip("\n"))
        reporter.log("--- End DifMAP commands ---")
