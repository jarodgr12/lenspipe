#!/usr/bin/env python3
"""
===============================================================================
DifMAP Spectral Pipeline — Stage 3
Multi-epoch spectral fitting and publication-quality science plots
Version: 1.3.37
===============================================================================

Purpose
-------
Read manifest-defined Stage 2 grouped-image spectra, fit each lensed-image
spectrum, write publication-quality plots for every visit, and create fixed
5-row by 4-column summaries of all selected visits.

Stage 2 inputs
--------------
<project>/stage2/<source>.<epoch>/
    <source>.<epoch>.<product>.spectrum.csv
    <source>.<epoch>.<product>.stage2.metadata.json
    <source>.<epoch>.<product>.stage2.manifest.json

Per-visit outputs
-----------------
<project>/stage3/<source>.<epoch>/<product>/
    fits/
    plots/
    <source>.<epoch>.<product>.stage3.metadata.json
    <source>.<epoch>.<product>.stage3.manifest.json

Combined outputs
----------------
<project>/stage3/combined/<source>/<product>/
    plots/<source>.<product>.all_epochs_spectra_4col.pdf
    plots/<source>.<product>.all_epochs_spectra_4col.png
    plots/<source>.<product>.all_epochs_flux_ratios_4col.pdf
    plots/<source>.<product>.all_epochs_flux_ratios_4col.png
    plots/<source>.<product>.reference_fluxes_vs_mjd.pdf
    plots/<source>.<product>.average_spectrum.pdf
    plots/<source>.<product>.average_flux_ratios.pdf
    plots/<source>.<product>.weighted_flux_ratios_vs_mjd.pdf
    plots/<source>.<product>.normalised_weighted_flux_ratios_vs_mjd.pdf
    plots/<source>.<product>.rcusp_vs_mjd.pdf
    plots/<source>.<product>.rms_vs_channel_all_epochs.pdf
    plots/<source>.<product>.median_rms_vs_channel.pdf
    plots/<source>.<product>.relative_rms_vs_channel_all_epochs.pdf
    plots/<source>.<product>.rms_visit_robust_score_vs_channel.pdf
    tables/<source>.<product>.rms_channel_statistics.csv
    tables/<source>.<product>.rms_visit_diagnostics.csv
    tables/<source>.<product>.average_spectrum.csv
    tables/<source>.<product>.average_flux_ratios.csv
    tables/<source>.<product>.reference_fluxes_vs_mjd.csv
    tables/<source>.<product>.normalised_weighted_flux_ratios_vs_mjd.csv
    tables/<source>.<product>.rcusp_vs_mjd.csv
    <source>.<product>.combined.stage3.metadata.json
    <source>.<product>.combined.stage3.manifest.json

The visual design follows the established Stage 3 convention: STIX fonts,
white background, inward major ticks on all four sides, no minor ticks,
coloured measurements, black dashed fitted/mean lines, MJD-only titles, and
fixed 12--18 GHz tick marks on a slightly padded frequency frame.
===============================================================================
"""

from __future__ import annotations

import csv
import json
import math
import multiprocessing
import shutil
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np

from lenspipe import __version__
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.project import Layout
from lenspipe.provenance import fingerprint
from lenspipe.stage3 import plotting
from lenspipe.stage3.combined import (
    calculate_normalised_scatter,
    fit_average_flux_ratios,
    fit_average_spectra,
    fit_constant,
)
from lenspipe.stage3.fitting import (
    calculate_ratio,
    fit_power_law,
    fit_power_law_emcee,
    fit_ratio_power_law,
)
from lenspipe.stage3.io import (
    Stage2Dataset,
    discover_stage2_datasets,
    grouped_flux_columns,
    load_spectrum,
    load_stage2_metadata,
    safe_label,
)
from lenspipe.stage3.plotting import (
    calculate_ratio_y_limits,
    calculate_spectrum_y_limits,
    configure_matplotlib,
    plot_all_epochs_flux_ratios,
    plot_all_epochs_spectra,
    plot_combined_average_flux_ratios,
    plot_combined_average_spectrum,
    plot_epoch_normalized_weighted_flux_ratios_vs_mjd,
    plot_epoch_rcusp_vs_mjd,
    plot_epoch_reference_fluxes_vs_mjd,
    plot_epoch_weighted_flux_ratios_vs_mjd,
    plot_flux_ratios,
    plot_spectra,
    plot_spectra_with_residuals,
)

STAGE3_VERSION = __version__


@dataclass
class EpochAnalysis:
    dataset: Stage2Dataset
    metadata: dict[str, Any]
    mjd: float
    fit_indices: np.ndarray
    frequency_ghz: np.ndarray
    rms_jy_per_beam: np.ndarray
    excluded_fit_indices: tuple[int, ...]
    fluxes: dict[str, np.ndarray]
    error_source: str
    uncertainty_sources: dict[str, str]
    uncertainties: dict[str, np.ndarray]
    spectral_fits: dict[str, Any]
    posteriors: dict[str, dict[str, np.ndarray]]
    ratios: dict[str, np.ndarray]
    ratio_errors: dict[str, np.ndarray]
    ratio_fits: dict[str, Any]
    reference_group: str

    def plot_record(self) -> dict[str, Any]:
        return {
            "mjd": self.mjd,
            "frequency_ghz": self.frequency_ghz,
            "fit_indices": self.fit_indices,
            "rms_jy_per_beam": self.rms_jy_per_beam,
            "fluxes": self.fluxes,
            "uncertainties": self.uncertainties,
            "fits": self.spectral_fits,
            "ratios": self.ratios,
            "ratio_errors": self.ratio_errors,
        }


def _parse_index_spec(spec: str, option_name: str) -> list[int]:
    """Parse a comma-separated list of positive integers and ranges."""
    values: set[int] = set()
    for chunk in str(spec).split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid range in {option_name}: {token!r}"
                ) from exc
            if start <= 0 or end <= 0 or end < start:
                raise ValueError(f"Invalid range in {option_name}: {token!r}")
            values.update(range(start, end + 1))
        else:
            try:
                value = int(token)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid index in {option_name}: {token!r}"
                ) from exc
            if value <= 0:
                raise ValueError(f"Invalid index in {option_name}: {token!r}")
            values.add(value)
    if not values:
        raise ValueError(f"{option_name} did not contain any valid indices.")
    return sorted(values)


def _parse_epoch_exclusion_specs(
    specifications: list[str],
) -> dict[str, list[int]]:
    """Parse repeatable EPOCH:INDEX-SPEC exclusion arguments."""
    exclusions: dict[str, set[int]] = defaultdict(set)
    for specification in specifications:
        epoch, separator, index_spec = str(specification).partition(":")
        epoch = epoch.strip()
        index_spec = index_spec.strip()
        if not separator or not epoch or not index_spec:
            raise ValueError(
                "--exclude-epoch-channels must use EPOCH:CHANNELS syntax, "
                "for example 'E:897-960'."
            )
        exclusions[epoch].update(
            _parse_index_spec(index_spec, "--exclude-epoch-channels")
        )
    return {
        epoch: sorted(indices)
        for epoch, indices in sorted(exclusions.items())
    }


def _format_index_spec(values: list[int] | tuple[int, ...]) -> str:
    """Return a compact stable range string."""
    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return ""
    ranges: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(
            f"{start}-{previous}" if start != previous else str(start)
        )
        start = previous = value
    ranges.append(
        f"{start}-{previous}" if start != previous else str(start)
    )
    return ",".join(ranges)


def _tag_token(value: str) -> str:
    token = "".join(
        character if character.isalnum() else "_"
        for character in str(value).strip()
    ).strip("_")
    return token or "epoch"


def _analysis_tag_from_settings(
    global_indices: list[int],
    epoch_indices: dict[str, list[int]],
    error_source: str,
) -> str | None:
    """Create a deterministic output-directory tag for non-default settings."""
    tags: list[str] = []

    # RMS is the Stage 3 default and therefore keeps the historical output path.
    # Opt-in DifMAP-error runs are separated so they can coexist with RMS results.
    if error_source != "rms":
        tags.append(f"error_{_tag_token(error_source)}")

    if global_indices or epoch_indices:
        if global_indices and not epoch_indices:
            exclusion_tag = (
                "exclude_" + _format_index_spec(global_indices).replace(",", "_")
            )
        else:
            parts: list[str] = []
            if global_indices:
                parts.append(
                    "all_" + _format_index_spec(global_indices).replace(",", "_")
                )
            for epoch, indices in sorted(epoch_indices.items()):
                parts.append(
                    f"{_tag_token(epoch)}_"
                    f"{_format_index_spec(indices).replace(',', '_')}"
                )
            exclusion_tag = "exclude_" + "__".join(parts)
        tags.append(exclusion_tag)

    return "__".join(tags) if tags else None


