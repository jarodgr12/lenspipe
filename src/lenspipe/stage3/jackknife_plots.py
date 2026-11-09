"""Jackknife figures, drawn in the ``_mnras`` style.

No titles and no annotations, axis labels and tick labels ``MNRAS_FONT_STEP``
points larger than the standard figures, and any key inside the axes where it
covers no data (``plotting.legend_inside_clear``). Stacked panels name their
quantity on the y-axis, so they need no key; the per-epoch spectra grid labels
each panel with its MJD above the frame and keys the images once, in one row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.font_manager import FontProperties
from matplotlib.ticker import MaxNLocator
from numpy.typing import ArrayLike

from lenspipe.stage3 import plotting
from lenspipe.stage3.plotting import (
    COLOURS,
    correct_tick_marks,
    draw_points,
    drop_edge_yticks,
    legend_inside_clear,
    mnras_axis_fonts,
)

__all__ = ["plot_jackknife_histograms", "plot_jackknife_spectra_grid", "plot_jackknife_stack"]


def _bins(values: np.ndarray) -> np.ndarray | int:
    finite = values[np.isfinite(values)]
    if finite.size < 2 or np.ptp(finite) == 0:
        return 1
    return np.histogram_bin_edges(finite, bins="auto")


def plot_jackknife_histograms(
    samples: Mapping[str, ArrayLike],
    x_labels: Mapping[str, str],
    output_base: Path,
    colour_offset: int = 0,
) -> None:
    """One histogram panel per quantity: its value in every jackknife sample."""
    items = list(samples.items())
    if not items:
        return
    fig, axes = plt.subplots(1, len(items), figsize=(4.2 * len(items), 4.2), squeeze=False)
    for index, (ax, (label, values)) in enumerate(zip(axes.ravel(), items, strict=False)):
        correct_tick_marks(ax)
        data = np.asarray(values, dtype=float)
        ax.hist(
            data[np.isfinite(data)], bins=_bins(data), color=COLOURS[(index + colour_offset) % len(COLOURS)],
            alpha=0.8, edgecolor="black", linewidth=0.8,
        )
        ax.set_xlabel(x_labels[label])
        if index == 0:
            ax.set_ylabel("Count")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        mnras_axis_fonts(ax)
    fig.tight_layout()
    plotting.save_figure(fig, output_base)


def plot_jackknife_stack(
    mjds: ArrayLike,
    series: Mapping[str, tuple[ArrayLike, ArrayLike]],
    y_labels: Mapping[str, str],
    output_base: Path,
    colour_offset: int = 0,
    centre: float | None = None,
) -> None:
    """Stacked panels sharing the MJD axis: one quantity per panel, jackknife mean and error per epoch.

    ``centre`` (1.0 for normalised series) puts every panel's y-axis symmetric
    about that value, wide enough for the furthest point plus its error bar.
    """
    items = list(series.items())
    if not items:
        return
    mjd = np.asarray(mjds, dtype=float)
    fig, axes = plt.subplots(
        len(items), 1, figsize=(8, max(2.3 * len(items), 4.5)), sharex=True, squeeze=False,
        gridspec_kw={"hspace": 0.0},
    )
    for index, (ax, (label, (values, errors))) in enumerate(zip(axes.ravel(), items, strict=False)):
        correct_tick_marks(ax)
        y = np.asarray(values, dtype=float)
        sigma = np.asarray(errors, dtype=float)
        mask = np.isfinite(mjd) & np.isfinite(y)
        if np.any(mask):
            draw_points(
                ax, mjd[mask], y[mask], plotting._yerr(np.where(np.isfinite(sigma), sigma, 0.0)[mask]),
                color=COLOURS[(index + colour_offset) % len(COLOURS)], ms=4.5,
            )
        if centre is not None and np.any(mask):
            reach = np.abs(y[mask] - centre) + np.where(np.isfinite(sigma[mask]), sigma[mask], 0.0)
            half = max(float(np.max(reach)) * 1.15, 1e-9)
            ax.set_ylim(centre - half, centre + half)
        else:
            ax.margins(y=0.15)
        ax.set_ylabel(y_labels[label])
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4))
        drop_edge_yticks(ax)
        mnras_axis_fonts(ax)
    axes.ravel()[-1].set_xlabel("MJD")
    axes.ravel()[-1].xaxis.label.set_fontsize(axes.ravel()[0].yaxis.label.get_fontsize())
    fig.align_ylabels(list(axes.ravel()))
    plotting.save_figure(fig, output_base)


def plot_jackknife_spectra_grid(
    epochs: Sequence[tuple[float, Mapping[str, tuple[np.ndarray, np.ndarray, np.ndarray]]]],
    output_base: Path,
) -> None:
    """Per-epoch image spectra (jackknife mean and error per channel), one panel per epoch.

    ``epochs`` is ``[(MJD, {image: (frequency_ghz, flux_mjy, error_mjy)})]``. Panels
    share both axes and are labelled with their MJD above the frame; the images are
    keyed once, in a single horizontal row inside the first panel, clear of the data.
    """
    if not epochs:
        return
    n_cols = min(plotting.SUMMARY_NCOLS, len(epochs))
    n_rows = int(np.ceil(len(epochs) / n_cols))
    fig, axes = plt.subplots(
        n_rows, n_cols, figsize=(plotting.SUMMARY_PANEL_WIDTH_INCHES * n_cols, plotting.SUMMARY_PANEL_HEIGHT_INCHES * n_rows),
        sharex=True, sharey=True, squeeze=False,
    )
    flat = axes.ravel()
    label_size = FontProperties(size=mpl.rcParams["xtick.labelsize"]).get_size_in_points() + plotting.MNRAS_FONT_STEP
    for panel, (ax, (mjd, images)) in enumerate(zip(flat, epochs, strict=False)):
        correct_tick_marks(ax)
        for index, (name, (frequency, flux, error)) in enumerate(images.items()):
            draw_points(
                ax, frequency, flux, plotting._yerr(np.where(np.isfinite(error), error, 0.0)),
                color=COLOURS[index % len(COLOURS)], label=name if panel == 0 else None, ms=3.0,
            )
        ax.set_xlim(*plotting.SUMMARY_FREQUENCY_LIMITS_GHZ)
        mnras_axis_fonts(ax)
        ax.set_title(f"MJD {plotting._format_mjd(mjd)}", fontsize=label_size)  # outside the frame, as asked
    for ax in flat[len(epochs):]:
        fig.delaxes(ax)
    size = flat[0].xaxis.label.get_fontsize()
    fig.supxlabel("Frequency [GHz]", fontsize=size)
    fig.supylabel("Flux density [mJy]", fontsize=size)
    fig.tight_layout()
    n_images = len(epochs[0][1])
    legend_inside_clear(flat[0], ncol=max(1, n_images))
    plotting.save_figure(fig, output_base)
