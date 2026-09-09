#!/usr/bin/env python3
"""Publication-style plotting for DifMAP Spectral Pipeline Stage 3."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import matplotlib as mpl
mpl.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from stage3_fitting import power_law


COLOURS = [
    "tab:blue", "tab:orange", "tab:green", "tab:pink",
    "tab:brown", "tab:purple", "tab:gray", "tab:red", "tab:olive",
]

SUMMARY_NCOLS = 4
SUMMARY_PANEL_WIDTH_INCHES = 3.5
SUMMARY_PANEL_HEIGHT_INCHES = 3.6

# Established Stage 3 display range for the 12--18 GHz JVLA data.
SUMMARY_DATA_FREQUENCY_RANGE_GHZ = (12.0, 18.0)
SUMMARY_FREQUENCY_LIMITS_GHZ = (11.7, 18.3)
SUMMARY_FREQUENCY_TICKS_GHZ = np.arange(12.0, 19.0, 1.0)


def _format_mjd(mjd: float) -> str:
    """Format MJD to four decimal places."""
    return f"{float(mjd):.4f}"


def _format_value_uncertainty(
    value: float,
    uncertainty: float,
    uncertainty_significant_figures: int = 2,
) -> tuple[str, str]:
    """Format a fitted value and uncertainty at matched precision."""
    value = float(value)
    uncertainty = abs(float(uncertainty))

    if not np.isfinite(value) or not np.isfinite(uncertainty):
        return f"{value:.6g}", f"{uncertainty:.2g}"
    if uncertainty == 0.0:
        return f"{value:.6g}", "0"

    order = int(np.floor(np.log10(uncertainty)))
    decimal_places = uncertainty_significant_figures - 1 - order
    rounded_uncertainty = round(uncertainty, decimal_places)

    if rounded_uncertainty > 0.0:
        rounded_order = int(np.floor(np.log10(rounded_uncertainty)))
        if rounded_order != order:
            order = rounded_order
            decimal_places = uncertainty_significant_figures - 1 - order
            rounded_uncertainty = round(uncertainty, decimal_places)

    rounded_value = round(value, decimal_places)
    if decimal_places > 0:
        return (
            f"{rounded_value:.{decimal_places}f}",
            f"{rounded_uncertainty:.{decimal_places}f}",
        )
    return f"{rounded_value:.0f}", f"{rounded_uncertainty:.0f}"


def configure_matplotlib(use_tex: bool = False, font_size: float = 11.0) -> None:
    """Configure the established Stage 3 publication style."""
    mpl.rcParams.update({
        "font.family": "STIXGeneral",
        "font.serif": [
            "STIXGeneral",
            "Times New Roman",
            "Times",
            "DejaVu Serif",
        ],
        "mathtext.fontset": "stix",
        "font.size": font_size,
        "axes.labelsize": 14,
        "axes.titlesize": 13,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "legend.fontsize": 10,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.grid": False,
        "text.usetex": use_tex,
    })
    if use_tex:
        mpl.rcParams["font.family"] = "serif"
        mpl.rcParams["font.serif"] = ["Times", "Times New Roman", "STIXGeneral"]
        mpl.rcParams["text.latex.preamble"] = (
            r"\usepackage{newtxtext}\usepackage{newtxmath}"
        )


def correct_tick_marks(axis) -> None:
    """Apply inward-facing major ticks on all four sides, without minor ticks."""
    axis.tick_params(
        axis="both",
        which="major",
        direction="in",
        length=5.0,
        width=1.0,
        top=True,
        right=True,
    )
    axis.minorticks_off()


def _save_figure(fig, base_path: Path, dpi: int = 300) -> None:
    """Save matching PDF and PNG versions of a figure."""
    base_path = Path(base_path)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(Path(f"{base_path}.pdf"), bbox_inches="tight")
    fig.savefig(Path(f"{base_path}.png"), dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def _finite_values(arrays: Iterable[Any]) -> np.ndarray:
    chunks: list[np.ndarray] = []
    for values in arrays:
        array = np.asarray(values, dtype=float).ravel()
        finite = array[np.isfinite(array)]
        if finite.size:
            chunks.append(finite)
    return np.concatenate(chunks) if chunks else np.array([], dtype=float)


def _positive_quantity_limits(
    value_arrays: Iterable[Any],
    lower_fraction: float = 0.05,
    upper_fraction: float = 0.08,
) -> tuple[float, float]:
    """Return common limits for intrinsically non-negative measurements."""
    values = _finite_values(value_arrays)
    if values.size == 0:
        raise ValueError("Cannot calculate y-limits: no finite values.")

    positive_max = float(np.nanmax(values))
    scale = positive_max if positive_max > 0 else max(
        float(np.nanmax(np.abs(values))), 1.0
    )
    return -lower_fraction * scale, (1.0 + upper_fraction) * scale

def _symmetric_limits_around_centre(
    values: Any,
    centre: float,
    min_half_range: float,
    padding_fraction: float = 0.10,
) -> tuple[float, float]:
    """Return symmetric limits around a chosen central value.

    The furthest finite measurement from ``centre`` sets the half-range.
    Error-bar extents are intentionally excluded, matching the established
    Stage 3 design for normalised ratios and ``R_cusp``.
    """
    array = np.asarray(values, dtype=float).ravel()
    finite = array[np.isfinite(array)]
    max_deviation = (
        float(np.nanmax(np.abs(finite - float(centre))))
        if finite.size else 0.0
    )
    max_deviation = max(max_deviation, float(min_half_range))
    half_range = max_deviation * (1.0 + float(padding_fraction))
    return float(centre) - half_range, float(centre) + half_range


def _auto_quantity_limits(
    value_arrays: Iterable[Any],
    padding_fraction: float = 0.08,
) -> tuple[float, float]:
    """Return automatic limits for quantities that need not include zero."""
    values = _finite_values(value_arrays)
    if values.size == 0:
        raise ValueError("Cannot calculate y-limits: no finite values.")
    y_min = float(np.nanmin(values))
    y_max = float(np.nanmax(values))
    span = y_max - y_min
    if span > 0:
        padding = padding_fraction * span
    else:
        padding = max(abs(y_min), abs(y_max), 1.0) * padding_fraction
    return y_min - padding, y_max + padding


def calculate_spectrum_y_limits(
    epoch_records: Sequence[Mapping[str, Any]],
) -> tuple[float, float]:
    """Return the common flux-density limits used by every selected visit."""
    all_flux_mjy: list[np.ndarray] = []
    for record in epoch_records:
        all_flux_mjy.extend(
            np.asarray(values, dtype=float) * 1000.0
            for values in record["fluxes"].values()
        )
    return _positive_quantity_limits(all_flux_mjy)


def calculate_ratio_y_limits(
    epoch_records: Sequence[Mapping[str, Any]],
) -> tuple[float, float] | None:
    """Return the common limits used by every selected flux-ratio plot."""
    all_ratios: list[np.ndarray] = []
    for record in epoch_records:
        all_ratios.extend(record["ratios"].values())
    if not all_ratios:
        return None
    return _positive_quantity_limits(all_ratios)


def _weighted_mean(values: Any, uncertainties: Any) -> tuple[float, float]:
    y = np.asarray(values, dtype=float)
    sigma = np.asarray(uncertainties, dtype=float)
    mask = np.isfinite(y) & np.isfinite(sigma) & (sigma > 0)
    if not np.any(mask):
        return np.nan, np.nan
    weights = 1.0 / np.square(sigma[mask])
    return (
        float(np.sum(weights * y[mask]) / np.sum(weights)),
        float(np.sqrt(1.0 / np.sum(weights))),
    )


def _draw_spectrum_panel(
    ax,
    frequency_ghz,
    fluxes,
    uncertainties,
    fits,
    reference_frequency_ghz: float = 15.0,
    show_legend: bool = False,
    marker_size: float = 3.0,
    fit_linewidth: float = 1.2,
) -> None:
    """Draw one spectrum panel on an existing axis."""
    frequency = np.asarray(frequency_ghz, dtype=float)
    finite_frequency = frequency[np.isfinite(frequency)]
    if finite_frequency.size == 0:
        return

    grid = np.linspace(
        float(np.nanmin(finite_frequency)),
        float(np.nanmax(finite_frequency)),
        500,
    )

    for index, (label, values) in enumerate(fluxes.items()):
        colour = COLOURS[index % len(COLOURS)]
        y = np.asarray(values, dtype=float) * 1000.0
        yerr = np.asarray(uncertainties[label], dtype=float) * 1000.0
        mask = (
            np.isfinite(frequency)
            & np.isfinite(y)
            & np.isfinite(yerr)
            & (yerr > 0)
        )
        ax.errorbar(
            frequency[mask],
            y[mask],
            yerr=yerr[mask],
            fmt="o",
            ms=marker_size,
            capsize=0,
            linestyle="none",
            color=colour,
            label=label,
            zorder=3,
        )

        fit = fits[label]
        model_mjy = power_law(
            grid,
            fit.s_ref_jy,
            fit.alpha,
            reference_frequency_ghz,
        ) * 1000.0
        ax.plot(
            grid,
            model_mjy,
            color="black",
            linewidth=fit_linewidth,
            linestyle=(0, (5, 5)),
            zorder=4,
        )

    if show_legend:
        ax.legend(loc="best", ncol=2, frameon=False)


def _draw_ratio_panel(
    ax,
    frequency_ghz,
    ratios,
    ratio_errors,
    show_legend: bool = False,
    marker_size: float = 3.0,
    mean_linewidth: float = 1.1,
) -> None:
    """Draw measured ratios and their weighted-mean levels."""
    frequency = np.asarray(frequency_ghz, dtype=float)

    for index, (label, values) in enumerate(ratios.items()):
        colour = COLOURS[(index + 1) % len(COLOURS)]
        y = np.asarray(values, dtype=float)
        yerr = np.asarray(ratio_errors[label], dtype=float)
        mask = (
            np.isfinite(frequency)
            & np.isfinite(y)
            & np.isfinite(yerr)
            & (yerr > 0)
        )
        ax.errorbar(
            frequency[mask],
            y[mask],
            yerr=yerr[mask],
            fmt="o",
            ms=marker_size,
            capsize=0,
            linestyle="none",
            color=colour,
            label=label,
            zorder=3,
        )

        mean, _ = _weighted_mean(y, yerr)
        if np.isfinite(mean) and np.any(mask):
            x_segment = frequency[mask]
            ax.plot(
                [float(np.nanmin(x_segment)), float(np.nanmax(x_segment))],
                [mean, mean],
                color="black",
                linewidth=mean_linewidth,
                linestyle=(0, (5, 5)),
                zorder=4,
            )

    if show_legend:
        ax.legend(loc="best", frameon=False)


def plot_spectra(
    frequency_ghz,
    fluxes,
    uncertainties,
    fits,
    source,
    epoch,
    output_base,
    reference_frequency_ghz=15.0,
    annotate_fits=True,
    mjd=None,
    x_limits=SUMMARY_FREQUENCY_LIMITS_GHZ,
    y_limits=None,
) -> None:
    """Plot all lensed-image spectra for one visit."""
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)
    _draw_spectrum_panel(
        ax,
        frequency_ghz,
        fluxes,
        uncertainties,
        fits,
        reference_frequency_ghz=reference_frequency_ghz,
        show_legend=True,
        marker_size=4.5,
        fit_linewidth=1.6,
    )

    annotation_lines: list[str] = []
    for label, fit in fits.items():
        flux_text, flux_error_text = _format_value_uncertainty(
            fit.s_ref_jy * 1000.0,
            fit.s_ref_error_jy * 1000.0,
        )
        alpha_text, alpha_error_text = _format_value_uncertainty(
            fit.alpha,
            fit.alpha_error,
        )
        annotation_lines.append(
            rf"{label}: $S_{{{reference_frequency_ghz:g}}}="
            rf"{flux_text}\pm{flux_error_text}$ mJy, "
            rf"$\alpha={alpha_text}\pm{alpha_error_text}$"
        )

    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("Flux density [mJy]")
    ax.set_title(f"Epoch {epoch}" if mjd is None else f"MJD {_format_mjd(mjd)}")
    ax.set_xlim(*x_limits)
    ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)

    if y_limits is None:
        y_limits = _positive_quantity_limits(
            np.asarray(values, dtype=float) * 1000.0
            for values in fluxes.values()
        )
    ax.set_ylim(*y_limits)

    if annotate_fits:
        ax.text(
            0.03,
            0.03,
            "\n".join(annotation_lines),
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=9.0,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85},
        )

    _save_figure(fig, output_base)


def plot_flux_ratios(
    frequency_ghz,
    ratios,
    ratio_errors,
    source,
    epoch,
    output_base,
    mjd=None,
    x_limits=SUMMARY_FREQUENCY_LIMITS_GHZ,
    y_limits=None,
) -> None:
    """Plot directly measured channel-by-channel flux-density ratios."""
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)
    _draw_ratio_panel(
        ax,
        frequency_ghz,
        ratios,
        ratio_errors,
        show_legend=True,
        marker_size=4.5,
        mean_linewidth=1.4,
    )

    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("Flux ratio")
    ax.set_title(f"Epoch {epoch}" if mjd is None else f"MJD {_format_mjd(mjd)}")
    ax.set_xlim(*x_limits)
    ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)
    if y_limits is None:
        y_limits = _positive_quantity_limits(ratios.values())
    ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)


def plot_spectra_with_residuals(
    frequency_ghz,
    fluxes,
    uncertainties,
    fits,
    source,
    epoch,
    output_base,
    reference_frequency_ghz=15.0,
    mjd=None,
    x_limits=SUMMARY_FREQUENCY_LIMITS_GHZ,
    y_limits=None,
) -> None:
    """Plot spectra with fitted power laws and normalized residuals."""
    labels = list(fluxes)
    fig, (ax, residual_ax) = plt.subplots(
        2,
        1,
        figsize=(8, 9),
        sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.0], "hspace": 0.05},
    )
    correct_tick_marks(ax)
    correct_tick_marks(residual_ax)

    frequency = np.asarray(frequency_ghz, dtype=float)
    finite_frequency = frequency[np.isfinite(frequency)]
    if finite_frequency.size == 0:
        raise ValueError("No finite frequencies available for plotting.")
    grid = np.linspace(
        float(np.nanmin(finite_frequency)),
        float(np.nanmax(finite_frequency)),
        1000,
    )

    for index, label in enumerate(labels):
        colour = COLOURS[index % len(COLOURS)]
        flux = np.asarray(fluxes[label], dtype=float)
        sigma = np.asarray(uncertainties[label], dtype=float)
        fit = fits[label]
        model_at_data = power_law(
            frequency,
            fit.s_ref_jy,
            fit.alpha,
            reference_frequency_ghz,
        )
        mask = (
            np.isfinite(frequency)
            & np.isfinite(flux)
            & np.isfinite(sigma)
            & (sigma > 0)
            & np.isfinite(model_at_data)
        )
        residual_sigma = np.full_like(flux, np.nan, dtype=float)
        residual_sigma[mask] = (
            flux[mask] - model_at_data[mask]
        ) / sigma[mask]

        ax.errorbar(
            frequency[mask],
            flux[mask] * 1000.0,
            yerr=sigma[mask] * 1000.0,
            fmt="o",
            ms=4.5,
            linestyle="none",
            color=colour,
            label=label,
        )
        ax.plot(
            grid,
            power_law(
                grid,
                fit.s_ref_jy,
                fit.alpha,
                reference_frequency_ghz,
            ) * 1000.0,
            color="black",
            linewidth=1.4,
            linestyle=(0, (5, 5)),
        )
        residual_ax.plot(
            frequency[mask],
            residual_sigma[mask],
            "o",
            ms=4.0,
            color=colour,
        )

    ax.set_ylabel("Flux density [mJy]")
    ax.legend(loc="best", ncol=2, frameon=False)
    ax.set_title(f"Epoch {epoch}" if mjd is None else f"MJD {_format_mjd(mjd)}")
    ax.set_xlim(*x_limits)
    ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)

    if y_limits is None:
        y_limits = _positive_quantity_limits(
            np.asarray(values, dtype=float) * 1000.0
            for values in fluxes.values()
        )
    ax.set_ylim(*y_limits)

    residual_ax.axhline(0.0, color="black", linewidth=1.0)
    residual_ax.axhline(3.0, color="0.5", linewidth=0.8, linestyle=":")
    residual_ax.axhline(-3.0, color="0.5", linewidth=0.8, linestyle=":")
    residual_ax.set_ylabel(r"Residual [$\sigma$]")
    residual_ax.set_xlabel("Frequency [GHz]")
    _save_figure(fig, output_base)



def plot_combined_average_spectrum(
    frequency_ghz,
    averaged_flux_series,
    source,
    output_base,
    fits=None,
    reference_frequency_ghz=15.0,
    y_limits=None,
) -> None:
    """Plot the epoch-averaged grouped-image spectra in a single panel.

    Each data point is the inverse-variance weighted mean of the fitted grouped
    flux densities from all selected epochs at the corresponding Stage 2
    frequency interval.
    """
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)

    frequency = np.asarray(frequency_ghz, dtype=float)
    limit_arrays = []
    for index, (label, series) in enumerate(averaged_flux_series.items()):
        colour = COLOURS[index % len(COLOURS)]
        values = np.asarray(series["values"], dtype=float) * 1000.0
        errors = np.asarray(series["errors"], dtype=float) * 1000.0
        mask = (
            np.isfinite(frequency)
            & np.isfinite(values)
            & np.isfinite(errors)
            & (errors >= 0)
        )
        if not np.any(mask):
            continue
        legend_label = label
        if fits is not None and label in fits:
            fit = fits[label]
            s_value, s_error = _format_value_uncertainty(
                fit.s_ref_jy * 1000.0, fit.s_ref_error_jy * 1000.0
            )
            a_value, a_error = _format_value_uncertainty(
                fit.alpha, fit.alpha_error
            )
            legend_label = (
                rf"{label}: $S_{{{reference_frequency_ghz:g}}}="
                rf"{s_value}\pm{s_error}$ mJy, "
                rf"$\alpha={a_value}\pm{a_error}$"
            )
        ax.errorbar(
            frequency[mask], values[mask], yerr=errors[mask],
            fmt="o", ms=4.5, capsize=0, linestyle="none",
            color=colour, label=legend_label, zorder=3,
        )
        if fits is not None and label in fits:
            fit = fits[label]
            grid = np.linspace(
                float(np.nanmin(frequency[mask])),
                float(np.nanmax(frequency[mask])), 500
            )
            ax.plot(
                grid,
                power_law(grid, fit.s_ref_jy, fit.alpha, reference_frequency_ghz) * 1000.0,
                color=colour, linewidth=1.3, linestyle=(0, (5, 5)), zorder=2,
            )
        limit_arrays.extend([values[mask], values[mask] + errors[mask]])

    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("Flux density [mJy]")
    ax.set_title("All epochs")
    ax.legend(loc="best", frameon=False)
    ax.set_xlim(*SUMMARY_FREQUENCY_LIMITS_GHZ)
    ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)
    if y_limits is None and limit_arrays:
        y_limits = _positive_quantity_limits(limit_arrays)
    if y_limits is not None:
        ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)


def plot_combined_average_flux_ratios(
    frequency_ghz,
    averaged_ratio_series,
    source,
    output_base,
    fits=None,
    y_limits=None,
) -> None:
    """Plot channel-by-channel flux ratios averaged over all selected epochs.

    Each point is the inverse-variance weighted mean of the measured flux ratio
    at one retained Stage 2 fit index. When an epoch-specific channel mask is
    active, the point is formed from whichever epochs retain that index.
    """
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)

    frequency = np.asarray(frequency_ghz, dtype=float)
    limit_arrays = []
    for index, (label, series) in enumerate(averaged_ratio_series.items()):
        colour = COLOURS[(index + 1) % len(COLOURS)]
        values = np.asarray(series["values"], dtype=float)
        errors = np.asarray(series["errors"], dtype=float)
        mask = (
            np.isfinite(frequency)
            & np.isfinite(values)
            & np.isfinite(errors)
            & (errors >= 0)
        )
        if not np.any(mask):
            continue

        legend_label = label
        fit = fits.get(label) if fits is not None else None
        if fit is not None:
            value_text, error_text = _format_value_uncertainty(fit.value, fit.error)
            legend_label = rf"{label}: $R={value_text}\pm{error_text}$"
        ax.errorbar(
            frequency[mask],
            values[mask],
            yerr=errors[mask],
            fmt="o",
            ms=4.5,
            capsize=0,
            linestyle="none",
            color=colour,
            label=legend_label,
            zorder=3,
        )

        mean = fit.value if fit is not None else _weighted_mean(values[mask], errors[mask])[0]
        if np.isfinite(mean):
            x_values = frequency[mask]
            ax.plot(
                [float(np.nanmin(x_values)), float(np.nanmax(x_values))],
                [mean, mean],
                color=colour,
                linewidth=1.2,
                linestyle=(0, (5, 5)),
                zorder=2,
            )
        limit_arrays.extend([values[mask], values[mask] + errors[mask]])

    ax.set_xlabel("Frequency [GHz]")
    ax.set_ylabel("Flux ratio")
    ax.set_title("All epochs")
    ax.legend(loc="best", frameon=False)
    ax.set_xlim(*SUMMARY_FREQUENCY_LIMITS_GHZ)
    ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)
    if y_limits is None and limit_arrays:
        y_limits = _positive_quantity_limits(limit_arrays)
    if y_limits is not None:
        ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)

def plot_epoch_weighted_flux_ratios_vs_mjd(
    mjds,
    weighted_ratio_series,
    source,
    output_base,
    y_limits=None,
) -> None:
    """Plot per-visit weighted-average flux ratios as a function of MJD."""
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)

    mjd = np.asarray(mjds, dtype=float)
    limit_arrays = []
    for index, (label, series) in enumerate(weighted_ratio_series.items()):
        colour = COLOURS[(index + 1) % len(COLOURS)]
        values = np.asarray(series["values"], dtype=float)
        errors = np.asarray(series["errors"], dtype=float)
        mask = (
            np.isfinite(mjd)
            & np.isfinite(values)
            & np.isfinite(errors)
            & (errors >= 0)
        )
        if not np.any(mask):
            continue
        ax.errorbar(
            mjd[mask], values[mask], yerr=errors[mask],
            fmt="o", ms=4.5, capsize=0, linestyle="none",
            color=colour, label=label, zorder=3,
        )
        limit_arrays.extend([values[mask], values[mask] + errors[mask]])

    ax.set_xlabel("MJD")
    ax.set_ylabel("Flux ratio")
    ax.set_title("All epochs")
    ax.legend(loc="best", frameon=False)
    if y_limits is None and limit_arrays:
        y_limits = _auto_quantity_limits(limit_arrays)
    if y_limits is not None:
        ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)


def plot_epoch_normalized_weighted_flux_ratios_vs_mjd(
    mjds,
    normalized_ratio_series,
    source,
    output_base,
    scatter_statistics=None,
    y_limits=None,
) -> None:
    """Plot normalised weighted-average flux ratios in stacked panels.

    Each ratio is divided by its inverse-variance weighted mean over all
    selected visits. Panels are independently centred on 1.0 and use the
    measured point furthest from 1.0, plus 10 per cent padding, to set their
    symmetric vertical range.
    """
    items = list(normalized_ratio_series.items())
    if not items:
        raise ValueError("No normalized flux-ratio series were supplied.")

    n_panels = len(items)
    fig, axes = plt.subplots(
        n_panels,
        1,
        figsize=(8, max(2.30 * n_panels, 4.5)),
        sharex=True,
        squeeze=False,
        gridspec_kw={"hspace": 0.0},
    )
    axes = axes.ravel()

    mjd = np.asarray(mjds, dtype=float)
    finite_mjd = mjd[np.isfinite(mjd)]
    panel_limits = []

    for panel_index, (axis, (label, series)) in enumerate(zip(axes, items)):
        correct_tick_marks(axis)
        colour = COLOURS[(panel_index + 1) % len(COLOURS)]
        values = np.asarray(series["values"], dtype=float)
        errors = np.asarray(series["errors"], dtype=float)
        mask = (
            np.isfinite(mjd)
            & np.isfinite(values)
            & np.isfinite(errors)
            & (errors >= 0)
        )

        if np.any(mask):
            axis.errorbar(
                mjd[mask], values[mask], yerr=errors[mask],
                fmt="o", ms=4.5, capsize=0, linestyle="none",
                color=colour, zorder=3,
            )
            panel_limits.append(
                _symmetric_limits_around_centre(
                    values[mask], centre=1.0,
                    min_half_range=0.02, padding_fraction=0.10,
                )
            )
            x0 = float(np.nanmin(mjd[mask]))
            x1 = float(np.nanmax(mjd[mask]))
        elif finite_mjd.size:
            panel_limits.append((0.95, 1.05))
            x0 = float(np.nanmin(finite_mjd))
            x1 = float(np.nanmax(finite_mjd))
        else:
            panel_limits.append((0.95, 1.05))
            x0, x1 = 0.0, 1.0

        axis.plot(
            [x0, x1], [1.0, 1.0], color="black",
            linewidth=1.2, linestyle=(0, (5, 5)), zorder=2,
        )
        annotation = label
        if scatter_statistics is not None and label in scatter_statistics:
            statistic = scatter_statistics[label]
            annotation = (
                f"{label}\n"
                rf"$\sigma={statistic.sigma_percent:.2f}\%$"
            )
        axis.text(
            0.98, 0.92, annotation, transform=axis.transAxes,
            ha="right", va="top", fontsize=10,
        )
        if panel_index < n_panels - 1:
            axis.tick_params(labelbottom=False)

    if y_limits is not None:
        for axis in axes:
            axis.set_ylim(*y_limits)
    else:
        for axis, limits in zip(axes, panel_limits):
            axis.set_ylim(*limits)

    fig.subplots_adjust(
        left=0.12, right=0.98, bottom=0.10, top=0.98, hspace=0.0
    )

    # Use an ordinary x-axis label on the bottom panel. This gives exactly
    # the same label-to-tick spacing as the single-panel MJD plots.
    axes[-1].set_xlabel("MJD")

    # Measure where Matplotlib places an ordinary y-axis label for these
    # panels, then use that measured x-position for one label centred on the
    # complete stack. This preserves the standard label-to-tick spacing while
    # avoiding the canvas-centred placement of fig.supylabel().
    for axis in axes:
        axis.set_ylabel("Flux ratio")
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    label_centres_x = []
    for axis in axes:
        bbox_display = axis.yaxis.label.get_window_extent(renderer=renderer)
        bbox_figure = bbox_display.transformed(fig.transFigure.inverted())
        label_centres_x.append(0.5 * (bbox_figure.x0 + bbox_figure.x1))
    ylabel_fontproperties = axes[0].yaxis.label.get_fontproperties().copy()
    for axis in axes:
        axis.set_ylabel("")

    positions = [axis.get_position() for axis in axes]
    axes_bottom = min(position.y0 for position in positions)
    axes_top = max(position.y1 for position in positions)
    axes_y_center = 0.5 * (axes_bottom + axes_top)
    shared_y_label_x = min(label_centres_x)
    fig.text(
        shared_y_label_x,
        axes_y_center,
        "Flux ratio",
        rotation=90,
        ha="center",
        va="center",
        fontproperties=ylabel_fontproperties,
    )
    _save_figure(fig, output_base)


def plot_epoch_reference_fluxes_vs_mjd(
    mjds,
    reference_flux_series,
    source,
    output_base,
    reference_frequency_ghz=15.0,
    y_limits=None,
) -> None:
    """Plot fitted reference-frequency flux densities as a function of MJD.

    This follows the same single-panel style as the weighted flux-ratio versus
    MJD plot: one figure containing all grouped images, distinguished by
    colour and legend entry. Flux densities are shown in mJy.
    """
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)

    mjd = np.asarray(mjds, dtype=float)
    limit_arrays = []
    for index, (label, series) in enumerate(reference_flux_series.items()):
        colour = COLOURS[index % len(COLOURS)]
        values = np.asarray(series["values"], dtype=float) * 1000.0
        errors = np.asarray(series["errors"], dtype=float) * 1000.0
        mask = (
            np.isfinite(mjd)
            & np.isfinite(values)
            & np.isfinite(errors)
            & (errors >= 0)
        )
        if not np.any(mask):
            continue
        ax.errorbar(
            mjd[mask], values[mask], yerr=errors[mask],
            fmt="o", ms=4.5, capsize=0, linestyle="none",
            color=colour, label=label, zorder=3,
        )
        limit_arrays.extend([values[mask], values[mask] + errors[mask]])

    ax.set_xlabel("MJD")
    ax.set_ylabel("Flux density [mJy]")
    ax.set_title("All epochs")
    ax.legend(loc="best", frameon=False)
    if y_limits is None and limit_arrays:
        y_limits = _auto_quantity_limits(limit_arrays)
    if y_limits is not None:
        ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)


def plot_epoch_rcusp_vs_mjd(
    mjds,
    rcusp_values,
    rcusp_errors,
    source,
    output_base,
    fit=None,
    y_limits=None,
) -> None:
    """Plot the cusp relation ``R_cusp(A1, A2, B)`` versus MJD."""
    fig, ax = plt.subplots(figsize=(8, 8))
    correct_tick_marks(ax)

    mjd = np.asarray(mjds, dtype=float)
    values = np.asarray(rcusp_values, dtype=float)
    errors = np.asarray(rcusp_errors, dtype=float)
    mask = (
        np.isfinite(mjd)
        & np.isfinite(values)
        & np.isfinite(errors)
        & (errors >= 0)
    )

    if np.any(mask):
        ax.errorbar(
            mjd[mask], values[mask], yerr=errors[mask],
            fmt="o", ms=4.5, capsize=0, linestyle="none",
            color=COLOURS[0], zorder=3,
        )

        if fit is not None and np.isfinite(fit.value):
            mean_value = float(fit.value)
        else:
            weights = np.zeros_like(errors[mask], dtype=float)
            positive_error = errors[mask] > 0
            weights[positive_error] = 1.0 / np.square(errors[mask][positive_error])
            if np.any(weights > 0):
                mean_value = float(np.sum(weights * values[mask]) / np.sum(weights))
            else:
                mean_value = float(np.nanmean(values[mask]))

        x0 = float(np.nanmin(mjd[mask]))
        x1 = float(np.nanmax(mjd[mask]))
        ax.plot(
            [x0, x1], [mean_value, mean_value], color="black",
            linewidth=1.2, linestyle=(0, (5, 5)), zorder=2,
        )
        if y_limits is None:
            y_limits = _symmetric_limits_around_centre(
                values[mask], centre=mean_value,
                min_half_range=0.01, padding_fraction=0.10,
            )
        if fit is not None and np.isfinite(fit.value):
            value_text, error_text = _format_value_uncertainty(fit.value, fit.error)
            ax.text(
                0.98, 0.96,
                rf"$R_{{\rm cusp}}={value_text}\pm{error_text}$",
                transform=ax.transAxes, ha="right", va="top", fontsize=11,
            )

    ax.set_xlabel("MJD")
    ax.set_ylabel(r"$R_{\rm cusp}$")
    ax.set_title("All epochs")
    if y_limits is not None:
        ax.set_ylim(*y_limits)
    _save_figure(fig, output_base)


def _summary_grid_shape(n_records: int) -> tuple[int, int]:
    """Return an adaptive four-column grid for the requested visit count."""
    if n_records < 1:
        raise ValueError("At least one visit is required for a summary grid.")
    n_rows = int(np.ceil(n_records / SUMMARY_NCOLS))
    return n_rows, SUMMARY_NCOLS


def _summary_grid_figure_size(n_rows: int) -> tuple[float, float]:
    """Keep individual panel dimensions stable as the row count changes."""
    width = SUMMARY_NCOLS * SUMMARY_PANEL_WIDTH_INCHES
    height = max(4.5, n_rows * SUMMARY_PANEL_HEIGHT_INCHES)
    return width, height


def plot_all_epochs_spectra(
    epoch_records: Sequence[Mapping[str, Any]],
    output_base,
    reference_frequency_ghz: float = 15.0,
    x_limits: tuple[float, float] = SUMMARY_FREQUENCY_LIMITS_GHZ,
    y_limits: tuple[float, float] | None = None,
) -> None:
    """Create an adaptive four-column all-visit spectrum summary."""
    records = sorted(epoch_records, key=lambda item: float(item["mjd"]))
    if not records:
        raise ValueError("No epoch records supplied.")
    if y_limits is None:
        y_limits = calculate_spectrum_y_limits(records)

    n_rows, n_cols = _summary_grid_shape(len(records))
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        figsize=_summary_grid_figure_size(n_rows),
        sharex=False,
        sharey=False,
        squeeze=False,
    )
    axes_flat = axes.ravel()
    legend_handles = None
    legend_labels = None

    for index, record in enumerate(records):
        ax = axes_flat[index]
        correct_tick_marks(ax)
        _draw_spectrum_panel(
            ax,
            record["frequency_ghz"],
            record["fluxes"],
            record["uncertainties"],
            record["fits"],
            reference_frequency_ghz=reference_frequency_ghz,
            show_legend=False,
            marker_size=2.5,
            fit_linewidth=1.0,
        )
        ax.set_xlim(*x_limits)
        ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)
        ax.set_ylim(*y_limits)
        ax.set_title(f"MJD {_format_mjd(record['mjd'])}", fontsize=10.5)
        ax.set_xlabel("Frequency [GHz]", fontsize=10)
        ax.set_ylabel("Flux density [mJy]", fontsize=10)
        ax.tick_params(axis="both", which="major", labelsize=9)
        if legend_handles is None:
            legend_handles, legend_labels = ax.get_legend_handles_labels()

    for ax in axes_flat[len(records):]:
        ax.set_axis_off()

    if legend_handles:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="upper center",
            ncol=min(len(legend_labels), 6),
            frameon=False,
            bbox_to_anchor=(0.5, 0.995),
        )

    fig.subplots_adjust(
        left=0.07,
        right=0.985,
        bottom=max(0.055, 0.07 / n_rows),
        top=0.945,
        wspace=0.38,
        hspace=0.42,
    )
    _save_figure(fig, output_base)


def plot_all_epochs_flux_ratios(
    epoch_records: Sequence[Mapping[str, Any]],
    output_base,
    x_limits: tuple[float, float] = SUMMARY_FREQUENCY_LIMITS_GHZ,
    y_limits: tuple[float, float] | None = None,
) -> None:
    """Create an adaptive four-column all-visit flux-ratio summary."""
    records = sorted(epoch_records, key=lambda item: float(item["mjd"]))
    if not records:
        raise ValueError("No epoch records supplied.")
    if y_limits is None:
        y_limits = calculate_ratio_y_limits(records)
    if y_limits is None:
        raise ValueError("No flux-ratio measurements are available.")

    n_rows, n_cols = _summary_grid_shape(len(records))
    fig, axes = plt.subplots(
        n_rows,
        n_cols,
        figsize=_summary_grid_figure_size(n_rows),
        sharex=False,
        sharey=False,
        squeeze=False,
    )
    axes_flat = axes.ravel()
    legend_handles = None
    legend_labels = None

    for index, record in enumerate(records):
        ax = axes_flat[index]
        correct_tick_marks(ax)
        _draw_ratio_panel(
            ax,
            record["frequency_ghz"],
            record["ratios"],
            record["ratio_errors"],
            show_legend=False,
            marker_size=2.5,
            mean_linewidth=1.0,
        )
        ax.set_xlim(*x_limits)
        ax.set_xticks(SUMMARY_FREQUENCY_TICKS_GHZ)
        ax.set_ylim(*y_limits)
        ax.set_title(f"MJD {_format_mjd(record['mjd'])}", fontsize=10.5)
        ax.set_xlabel("Frequency [GHz]", fontsize=10)
        ax.set_ylabel("Flux ratio", fontsize=10)
        ax.tick_params(axis="both", which="major", labelsize=9)
        if legend_handles is None:
            legend_handles, legend_labels = ax.get_legend_handles_labels()

    for ax in axes_flat[len(records):]:
        ax.set_axis_off()

    if legend_handles:
        fig.legend(
            legend_handles,
            legend_labels,
            loc="upper center",
            ncol=min(len(legend_labels), 6),
            frameon=False,
            bbox_to_anchor=(0.5, 0.995),
        )

    fig.subplots_adjust(
        left=0.07,
        right=0.985,
        bottom=max(0.055, 0.07 / n_rows),
        top=0.945,
        wspace=0.38,
        hspace=0.42,
    )
    _save_figure(fig, output_base)


# Backwards-compatible aliases for earlier Stage 3 function names.
# The stable public names are plot_all_epochs_spectra and
# plot_all_epochs_flux_ratios; all aliases use the same adaptive 4-column grid.
plot_all_epochs_spectra_grid = plot_all_epochs_spectra
plot_all_epochs_flux_ratios_grid = plot_all_epochs_flux_ratios
plot_all_epochs_spectra_4col = plot_all_epochs_spectra
plot_all_epochs_flux_ratios_4col = plot_all_epochs_flux_ratios
plot_all_epochs_spectra_5x4 = plot_all_epochs_spectra
plot_all_epochs_flux_ratios_5x4 = plot_all_epochs_flux_ratios