def _exclusion_configuration(
    global_spec: str | None,
    global_indices: list[int],
    epoch_specs: list[str],
    epoch_indices: dict[str, list[int]],
) -> dict[str, Any]:
    return {
        "global_spec": global_spec,
        "global_fit_indices": global_indices,
        "epoch_specs": list(epoch_specs),
        "per_epoch_fit_indices": epoch_indices,
    }



def json_safe(value):
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(payload), handle, indent=2)
        handle.write("\n")


def _print_available_products(products: dict[str, list[str]]) -> None:
    print("Available Stage 2 products")
    print("--------------------------")
    for observation, tags in products.items():
        print(f"{observation}: {', '.join(tags)}")


def _metadata_mjd(metadata: dict, dataset: Stage2Dataset) -> float:
    try:
        mjd = float(metadata.get("mjd"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{dataset.prefix}: Stage 2 metadata does not contain a valid MJD."
        ) from exc
    if not np.isfinite(mjd):
        raise ValueError(
            f"{dataset.prefix}: Stage 2 metadata does not contain a finite MJD."
        )
    return mjd


def _weighted_mean_and_error(values, errors) -> tuple[float, float]:
    """Return an inverse-variance weighted mean and uncertainty."""
    values = np.asarray(values, dtype=float)
    errors = np.asarray(errors, dtype=float)
    mask = np.isfinite(values) & np.isfinite(errors) & (errors > 0)
    if not np.any(mask):
        return float("nan"), float("nan")
    weights = 1.0 / np.square(errors[mask])
    mean = float(np.sum(weights * values[mask]) / np.sum(weights))
    error = float(np.sqrt(1.0 / np.sum(weights)))
    return mean, error


def _combined_average_spectrum_series(
    analyses: list[EpochAnalysis],
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    dict[str, dict[str, np.ndarray]],
]:
    """Return mean spectra aligned by Stage 2 fit index.

    The union of retained fit indices is used. If a channel is excluded from
    one epoch only, the combined point is formed from the remaining epochs.
    The plotted frequency is the arithmetic mean of the contributing epoch
    frequencies for that fit index; flux densities use inverse-variance
    weighting.
    """
    if not analyses:
        return (
            np.array([], dtype=int),
            np.array([], dtype=float),
            np.array([], dtype=int),
            {},
        )

    groups = list(analyses[0].fluxes)
    index_maps: list[dict[int, int]] = []
    for analysis in analyses:
        mapping: dict[int, int] = {}
        for position, fit_index in enumerate(analysis.fit_indices):
            key = int(fit_index)
            if key in mapping:
                raise ValueError(
                    f"{analysis.dataset.prefix}: duplicate fit_index {key}."
                )
            mapping[key] = position
        index_maps.append(mapping)

    union_indices = sorted(
        set().union(*(mapping.keys() for mapping in index_maps))
    )
    rows: list[tuple[int, float, int, dict[str, tuple[float, float]]]] = []

    for fit_index in union_indices:
        contributions = [
            (analysis, mapping[fit_index])
            for analysis, mapping in zip(analyses, index_maps, strict=False)
            if fit_index in mapping
        ]
        frequencies = np.asarray(
            [analysis.frequency_ghz[position] for analysis, position in contributions],
            dtype=float,
        )
        frequency = float(np.nanmean(frequencies))
        group_results: dict[str, tuple[float, float]] = {}
        for group in groups:
            values = [
                analysis.fluxes[group][position]
                for analysis, position in contributions
            ]
            errors = [
                analysis.uncertainties[group][position]
                for analysis, position in contributions
            ]
            group_results[group] = _weighted_mean_and_error(values, errors)
        rows.append((fit_index, frequency, len(contributions), group_results))

    rows.sort(key=lambda row: (row[1], row[0]))
    fit_indices = np.asarray([row[0] for row in rows], dtype=int)
    frequencies = np.asarray([row[1] for row in rows], dtype=float)
    n_epochs = np.asarray([row[2] for row in rows], dtype=int)
    result = {
        group: {
            "values": np.asarray(
                [row[3][group][0] for row in rows], dtype=float
            ),
            "errors": np.asarray(
                [row[3][group][1] for row in rows], dtype=float
            ),
        }
        for group in groups
    }
    return fit_indices, frequencies, n_epochs, result


def _combined_average_ratio_series(
    analyses: list[EpochAnalysis],
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    dict[str, dict[str, np.ndarray]],
]:
    """Return mean flux ratios aligned by Stage 2 fit index.

    The union of retained fit indices is used. For an index excluded from only
    one epoch, the average is formed from the remaining epochs. Frequencies are
    the arithmetic mean of the contributing epoch frequencies; flux ratios use
    inverse-variance weighting.
    """
    if not analyses or not analyses[0].ratios:
        return (
            np.array([], dtype=int),
            np.array([], dtype=float),
            np.array([], dtype=int),
            {},
        )

    ratio_labels = list(analyses[0].ratios)
    index_maps: list[dict[int, int]] = []
    for analysis in analyses:
        mapping: dict[int, int] = {}
        for position, fit_index in enumerate(analysis.fit_indices):
            key = int(fit_index)
            if key in mapping:
                raise ValueError(
                    f"{analysis.dataset.prefix}: duplicate fit_index {key}."
                )
            mapping[key] = position
        index_maps.append(mapping)

    union_indices = sorted(
        set().union(*(mapping.keys() for mapping in index_maps))
    )
    rows: list[
        tuple[
            int,
            float,
            int,
            dict[str, tuple[float, float, int]],
        ]
    ] = []

    for fit_index in union_indices:
        contributions = [
            (analysis, mapping[fit_index])
            for analysis, mapping in zip(analyses, index_maps, strict=False)
            if fit_index in mapping
        ]
        frequencies = np.asarray(
            [
                analysis.frequency_ghz[position]
                for analysis, position in contributions
            ],
            dtype=float,
        )
        frequency = float(np.nanmean(frequencies))
        ratio_results: dict[str, tuple[float, float, int]] = {}
        for label in ratio_labels:
            values = np.asarray(
                [analysis.ratios[label][position] for analysis, position in contributions],
                dtype=float,
            )
            errors = np.asarray(
                [analysis.ratio_errors[label][position] for analysis, position in contributions],
                dtype=float,
            )
            valid = np.isfinite(values) & np.isfinite(errors) & (errors > 0)
            mean, error = _weighted_mean_and_error(values, errors)
            ratio_results[label] = (mean, error, int(np.count_nonzero(valid)))
        rows.append((fit_index, frequency, len(contributions), ratio_results))

    rows.sort(key=lambda row: (row[1], row[0]))
    fit_indices = np.asarray([row[0] for row in rows], dtype=int)
    frequencies = np.asarray([row[1] for row in rows], dtype=float)
    n_epochs_with_channel = np.asarray([row[2] for row in rows], dtype=int)
    result = {
        label: {
            "values": np.asarray(
                [row[3][label][0] for row in rows], dtype=float
            ),
            "errors": np.asarray(
                [row[3][label][1] for row in rows], dtype=float
            ),
            "n_epochs_contributing": np.asarray(
                [row[3][label][2] for row in rows], dtype=int
            ),
        }
        for label in ratio_labels
    }
    return fit_indices, frequencies, n_epochs_with_channel, result


def _reference_flux_series(
    analyses: list[EpochAnalysis],
) -> dict[str, dict[str, np.ndarray]]:
    """Collect fitted reference-frequency flux densities versus MJD."""
    if not analyses:
        return {}
    groups = list(analyses[0].spectral_fits)
    return {
        group: {
            "values": np.asarray([item.spectral_fits[group].s_ref_jy for item in analyses], dtype=float),
            "errors": np.asarray([item.spectral_fits[group].s_ref_error_jy for item in analyses], dtype=float),
        }
        for group in groups
    }


def _normalised_ratio_series(
    weighted_ratio_series: dict[str, dict[str, np.ndarray]],
) -> dict[str, dict[str, Any]]:
    """Normalise each visit's weighted ratio by the all-visit weighted mean."""
    normalised: dict[str, dict[str, Any]] = {}
    for label, series in weighted_ratio_series.items():
        values = np.asarray(series["values"], dtype=float)
        errors = np.asarray(series["errors"], dtype=float)
        overall_mean, overall_error = _weighted_mean_and_error(values, errors)

        norm_values = np.full(values.shape, np.nan, dtype=float)
        norm_errors = np.full(errors.shape, np.nan, dtype=float)
        valid = (
            np.isfinite(values)
            & np.isfinite(errors)
            & (errors > 0)
            & (values != 0.0)
            & np.isfinite(overall_mean)
            & (overall_mean != 0.0)
        )
        if np.any(valid):
            norm_values[valid] = values[valid] / overall_mean
            norm_errors[valid] = np.abs(norm_values[valid]) * np.sqrt(
                np.square(errors[valid] / values[valid])
                + (overall_error / overall_mean) ** 2
            )

        normalised[label] = {
            "values": norm_values,
            "errors": norm_errors,
            "all_epoch_weighted_mean": overall_mean,
            "all_epoch_weighted_mean_error": overall_error,
        }
    return normalised


def _rcusp_from_fluxes(a1: float, a2: float, b: float) -> float:
    """Return the established signed-parity cusp statistic in absolute form."""
    numerator = abs(float(a1) - float(a2) + float(b))
    denominator = float(a1) + float(a2) + float(b)
    if denominator <= 0 or not np.isfinite(numerator):
        return float("nan")
    return float(numerator / denominator)


def _rcusp_error_from_fluxes(a1, a2, b, sa1, sa2, sb) -> float:
    """Propagate independent fitted reference-flux uncertainties into R_cusp."""
    a1 = float(a1)
    a2 = float(a2)
    b = float(b)
    sa1 = float(sa1)
    sa2 = float(sa2)
    sb = float(sb)
    numerator = a1 - a2 + b
    denominator = a1 + a2 + b
    if denominator <= 0 or not np.isfinite(numerator):
        return float("nan")

    sign_n = 1.0 if numerator >= 0 else -1.0
    abs_n = abs(numerator)
    d_a1 = (sign_n * denominator - abs_n) / denominator**2
    d_a2 = (-sign_n * denominator - abs_n) / denominator**2
    d_b = (sign_n * denominator - abs_n) / denominator**2
    variance = (d_a1 * sa1) ** 2 + (d_a2 * sa2) ** 2 + (d_b * sb) ** 2
    return float(np.sqrt(variance)) if variance >= 0 else float("nan")


def _build_rcusp_series(
    analyses: list[EpochAnalysis],
    images: tuple[str, str, str] | list[str] = ("A1", "A2", "B"),
) -> tuple[np.ndarray, np.ndarray] | None:
    """Build R_cusp from fitted reference-frequency fluxes of the three cusp images.

    ``images`` names the groups in the order (A1, A2, B) of the cusp relation
    |S_A1 - S_A2 + S_B| / (S_A1 + S_A2 + S_B). An empty list disables R_cusp.
    """
    if not images or len(images) != 3:
        return None
    a1_name, a2_name, b_name = images
    required = (a1_name, a2_name, b_name)
    if not analyses or not all(
        all(name in item.spectral_fits for name in required)
        for item in analyses
    ):
        return None

    values: list[float] = []
    errors: list[float] = []
    for item in analyses:
        fa1 = item.spectral_fits[a1_name].s_ref_jy
        sa1 = item.spectral_fits[a1_name].s_ref_error_jy
        fa2 = item.spectral_fits[a2_name].s_ref_jy
        sa2 = item.spectral_fits[a2_name].s_ref_error_jy
        fb = item.spectral_fits[b_name].s_ref_jy
        sb = item.spectral_fits[b_name].s_ref_error_jy
        values.append(_rcusp_from_fluxes(fa1, fa2, fb))
        errors.append(_rcusp_error_from_fluxes(fa1, fa2, fb, sa1, sa2, sb))
    return np.asarray(values, dtype=float), np.asarray(errors, dtype=float)


def analyse_dataset(
    dataset: Stage2Dataset,
    reference_frequency_ghz: float,
    fit_method: str,
    emcee_settings: dict,
    error_source: str = "rms",
    excluded_fit_indices: set[int] | None = None,
) -> EpochAnalysis:
    metadata = load_stage2_metadata(dataset)
    frame = load_spectrum(
        dataset, metadata, excluded_fit_indices=excluded_fit_indices
    )
    grouped_columns = grouped_flux_columns(metadata, frame)
    reference_group = grouped_columns[0][0]

    if "fit_index" in frame.columns:
        fit_indices = frame["fit_index"].to_numpy(dtype=int)
    else:
        fit_indices = np.arange(1, len(frame) + 1, dtype=int)
    if len(np.unique(fit_indices)) != len(fit_indices):
        raise ValueError(f"{dataset.prefix}: fit_index values are not unique.")

    frequency = frame["frequency_ghz"].to_numpy(dtype=float)
    rms = frame["rms_jy_per_beam"].to_numpy(dtype=float)
    mjd = _metadata_mjd(metadata, dataset)

    fluxes: dict[str, np.ndarray] = {}
    uncertainties: dict[str, np.ndarray] = {}
    uncertainty_sources: dict[str, str] = {}
    spectral_fits: dict[str, Any] = {}
    posteriors: dict[str, dict[str, np.ndarray]] = {}

    for group, column in grouped_columns:
        flux = frame[column].to_numpy(dtype=float)
        fluxes[group] = flux

        error_column = f"{safe_label(group)}_error_jy"
        if error_source == "rms":
            uncertainty = rms.copy()
            valid_error = np.isfinite(uncertainty) & (uncertainty > 0)
            if np.count_nonzero(valid_error) < 3:
                raise ValueError(
                    f"{dataset.prefix}: residual-image RMS contains fewer than "
                    "three finite positive values after exclusions."
                )
            uncertainty_sources[group] = "stage2_rms:rms_jy_per_beam"
        elif error_source == "difmap":
            if error_column not in frame.columns:
                raise ValueError(
                    f"{dataset.prefix}: --error-source difmap requested, but "
                    f"Stage 2 column '{error_column}' is missing for group '{group}'."
                )
            try:
                uncertainty = frame[error_column].to_numpy(dtype=float)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{dataset.prefix}: --error-source difmap requested, but "
                    f"Stage 2 column '{error_column}' cannot be converted to "
                    "numeric uncertainties."
                ) from exc
            valid_error = np.isfinite(uncertainty) & (uncertainty > 0)
            if np.count_nonzero(valid_error) < 3:
                raise ValueError(
                    f"{dataset.prefix}: --error-source difmap requested, but "
                    f"Stage 2 column '{error_column}' has fewer than three "
                    "finite positive uncertainties after exclusions."
                )
            uncertainty_sources[group] = f"stage2_difmap:{error_column}"
        else:
            raise ValueError(f"Unsupported error source: {error_source}")

        uncertainties[group] = uncertainty
        if fit_method == "emcee":
            fit_result, posterior = fit_power_law_emcee(
                group,
                frequency,
                flux,
                uncertainty,
                reference_frequency_ghz,
                **emcee_settings,
            )
            spectral_fits[group] = fit_result
            posteriors[group] = posterior
        else:
            spectral_fits[group] = fit_power_law(
                group,
                frequency,
                flux,
                uncertainty,
                reference_frequency_ghz,
            )

    ratios: dict[str, np.ndarray] = {}
    ratio_errors: dict[str, np.ndarray] = {}
    ratio_fits: dict[str, Any] = {}

    for group, _ in grouped_columns[1:]:
        label = f"{group}/{reference_group}"
        ratio, ratio_error = calculate_ratio(
            fluxes[group],
            fluxes[reference_group],
            uncertainties[group],
            uncertainties[reference_group],
        )
        ratios[label] = ratio
        ratio_errors[label] = ratio_error
        ratio_fits[label] = fit_ratio_power_law(
            group,
            reference_group,
            frequency,
            ratio,
            ratio_error,
            reference_frequency_ghz,
        )

    return EpochAnalysis(
        dataset=dataset,
        metadata=metadata,
        mjd=mjd,
        fit_indices=fit_indices,
        frequency_ghz=frequency,
        rms_jy_per_beam=rms,
        excluded_fit_indices=tuple(sorted(excluded_fit_indices or set())),
        fluxes=fluxes,
        error_source=error_source,
        uncertainty_sources=uncertainty_sources,
        uncertainties=uncertainties,
        spectral_fits=spectral_fits,
        posteriors=posteriors,
        ratios=ratios,
        ratio_errors=ratio_errors,
        ratio_fits=ratio_fits,
        reference_group=reference_group,
    )


