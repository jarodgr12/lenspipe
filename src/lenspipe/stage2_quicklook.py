"""Quick-look PNGs for a Stage 2 spectrum CSV (same figures as the legacy script)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

__all__ = ["write_quicklook_plots"]

PLOT_ERROR_BARS = True
PLOT_DPI = 200


def _title(data: pd.DataFrame, source: str, epoch: str, description: str) -> str:
    title = f"{source}.{epoch} {description}"
    if "mjd" in data.columns:
        mjd_values = pd.to_numeric(data["mjd"], errors="coerce").dropna()
        if not mjd_values.empty:
            title += f" (MJD {float(mjd_values.iloc[0]):.2f})"
    return title


def _good_rows(data: pd.DataFrame, labels: list[str]) -> pd.DataFrame:
    good = data.loc[data["fit_status"].eq("ok")].copy()
    good["frequency_ghz"] = pd.to_numeric(good["frequency_ghz"], errors="coerce")
    good["rms_jy_per_beam"] = pd.to_numeric(good["rms_jy_per_beam"], errors="coerce")
    for label in labels:
        good[label] = pd.to_numeric(good[label], errors="coerce")
    return good.dropna(subset=["frequency_ghz"]).sort_values("frequency_ghz")


def _plot_lines(
    data: pd.DataFrame, labels: list[str], plot_path: Path, ylabel: str, title: str
) -> None:
    good = _good_rows(data, labels)
    if good.empty:
        raise ValueError("No successful finite measurements are available to plot.")
    fig, ax = plt.subplots(figsize=(8, 5))
    for label in labels:
        finite = good[["frequency_ghz", label, "rms_jy_per_beam"]].dropna(
            subset=["frequency_ghz", label]
        )
        if finite.empty:
            continue
        display = label.removesuffix("_jy")
        if PLOT_ERROR_BARS and finite["rms_jy_per_beam"].notna().all():
            ax.errorbar(
                finite["frequency_ghz"], finite[label], yerr=finite["rms_jy_per_beam"],
                fmt="o-", linewidth=1, markersize=4, capsize=2, label=display,
            )
        else:
            ax.plot(finite["frequency_ghz"], finite[label], "o-", linewidth=1, markersize=4, label=display)
    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=PLOT_DPI, bbox_inches="tight")
    plt.close(fig)


def _plot_ratios(
    data: pd.DataFrame, grouped_labels: list[str], plot_path: Path, title: str
) -> None:
    if not grouped_labels:
        raise ValueError("No grouped components are available for ratio plotting.")
    reference_label = grouped_labels[0]
    reference_name = reference_label.removesuffix("_jy")
    good = data.loc[data["fit_status"].eq("ok")].copy()
    good["frequency_ghz"] = pd.to_numeric(good["frequency_ghz"], errors="coerce")
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
        ax.plot(
            finite["frequency_ghz"], finite[label] / finite[reference_label], "o-",
            linewidth=1, markersize=4, label=f"{label.removesuffix('_jy')}/{reference_name}",
        )
        plotted += 1
    if plotted == 0:
        plt.close(fig)
        raise ValueError("No non-reference grouped components are available.")
    ax.set_xlabel("Frequency (GHz)")
    ax.set_ylabel(f"Flux-density ratio relative to {reference_name}")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=PLOT_DPI, bbox_inches="tight")
    plt.close(fig)


def write_quicklook_plots(
    *,
    csv_path: Path,
    spectrum_png: Path,
    grouped_png: Path,
    ratio_png: Path,
    output_labels: list[str],
    grouped_labels: list[str],
    source: str,
    epoch: str,
    warn: Callable[[str], None],
) -> None:
    data = pd.read_csv(csv_path)
    required = {"frequency_ghz", "rms_jy_per_beam", "fit_status"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError("Missing required plotting column(s): " + ", ".join(sorted(missing)))

    jobs: list[tuple[str, Callable[[], None]]] = [
        (
            "spectrum",
            lambda: _plot_lines(
                data, output_labels, spectrum_png, "Flux density (Jy)",
                _title(data, source, epoch, "spectrum"),
            ),
        ),
        (
            "grouped spectrum",
            lambda: _plot_lines(
                data, grouped_labels, grouped_png, "Combined flux density (Jy)",
                _title(data, source, epoch, "grouped-component spectra"),
            ),
        ),
    ]
    if len(grouped_labels) > 1:
        jobs.append(
            (
                "flux ratios",
                lambda: _plot_ratios(
                    data, grouped_labels, ratio_png,
                    _title(data, source, epoch, "grouped-component flux ratios"),
                ),
            )
        )
    for name, job in jobs:
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - advisory plots
            warn(f"could not create {name} plot: {exc}")