def _write_measured_ratios(path_csv: Path, path_json: Path, analysis: EpochAnalysis) -> None:
    rows: list[dict[str, float]] = []
    for row_index, frequency_ghz in enumerate(analysis.frequency_ghz):
        row: dict[str, float] = {
            "fit_index": int(analysis.fit_indices[row_index]),
            "frequency_ghz": float(frequency_ghz),
            "frequency_hz": float(frequency_ghz * 1.0e9),
        }
        for label in analysis.ratios:
            output_label = label.replace("/", "_over_")
            row[f"{output_label}_ratio"] = float(
                analysis.ratios[label][row_index]
            )
            row[f"{output_label}_error"] = float(
                analysis.ratio_errors[label][row_index]
            )
        rows.append(row)
    write_csv(path_csv, rows)
    write_json(path_json, rows)


def _save_rms_figure(fig, output_base: Path) -> None:
    """Write one RMS diagnostic in the configured figure formats."""
    fig.tight_layout()
    plotting.save_figure(fig, output_base, dpi=200)


def _format_rms_axes(ax, xlabel: str, ylabel: str) -> None:
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.tick_params(which="major", direction="in", top=True, right=True)
    ax.minorticks_off()


def plot_epoch_rms_diagnostic(analysis: EpochAnalysis, output_base: Path) -> None:
    """Plot the retained Stage 2 residual-image RMS versus fit/channel index."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(
        analysis.fit_indices,
        analysis.rms_jy_per_beam * 1000.0,
        linewidth=0.8,
    )
    _format_rms_axes(ax, "Channel / fit index", "Residual RMS (mJy beam$^{-1}$)")
    ax.set_title(f"MJD {analysis.mjd:.4f}")
    _save_rms_figure(fig, output_base)


def _aligned_rms_diagnostics(analyses: list[EpochAnalysis]):
    """Align per-visit RMS measurements by Stage 2 fit index."""
    union = sorted(set().union(*(set(map(int, a.fit_indices)) for a in analyses)))
    index_to_col = {fit_index: col for col, fit_index in enumerate(union)}
    matrix = np.full((len(analyses), len(union)), np.nan, dtype=float)
    freq_matrix = np.full_like(matrix, np.nan)
    for row, analysis in enumerate(analyses):
        for pos, fit_index in enumerate(analysis.fit_indices):
            col = index_to_col[int(fit_index)]
            matrix[row, col] = float(analysis.rms_jy_per_beam[pos])
            freq_matrix[row, col] = float(analysis.frequency_ghz[pos])
    fit_indices = np.asarray(union, dtype=int)
    frequency = np.nanmean(freq_matrix, axis=0)
    n_epochs = np.sum(np.isfinite(matrix), axis=0).astype(int)
    median = np.nanmedian(matrix, axis=0)
    abs_dev = np.abs(matrix - median[None, :])
    mad = np.nanmedian(abs_dev, axis=0)
    robust_sigma = 1.4826 * mad
    return fit_indices, frequency, n_epochs, matrix, median, mad, robust_sigma


def write_combined_rms_diagnostics(
    analyses: list[EpochAnalysis],
    plots_dir: Path,
    tables_dir: Path,
    stem: str,
) -> dict[str, Any]:
    """Write cross-visit RMS diagnostics without rejecting any measurements."""
    (
        fit_indices,
        frequency,
        n_epochs,
        matrix,
        median,
        mad,
        robust_sigma,
    ) = _aligned_rms_diagnostics(analyses)

    # Raw RMS by visit.
    fig, ax = plt.subplots(figsize=(9, 5))
    for row, analysis in enumerate(analyses):
        ax.plot(fit_indices, matrix[row] * 1000.0, linewidth=0.7, label=analysis.dataset.epoch)
    _format_rms_axes(ax, "Channel / fit index", "Residual RMS (mJy beam$^{-1}$)")
    ax.legend(ncol=min(5, max(1, len(analyses))), fontsize=8)
    _save_rms_figure(fig, plots_dir / f"{stem}.rms_vs_channel_all_epochs")

    # Persistent band structure: median across visits for each channel.
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(fit_indices, median * 1000.0, linewidth=0.9)
    _format_rms_axes(ax, "Channel / fit index", "Median residual RMS (mJy beam$^{-1}$)")
    _save_rms_figure(fig, plots_dir / f"{stem}.median_rms_vs_channel")

    with np.errstate(divide="ignore", invalid="ignore"):
        relative = matrix / median[None, :]
        zscore = (matrix - median[None, :]) / robust_sigma[None, :]
    relative[~np.isfinite(relative)] = np.nan
    zscore[~np.isfinite(zscore)] = np.nan

    # Each visit relative to the visit-to-visit median of the same channel.
    fig, ax = plt.subplots(figsize=(9, 5))
    for row, analysis in enumerate(analyses):
        ax.plot(fit_indices, relative[row], linewidth=0.7, label=analysis.dataset.epoch)
    ax.axhline(1.0, linestyle="--", linewidth=0.8)
    _format_rms_axes(ax, "Channel / fit index", "RMS / channel median RMS")
    ax.legend(ncol=min(5, max(1, len(analyses))), fontsize=8)
    _save_rms_figure(fig, plots_dir / f"{stem}.relative_rms_vs_channel_all_epochs")

    # Robust visit-to-visit channel statistic.  It is diagnostic only.
    fig, ax = plt.subplots(figsize=(9, 5))
    for row, analysis in enumerate(analyses):
        ax.plot(fit_indices, zscore[row], linewidth=0.7, label=analysis.dataset.epoch)
    ax.axhline(0.0, linestyle="--", linewidth=0.8)
    _format_rms_axes(ax, "Channel / fit index", "Visit-to-visit robust RMS score")
    ax.legend(ncol=min(5, max(1, len(analyses))), fontsize=8)
    _save_rms_figure(fig, plots_dir / f"{stem}.rms_visit_robust_score_vs_channel")

    channel_rows: list[dict[str, Any]] = []
    visit_rows: list[dict[str, Any]] = []
    for col, fit_index in enumerate(fit_indices):
        channel_rows.append({
            "fit_index": int(fit_index),
            "frequency_ghz": float(frequency[col]),
            "n_epochs_contributing": int(n_epochs[col]),
            "median_rms_jy_per_beam": float(median[col]),
            "mad_rms_jy_per_beam": float(mad[col]),
            "robust_sigma_rms_jy_per_beam": float(robust_sigma[col]),
        })
        for row, analysis in enumerate(analyses):
            if not np.isfinite(matrix[row, col]):
                continue
            visit_rows.append({
                "epoch": analysis.dataset.epoch,
                "mjd": float(analysis.mjd),
                "fit_index": int(fit_index),
                "frequency_ghz": float(frequency[col]),
                "rms_jy_per_beam": float(matrix[row, col]),
                "channel_median_rms_jy_per_beam": float(median[col]),
                "channel_robust_sigma_rms_jy_per_beam": float(robust_sigma[col]),
                "rms_relative_to_channel_median": float(relative[row, col]),
                "rms_visit_robust_score": float(zscore[row, col]),
            })

    write_csv(tables_dir / f"{stem}.rms_channel_statistics.csv", channel_rows)
    write_json(tables_dir / f"{stem}.rms_channel_statistics.json", channel_rows)
    write_csv(tables_dir / f"{stem}.rms_visit_diagnostics.csv", visit_rows)
    write_json(tables_dir / f"{stem}.rms_visit_diagnostics.json", visit_rows)

    return {
        "definition": (
            "RMS diagnostics compare each Stage 2 fit index across visits. "
            "The per-channel reference is the visit-to-visit median; robust "
            "scatter is 1.4826 times the MAD. No RMS statistic excludes data."
        ),
        "n_channels": int(len(fit_indices)),
        "n_visits": int(len(analyses)),
    }


def write_epoch_products(
    analysis: EpochAnalysis,
    reference_frequency_ghz: float,
    overwrite: bool,
    annotate_fits: bool,
    fit_method: str,
    emcee_settings: dict,
    spectrum_y_limits: tuple[float, float],
    ratio_y_limits: tuple[float, float] | None,
    exclusion_configuration: dict[str, Any],
    analysis_tag: str | None,
) -> bool:
    dataset = analysis.dataset
    final_output = dataset.output_directory
    if final_output.exists() and not overwrite:
        print(f"SKIPPED WRITING: {dataset.prefix}; output already exists.")
        return False

    final_output.parent.mkdir(parents=True, exist_ok=True)
    working = final_output.parent / f".{final_output.name}.stage3-{STAGE3_VERSION}.tmp"
    if working.exists():
        shutil.rmtree(working)

    fits_dir = working / "fits"
    plots_dir = working / "plots"
    fits_dir.mkdir(parents=True)
    plots_dir.mkdir(parents=True)

    try:
        powerlaw_rows = [
            analysis.spectral_fits[group].as_dict()
            for group in analysis.fluxes
        ]
        ratio_rows = [
            analysis.ratio_fits[label].as_dict()
            for label in analysis.ratios
        ]

        write_csv(fits_dir / f"{dataset.prefix}.powerlaw_fits.csv", powerlaw_rows)
        write_json(fits_dir / f"{dataset.prefix}.powerlaw_fits.json", powerlaw_rows)

        if ratio_rows:
            write_csv(
                fits_dir / f"{dataset.prefix}.flux_ratio_fits.csv",
                ratio_rows,
            )
            write_json(
                fits_dir / f"{dataset.prefix}.flux_ratio_fits.json",
                ratio_rows,
            )
            _write_measured_ratios(
                fits_dir / f"{dataset.prefix}.measured_flux_ratios.csv",
                fits_dir / f"{dataset.prefix}.measured_flux_ratios.json",
                analysis,
            )

        for group, posterior in analysis.posteriors.items():
            np.savez_compressed(
                fits_dir / f"{dataset.prefix}.{safe_label(group)}.posterior.npz",
                **posterior,
            )

        # The primary spectrum figure remains clean; annotations are written to
        # a separate file so both publication and diagnostic versions coexist.
        plot_spectra(
            analysis.frequency_ghz,
            analysis.fluxes,
            analysis.uncertainties,
            analysis.spectral_fits,
            dataset.source,
            dataset.epoch,
            plots_dir / f"{dataset.prefix}.spectra",
            reference_frequency_ghz=reference_frequency_ghz,
            annotate_fits=False,
            mjd=analysis.mjd,
            x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
            y_limits=spectrum_y_limits,
        )
        if annotate_fits:
            plot_spectra(
                analysis.frequency_ghz,
                analysis.fluxes,
                analysis.uncertainties,
                analysis.spectral_fits,
                dataset.source,
                dataset.epoch,
                plots_dir / f"{dataset.prefix}.spectra_annotated",
                reference_frequency_ghz=reference_frequency_ghz,
                annotate_fits=True,
                mjd=analysis.mjd,
                x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
                y_limits=spectrum_y_limits,
            )
        plot_spectra_with_residuals(
            analysis.frequency_ghz,
            analysis.fluxes,
            analysis.uncertainties,
            analysis.spectral_fits,
            dataset.source,
            dataset.epoch,
            plots_dir / f"{dataset.prefix}.spectra_residuals",
            reference_frequency_ghz=reference_frequency_ghz,
            mjd=analysis.mjd,
            x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
            y_limits=spectrum_y_limits,
        )
        if analysis.ratios and ratio_y_limits is not None:
            plot_flux_ratios(
                analysis.frequency_ghz,
                analysis.ratios,
                analysis.ratio_errors,
                dataset.source,
                dataset.epoch,
                plots_dir / f"{dataset.prefix}.flux_ratios",
                mjd=analysis.mjd,
                x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
                y_limits=ratio_y_limits,
            )

        plot_epoch_rms_diagnostic(
            analysis, plots_dir / f"{dataset.prefix}.rms_vs_channel"
        )

        created = datetime.now(UTC).isoformat()
        stage3_metadata = {
            "schema_version": "1.2",
            "stage": 3,
            "stage3_version": STAGE3_VERSION,
            "created_utc": created,
            "source": dataset.source,
            "epoch": dataset.epoch,
            "product_tag": dataset.product_tag,
            "analysis_tag": analysis_tag,
            "mjd": analysis.mjd,
            "reference_frequency_ghz": reference_frequency_ghz,
            "fit_method": fit_method,
            "emcee_settings": emcee_settings if fit_method == "emcee" else None,
            "reference_group": analysis.reference_group,
            "groups": list(analysis.fluxes),
            "n_measurements": len(analysis.frequency_ghz),
            "plot_style": {
                "font_family": "STIXGeneral",
                "title": "MJD only",
                "major_ticks": "inward on all four sides",
                "minor_ticks": False,
                "error_bars": plotting.SHOW_ERROR_BARS,
                "frequency_limits_ghz": list(plotting.SUMMARY_FREQUENCY_LIMITS_GHZ),
                "spectrum_y_limits_mjy": list(spectrum_y_limits),
                "ratio_y_limits": (
                    list(ratio_y_limits) if ratio_y_limits is not None else None
                ),
            },
            "stage2": {
                "manifest": str(dataset.manifest_json),
                "spectrum": str(dataset.spectrum_csv),
                "metadata": str(dataset.metadata_json),
                "stage2_version": analysis.metadata.get("stage2_version"),
                "mode": analysis.metadata.get("mode"),
                "channels_per_if": analysis.metadata.get("channels_per_if"),
                "excluded_edge_channels_per_side": analysis.metadata.get(
                    "excluded_edge_channels_per_side"
                ),
                "fitted_channels_per_if": analysis.metadata.get(
                    "fitted_channels_per_if"
                ),
            },
            "channel_exclusion": {
                **exclusion_configuration,
                "effective_fit_indices_for_epoch": list(
                    analysis.excluded_fit_indices
                ),
            },
            "uncertainty_model": {
                "requested": analysis.error_source,
                "per_group_source": analysis.uncertainty_sources,
                "description": (
                    "Stage 2 residual-image RMS (rms_jy_per_beam) is used for "
                    "Stage 3 fitting and ratio propagation."
                    if analysis.error_source == "rms"
                    else
                    "Stage 2 hierarchy-defined grouped formal DifMAP flux "
                    "uncertainties (<group>_error_jy) are used for Stage 3 "
                    "fitting and ratio propagation."
                ),
                "fallback": None,
                "rms_role": (
                    "Residual-image RMS is also retained independently as a channel "
                    "quality diagnostic and does not trigger automatic exclusion."
                ),
            },
            "figure_formats": list(plotting.FIGURE_FORMATS),
            "provenance": {
                "spectrum": fingerprint(dataset.spectrum_csv),
                "metadata": fingerprint(dataset.metadata_json),
            },
        }
        write_json(
            working / f"{dataset.prefix}.stage3.metadata.json",
            stage3_metadata,
        )

        product_paths = sorted(
            path.relative_to(working).as_posix()
            for path in working.rglob("*")
            if path.is_file()
        )
        write_json(
            working / f"{dataset.prefix}.stage3.manifest.json",
            {
                "schema_version": "1.2",
                "stage": 3,
                "stage3_version": STAGE3_VERSION,
                "source": dataset.source,
                "epoch": dataset.epoch,
                "product_tag": dataset.product_tag,
                "analysis_tag": analysis_tag,
                "created_utc": created,
                "stage2_manifest": str(dataset.manifest_json),
                "products": product_paths,
            },
        )

        if final_output.exists():
            shutil.rmtree(final_output)
        working.replace(final_output)
    except Exception:
        if working.exists():
            shutil.rmtree(working)
        raise

    print(f"COMPLETED: {dataset.prefix}")
    print(f"  Plots: {final_output / 'plots'}")
    return True


def write_combined_products(
    analyses: list[EpochAnalysis],
    reference_frequency_ghz: float,
    overwrite: bool,
    spectrum_y_limits: tuple[float, float],
    ratio_y_limits: tuple[float, float] | None,
    exclusion_configuration: dict[str, Any],
    analysis_tag: str | None,
    rcusp_images: list[str] | tuple[str, str, str] = ("A1", "A2", "B"),
) -> bool:
    if not analyses:
        return False

    analyses = sorted(analyses, key=lambda item: item.mjd)
    first = analyses[0]
    source = first.dataset.source
    product_tag = first.dataset.product_tag

    for analysis in analyses[1:]:
        if analysis.dataset.source != source:
            raise ValueError("Cannot combine different sources in one summary.")
        if analysis.dataset.product_tag != product_tag:
            raise ValueError("Cannot combine different Stage 2 products.")
        if list(analysis.fluxes) != list(first.fluxes):
            raise ValueError("Group hierarchy differs between visits.")
        if list(analysis.ratios) != list(first.ratios):
            raise ValueError("Flux-ratio labels differ between visits.")

    project_root = first.dataset.project_root
    final_output = project_root / "stage3" / "combined" / source / product_tag
    if analysis_tag:
        final_output = final_output / analysis_tag
    if final_output.exists() and not overwrite:
        print(
            f"SKIPPED COMBINED: {source}.{product_tag}; output already exists."
        )
        return False

    final_output.parent.mkdir(parents=True, exist_ok=True)
    working = final_output.parent / f".{product_tag}.stage3-{STAGE3_VERSION}.tmp"
    if working.exists():
        shutil.rmtree(working)
    plots_dir = working / "plots"
    tables_dir = working / "tables"
    plots_dir.mkdir(parents=True)
    tables_dir.mkdir(parents=True)

    records = [analysis.plot_record() for analysis in analyses]
    stem = f"{source}.{product_tag}"
    combined_fit_rows: list[dict[str, Any]] = []
    combined_fit_payload: dict[str, Any] = {}

    try:
        rms_diagnostic_metadata = write_combined_rms_diagnostics(
            analyses, plots_dir, tables_dir, stem
        )

        (
            average_spectrum_fit_indices,
            average_spectrum_frequency,
            average_spectrum_n_epochs,
            average_spectrum_series,
        ) = _combined_average_spectrum_series(analyses)
        if average_spectrum_series:
            average_spectrum_fits = fit_average_spectra(
                average_spectrum_frequency,
                average_spectrum_series,
                reference_frequency_ghz,
            )
            combined_fit_payload["average_spectrum"] = {
                label: fit.as_dict() for label, fit in average_spectrum_fits.items()
            }
            for label, fit in average_spectrum_fits.items():
                combined_fit_rows.extend([
                    {
                        "product": "average_spectrum", "label": label,
                        "parameter": "s_ref_jy", "value": fit.s_ref_jy,
                        "error": fit.s_ref_error_jy,
                        "reference_frequency_ghz": fit.reference_frequency_ghz,
                        "chi_square": fit.chi_square,
                        "reduced_chi_square": fit.reduced_chi_square,
                        "degrees_of_freedom": fit.degrees_of_freedom,
                        "n_points": fit.n_points,
                    },
                    {
                        "product": "average_spectrum", "label": label,
                        "parameter": "alpha", "value": fit.alpha,
                        "error": fit.alpha_error,
                        "reference_frequency_ghz": fit.reference_frequency_ghz,
                        "chi_square": fit.chi_square,
                        "reduced_chi_square": fit.reduced_chi_square,
                        "degrees_of_freedom": fit.degrees_of_freedom,
                        "n_points": fit.n_points,
                    },
                ])
            plot_combined_average_spectrum(
                average_spectrum_frequency,
                average_spectrum_series,
                source,
                plots_dir / f"{stem}.average_spectrum",
                fits=average_spectrum_fits,
                reference_frequency_ghz=reference_frequency_ghz,
                y_limits=spectrum_y_limits,
            )

            average_spectrum_rows: list[dict[str, Any]] = []
            for row_index, frequency_ghz in enumerate(average_spectrum_frequency):
                row: dict[str, Any] = {
                    "fit_index": int(average_spectrum_fit_indices[row_index]),
                    "frequency_ghz": float(frequency_ghz),
                    "frequency_hz": float(frequency_ghz * 1.0e9),
                    "n_epochs_contributing": int(
                        average_spectrum_n_epochs[row_index]
                    ),
                }
                for label, series in average_spectrum_series.items():
                    output_label = safe_label(label)
                    row[f"{output_label}_flux_jy"] = float(series["values"][row_index])
                    row[f"{output_label}_flux_error_jy"] = float(series["errors"][row_index])
                    row[f"{output_label}_flux_mjy"] = float(series["values"][row_index] * 1000.0)
                    row[f"{output_label}_flux_error_mjy"] = float(series["errors"][row_index] * 1000.0)
                average_spectrum_rows.append(row)

            write_csv(
                tables_dir / f"{stem}.average_spectrum.csv",
                average_spectrum_rows,
            )
            write_json(
                tables_dir / f"{stem}.average_spectrum.json",
                average_spectrum_rows,
            )

        (
            average_ratio_fit_indices,
            average_ratio_frequency,
            average_ratio_n_epochs,
            average_ratio_series,
        ) = _combined_average_ratio_series(analyses)
        if average_ratio_series:
            average_ratio_fits = fit_average_flux_ratios(average_ratio_series)
            combined_fit_payload["average_flux_ratios"] = {
                label: fit.as_dict() for label, fit in average_ratio_fits.items()
            }
            for label, fit in average_ratio_fits.items():
                combined_fit_rows.append({
                    "product": "average_flux_ratio", "label": label,
                    "parameter": "constant", "value": fit.value,
                    "error": fit.error, "reference_frequency_ghz": None,
                    "chi_square": fit.chi_square,
                    "reduced_chi_square": fit.reduced_chi_square,
                    "degrees_of_freedom": fit.degrees_of_freedom,
                    "n_points": fit.n_points,
                })
            plot_combined_average_flux_ratios(
                average_ratio_frequency,
                average_ratio_series,
                source,
                plots_dir / f"{stem}.average_flux_ratios",
                fits=average_ratio_fits,
                y_limits=ratio_y_limits,
            )

            average_ratio_rows: list[dict[str, Any]] = []
            for row_index, frequency_ghz in enumerate(average_ratio_frequency):
                row: dict[str, Any] = {
                    "fit_index": int(average_ratio_fit_indices[row_index]),
                    "frequency_ghz": float(frequency_ghz),
                    "frequency_hz": float(frequency_ghz * 1.0e9),
                    "n_epochs_with_channel": int(
                        average_ratio_n_epochs[row_index]
                    ),
                }
                for label, series in average_ratio_series.items():
                    output_label = label.replace("/", "_over_")
                    row[f"{output_label}_ratio"] = float(
                        series["values"][row_index]
                    )
                    row[f"{output_label}_error"] = float(
                        series["errors"][row_index]
                    )
                    row[f"{output_label}_n_epochs_contributing"] = int(
                        series["n_epochs_contributing"][row_index]
                    )
                average_ratio_rows.append(row)

            write_csv(
                tables_dir / f"{stem}.average_flux_ratios.csv",
                average_ratio_rows,
            )
            write_json(
                tables_dir / f"{stem}.average_flux_ratios.json",
                average_ratio_rows,
            )

        plot_all_epochs_spectra(
            records,
            plots_dir / f"{stem}.all_epochs_spectra_4col",
            reference_frequency_ghz=reference_frequency_ghz,
            x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
            y_limits=spectrum_y_limits,
        )
        if first.ratios and ratio_y_limits is not None:
            plot_all_epochs_flux_ratios(
                records,
                plots_dir / f"{stem}.all_epochs_flux_ratios_4col",
                x_limits=plotting.SUMMARY_FREQUENCY_LIMITS_GHZ,
                y_limits=ratio_y_limits,
            )

        mjds = np.asarray([item.mjd for item in analyses], dtype=float)

        reference_flux_series = _reference_flux_series(analyses)
        if reference_flux_series:
            plot_epoch_reference_fluxes_vs_mjd(
                mjds,
                reference_flux_series,
                source,
                plots_dir / f"{stem}.reference_fluxes_vs_mjd",
                reference_frequency_ghz=reference_frequency_ghz,
            )

            reference_flux_rows: list[dict[str, Any]] = []
            for row_index, item in enumerate(analyses):
                row: dict[str, Any] = {
                    "epoch": item.dataset.epoch,
                    "mjd": float(item.mjd),
                }
                for label, series in reference_flux_series.items():
                    output_label = safe_label(label)
                    row[f"{output_label}_sref_jy"] = float(series["values"][row_index])
                    row[f"{output_label}_sref_error_jy"] = float(series["errors"][row_index])
                    row[f"{output_label}_sref_mjy"] = float(series["values"][row_index] * 1000.0)
                    row[f"{output_label}_sref_error_mjy"] = float(series["errors"][row_index] * 1000.0)
                reference_flux_rows.append(row)

            write_csv(
                tables_dir / f"{stem}.reference_fluxes_vs_mjd.csv",
                reference_flux_rows,
            )
            write_json(
                tables_dir / f"{stem}.reference_fluxes_vs_mjd.json",
                reference_flux_rows,
            )
        weighted_ratio_series = {
            label: {
                "values": np.asarray(
                    [item.ratio_fits[label].weighted_mean_ratio for item in analyses],
                    dtype=float,
                ),
                "errors": np.asarray(
                    [item.ratio_fits[label].weighted_mean_error for item in analyses],
                    dtype=float,
                ),
            }
            for label in first.ratio_fits
        }
        normalised_ratio_series = _normalised_ratio_series(weighted_ratio_series)
        normalised_scatter = calculate_normalised_scatter(normalised_ratio_series)
        if normalised_scatter:
            combined_fit_payload["normalised_flux_ratio_scatter"] = {
                label: result.as_dict() for label, result in normalised_scatter.items()
            }
            for label, result in normalised_scatter.items():
                combined_fit_rows.append({
                    "product": "normalised_flux_ratio", "label": label,
                    "parameter": "sigma_percent", "value": result.sigma_percent,
                    "error": None, "reference_frequency_ghz": None,
                    "chi_square": result.chi_square_about_unity,
                    "reduced_chi_square": result.reduced_chi_square_about_unity,
                    "degrees_of_freedom": result.degrees_of_freedom,
                    "n_points": result.n_points,
                })

        if weighted_ratio_series:
            plot_epoch_weighted_flux_ratios_vs_mjd(
                mjds, weighted_ratio_series, source,
                plots_dir / f"{stem}.weighted_flux_ratios_vs_mjd",
            )
            plot_epoch_normalized_weighted_flux_ratios_vs_mjd(
                mjds, normalised_ratio_series, source,
                plots_dir / f"{stem}.normalised_weighted_flux_ratios_vs_mjd",
                scatter_statistics=normalised_scatter,
            )

            weighted_rows: list[dict[str, Any]] = []
            normalised_rows: list[dict[str, Any]] = []
            for row_index, item in enumerate(analyses):
                weighted_row: dict[str, Any] = {
                    "epoch": item.dataset.epoch,
                    "mjd": float(item.mjd),
                }
                normalised_row: dict[str, Any] = {
                    "epoch": item.dataset.epoch,
                    "mjd": float(item.mjd),
                }
                for label, series in weighted_ratio_series.items():
                    output_label = label.replace("/", "_over_")
                    weighted_row[output_label] = float(series["values"][row_index])
                    weighted_row[f"{output_label}_error"] = float(
                        series["errors"][row_index]
                    )

                    norm_series = normalised_ratio_series[label]
                    normalised_row[output_label] = float(
                        norm_series["values"][row_index]
                    )
                    normalised_row[f"{output_label}_error"] = float(
                        norm_series["errors"][row_index]
                    )
                    normalised_row[
                        f"{output_label}_all_epoch_weighted_mean"
                    ] = float(norm_series["all_epoch_weighted_mean"])
                    normalised_row[
                        f"{output_label}_all_epoch_weighted_mean_error"
                    ] = float(norm_series["all_epoch_weighted_mean_error"])
                    scatter = normalised_scatter[label]
                    normalised_row[f"{output_label}_sigma_percent"] = float(scatter.sigma_percent)
                    normalised_row[f"{output_label}_reduced_chi_square_about_unity"] = float(scatter.reduced_chi_square_about_unity)
                weighted_rows.append(weighted_row)
                normalised_rows.append(normalised_row)

            write_csv(
                tables_dir / f"{stem}.weighted_flux_ratios_vs_mjd.csv",
                weighted_rows,
            )
            write_json(
                tables_dir / f"{stem}.weighted_flux_ratios_vs_mjd.json",
                weighted_rows,
            )
            write_csv(
                tables_dir / f"{stem}.normalised_weighted_flux_ratios_vs_mjd.csv",
                normalised_rows,
            )
            write_json(
                tables_dir / f"{stem}.normalised_weighted_flux_ratios_vs_mjd.json",
                normalised_rows,
            )

        rcusp_series = _build_rcusp_series(analyses, rcusp_images)
        if rcusp_series is not None:
            rcusp_values, rcusp_errors = rcusp_series
            rcusp_fit = fit_constant("R_cusp", rcusp_values, rcusp_errors)
            combined_fit_payload["rcusp"] = rcusp_fit.as_dict()
            combined_fit_rows.append({
                "product": "rcusp", "label": "R_cusp",
                "parameter": "constant", "value": rcusp_fit.value,
                "error": rcusp_fit.error, "reference_frequency_ghz": None,
                "chi_square": rcusp_fit.chi_square,
                "reduced_chi_square": rcusp_fit.reduced_chi_square,
                "degrees_of_freedom": rcusp_fit.degrees_of_freedom,
                "n_points": rcusp_fit.n_points,
            })
            plot_epoch_rcusp_vs_mjd(
                mjds, rcusp_values, rcusp_errors, source,
                plots_dir / f"{stem}.rcusp_vs_mjd",
                fit=rcusp_fit,
            )
            rcusp_rows = [
                {
                    "epoch": item.dataset.epoch,
                    "mjd": float(item.mjd),
                    "rcusp_a1_a2_b": float(value),
                    "rcusp_a1_a2_b_error": float(error),
                }
                for item, value, error in zip(
                    analyses, rcusp_values, rcusp_errors, strict=False
                )
            ]
            write_csv(
                tables_dir / f"{stem}.rcusp_vs_mjd.csv", rcusp_rows
            )
            write_json(
                tables_dir / f"{stem}.rcusp_vs_mjd.json", rcusp_rows
            )

        if combined_fit_rows:
            write_csv(tables_dir / f"{stem}.combined_fits.csv", combined_fit_rows)
            write_json(tables_dir / f"{stem}.combined_fits.json", combined_fit_payload)

        created = datetime.now(UTC).isoformat()
        write_json(
            working / f"{stem}.combined.stage3.metadata.json",
            {
                "schema_version": "1.0",
                "stage": 3,
                "stage3_version": STAGE3_VERSION,
                "created_utc": created,
                "source": source,
                "product_tag": product_tag,
                "analysis_tag": analysis_tag,
                "n_visits": len(analyses),
                "epochs": [analysis.dataset.epoch for analysis in analyses],
                "mjds": [analysis.mjd for analysis in analyses],
                "layout": {"columns": plotting.SUMMARY_NCOLS, "rows": "adaptive"},
                "frequency_limits_ghz": list(plotting.SUMMARY_FREQUENCY_LIMITS_GHZ),
                "spectrum_y_limits_mjy": list(spectrum_y_limits),
                "ratio_y_limits": (
                    list(ratio_y_limits) if ratio_y_limits is not None else None
                ),
                "channel_exclusion": {
                    **exclusion_configuration,
                    "effective_fit_indices_by_epoch": {
                        analysis.dataset.epoch: list(
                            analysis.excluded_fit_indices
                        )
                        for analysis in analyses
                    },
                },
                "uncertainty_model": {
                    "requested": analyses[0].error_source,
                    "per_epoch_per_group_source": {
                        analysis.dataset.epoch: analysis.uncertainty_sources
                        for analysis in analyses
                    },
                    "fallback": None,
                },
                "stage2_manifests": [
                    str(analysis.dataset.manifest_json) for analysis in analyses
                ],
                "average_spectrum_definition": (
                    "At each retained Stage 2 fit index, the plotted flux density "
                    "for each grouped image is the inverse-variance weighted mean "
                    "over all epochs that retain that index. The plotted frequency "
                    "is the mean contributing frequency."
                ),
                "average_flux_ratio_definition": (
                    "At each retained Stage 2 fit index, each plotted flux ratio "
                    "is the inverse-variance weighted mean over all epochs that "
                    "retain that index. The plotted frequency is the mean "
                    "contributing frequency."
                ),
                "reference_flux_definition": (
                    "Fitted power-law normalisation S_ref at the configured "
                    "reference frequency, plotted as a function of MJD for "
                    "each grouped image."
                ),
                "normalised_weighted_ratio_definition": (
                    "Each visit's inverse-variance weighted mean flux ratio "
                    "divided by the inverse-variance weighted mean over all "
                    "selected visits."
                ),
                "rcusp_definition": (
                    "abs(S_A1 - S_A2 + S_B) / (S_A1 + S_A2 + S_B), "
                    "using fitted reference-frequency flux densities."
                ),
                "rcusp_available": rcusp_series is not None,
                "rms_diagnostics": rms_diagnostic_metadata,
                "combined_fits": combined_fit_payload,
                "figure_formats": list(plotting.FIGURE_FORMATS),
                "provenance": {
                    "spectra": {
                        analysis.dataset.epoch: fingerprint(analysis.dataset.spectrum_csv)
                        for analysis in analyses
                    },
                },
            },
        )
        products = sorted(
            path.relative_to(working).as_posix()
            for path in working.rglob("*")
            if path.is_file()
        )
        write_json(
            working / f"{stem}.combined.stage3.manifest.json",
            {
                "schema_version": "1.0",
                "stage": 3,
                "stage3_version": STAGE3_VERSION,
                "source": source,
                "product_tag": product_tag,
                "analysis_tag": analysis_tag,
                "created_utc": created,
                "products": products,
            },
        )

        if final_output.exists():
            shutil.rmtree(final_output)
        working.replace(final_output)
    except Exception:
        if working.exists():
            shutil.rmtree(working)
        raise

    print(f"COMPLETED COMBINED: {source}.{product_tag}")
    print(f"  Visits: {len(analyses)}")
    print(f"  Plots:  {final_output / 'plots'}")
    return True




# ----------------------------------------------------------------------------
# Entry point


@dataclass
class Stage3Summary:
    analyses: int
    per_visit_written_or_skipped: int
    combined_written_or_skipped: int
    failures: int
    analysis_tag: str | None
    products: list[str]

    @property
    def ok(self) -> bool:
        return self.failures == 0


def _worker_init(
    use_tex: bool,
    frame: tuple[float, float],
    ticks: list[float],
    formats: list[str],
    error_bars: bool = True,
) -> None:
    configure_matplotlib(use_tex=use_tex)
    plotting.configure_frequency_frame(frame, ticks)
    plotting.configure_figure_formats(formats)
    plotting.configure_error_bars(error_bars)


def run_stage3(
    project_root: Path,
    config: LenspipeConfig,
    *,
    epochs: set[str] | None = None,
    products: set[str] | None = None,
    overwrite: bool = False,
    workers: int | None = None,
    reporter: Reporter | None = None,
) -> Stage3Summary:
    """Fit every selected Stage 2 product and write per-visit and combined outputs."""
    reporter = reporter or Reporter()
    settings = config.stage3
    root = Layout.at(project_root).root

    global_excluded = (
        _parse_index_spec(settings.exclude_channels, "stage3.exclude_channels")
        if settings.exclude_channels else []
    )
    epoch_excluded = _parse_epoch_exclusion_specs(settings.exclude_epoch_channels)
    analysis_tag = _analysis_tag_from_settings(global_excluded, epoch_excluded, settings.error_source)
    exclusion_configuration = _exclusion_configuration(
        settings.exclude_channels, global_excluded, settings.exclude_epoch_channels, epoch_excluded
    )

    if settings.fit_method == "emcee":
        try:
            import emcee  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Bayesian fitting requested but emcee is not installed "
                "(pip install lenspipe[emcee])."
            ) from exc

    configure_matplotlib(use_tex=settings.use_tex)
    plotting.configure_frequency_frame(settings.frequency_frame_ghz, settings.frequency_ticks_ghz)
    plotting.configure_figure_formats(settings.figure_formats)
    plotting.configure_error_bars(settings.plot_error_bars)

    datasets = discover_stage2_datasets(
        root, requested_epochs=epochs or None, requested_products=products or None
    )
    selected_epochs = {dataset.epoch for dataset in datasets}
    unknown = sorted(set(epoch_excluded) - selected_epochs)
    if unknown:
        raise ValueError(
            f"stage3.exclude_epoch_channels refers to epoch(s) not selected: {unknown}. "
            f"Selected epochs: {sorted(selected_epochs)}"
        )
    if analysis_tag:
        datasets = [
            replace(dataset, output_directory=dataset.output_directory / analysis_tag)
            for dataset in datasets
        ]

    emcee_settings = {
        "n_walkers": settings.emcee.walkers,
        "n_steps": settings.emcee.steps,
        "burn_in": settings.emcee.burn_in,
        "thin": settings.emcee.thin,
        "seed": settings.emcee.seed,
        "alpha_min": settings.emcee.alpha_prior_min,
        "alpha_max": settings.emcee.alpha_prior_max,
    }

    reporter.log(
        f"[stage3] products={sorted({d.product_tag for d in datasets})} visits={len(datasets)} "
        f"errors={settings.error_source} fit={settings.fit_method}"
        + (f" tag={analysis_tag}" if analysis_tag else "")
    )

    analyses: list[EpochAnalysis] = []
    failures = 0
    for position, dataset in enumerate(datasets, start=1):
        reporter.progress("stage3 analysis", position - 1, len(datasets), dataset.prefix)
        try:
            effective = set(global_excluded)
            effective.update(epoch_excluded.get(dataset.epoch, []))
            analyses.append(
                analyse_dataset(
                    dataset,
                    reference_frequency_ghz=settings.reference_frequency_ghz,
                    fit_method=settings.fit_method,
                    emcee_settings=emcee_settings,
                    error_source=settings.error_source,
                    excluded_fit_indices=effective or None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report and continue with other visits
            failures += 1
            reporter.error(f"analysis failed for {dataset.prefix}: {exc}")
    reporter.progress("stage3 analysis", len(datasets), len(datasets), "fits complete")

    grouped: dict[tuple[str, str], list[EpochAnalysis]] = defaultdict(list)
    for analysis in analyses:
        grouped[(analysis.dataset.source, analysis.dataset.product_tag)].append(analysis)

    written = 0
    combined_written = 0
    products_out: list[str] = []
    plot_workers = max(1, workers or config.run.plot_workers)

    for (source, product_tag), group in sorted(grouped.items()):
        group.sort(key=lambda item: item.mjd)
        records = [analysis.plot_record() for analysis in group]
        try:
            spectrum_y_limits = calculate_spectrum_y_limits(records)
            ratio_y_limits = calculate_ratio_y_limits(records)
        except Exception as exc:  # noqa: BLE001
            failures += len(group)
            reporter.error(f"limit calculation failed for {source}.{product_tag}: {exc}")
            continue

        common = dict(
            reference_frequency_ghz=settings.reference_frequency_ghz,
            overwrite=overwrite,
            annotate_fits=settings.annotations,
            fit_method=settings.fit_method,
            emcee_settings=emcee_settings,
            spectrum_y_limits=spectrum_y_limits,
            ratio_y_limits=ratio_y_limits,
            exclusion_configuration=exclusion_configuration,
            analysis_tag=analysis_tag,
        )

        label = f"stage3 plots {source}.{product_tag}"
        reporter.progress(label, 0, len(group), "per-visit products")
        if plot_workers == 1 or len(group) == 1:
            for done, analysis in enumerate(group, start=1):
                try:
                    write_epoch_products(analysis, **common)
                    written += 1
                    products_out.append(str(analysis.dataset.output_directory))
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    reporter.error(f"writing failed for {analysis.dataset.prefix}: {exc}")
                reporter.progress(label, done, len(group), analysis.dataset.prefix)
        else:
            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(
                max_workers=min(plot_workers, len(group)),
                mp_context=context,
                initializer=_worker_init,
                initargs=(
                    settings.use_tex,
                    settings.frequency_frame_ghz,
                    settings.frequency_ticks_ghz,
                    settings.figure_formats,
                    settings.plot_error_bars,
                ),
            ) as pool:
                futures = {
                    pool.submit(write_epoch_products, analysis, **common): analysis
                    for analysis in group
                }
                done = 0
                for future in futures:
                    analysis = futures[future]
                    try:
                        future.result()
                        written += 1
                        products_out.append(str(analysis.dataset.output_directory))
                    except Exception as exc:  # noqa: BLE001
                        failures += 1
                        reporter.error(f"writing failed for {analysis.dataset.prefix}: {exc}")
                    done += 1
                    reporter.progress(label, done, len(group), analysis.dataset.prefix)

        try:
            write_combined_products(
                group,
                reference_frequency_ghz=settings.reference_frequency_ghz,
                overwrite=overwrite,
                spectrum_y_limits=spectrum_y_limits,
                ratio_y_limits=ratio_y_limits,
                exclusion_configuration=exclusion_configuration,
                analysis_tag=analysis_tag,
                rcusp_images=settings.rcusp_images,
            )
            combined_written += 1
            combined_dir = root / "stage3" / "combined" / source / product_tag
            products_out.append(str(combined_dir / analysis_tag if analysis_tag else combined_dir))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            reporter.error(f"combined products failed for {source}.{product_tag}: {exc}")

    reporter.log(
        f"[stage3] analyses={len(analyses)} per-visit={written} combined={combined_written} "
        f"failures={failures}"
    )
    return Stage3Summary(
        analyses=len(analyses),
        per_visit_written_or_skipped=written,
        combined_written_or_skipped=combined_written,
        failures=failures,
        analysis_tag=analysis_tag,
        products=products_out,
    )
