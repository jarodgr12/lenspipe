"""Interactive Stage 3 figures for the console (Plotly figure dictionaries).

The publication figures are static PDFs and PNGs. These are the same data as
hoverable plots: every point carries its fit index, spectral window and
channel so a feature in a spectrum can be traced back to the visibilities.
Figures are plain ``dict`` objects in Plotly's JSON schema; no plotting
library is needed on the server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from lenspipe.stage3.fitting import power_law

__all__ = [
    "SpwLayout",
    "VisitData",
    "combined_figures",
    "load_visit",
    "ratio_figure",
    "spectrum_figure",
    "spw_label",
]

COLOURS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#e377c2", "#8c564b", "#9467bd", "#7f7f7f", "#d62728", "#bcbd22",
]
DEFAULT_CHANNELS_PER_SPW = 64


@dataclass(frozen=True)
class SpwLayout:
    channels_per_spw: int
    n_spw: int | None
    source: str  # "stage2 metadata" | "stage2 if mode" | "stage1 metadata" | "assumed"

    @classmethod
    def from_metadata(cls, stage2_meta: dict[str, Any], stage1_meta: dict[str, Any] | None = None) -> SpwLayout:
        layout = stage2_meta.get("spectral_windows")
        if isinstance(layout, dict) and layout.get("channels_per_spw"):
            return cls(int(layout["channels_per_spw"]), layout.get("n_spw"), "stage2 metadata")
        if stage2_meta.get("mode") == "if" and stage2_meta.get("channels_per_if"):
            return cls(int(stage2_meta["channels_per_if"]), None, "stage2 if mode")
        if stage1_meta:
            final = stage1_meta.get("final_if_selfcal") or {}
            if final.get("channels_per_if"):
                return cls(int(final["channels_per_if"]), final.get("n_ifs") or None, "stage1 metadata")
        return cls(DEFAULT_CHANNELS_PER_SPW, None, "assumed")


def spw_label(first_channel: int, last_channel: int, layout: SpwLayout) -> str:
    """'spw 12 ch 37' for one channel, 'spw 12 ch 1-64' for a block (channels are 1-based)."""
    cps = layout.channels_per_spw
    spw_first, ch_first = divmod(int(first_channel) - 1, cps)
    spw_last, ch_last = divmod(int(last_channel) - 1, cps)
    if spw_first == spw_last:
        if ch_first == ch_last:
            return f"spw {spw_first + 1} ch {ch_first + 1}"
        return f"spw {spw_first + 1} ch {ch_first + 1}-{ch_last + 1}"
    return f"spw {spw_first + 1} ch {ch_first + 1} to spw {spw_last + 1} ch {ch_last + 1}"


@dataclass
class VisitData:
    prefix: str
    epoch: str
    mjd: float | None
    reference_frequency_ghz: float
    groups: list[str]
    reference_group: str | None
    layout: SpwLayout
    error_source: str
    spectrum: pd.DataFrame  # Stage 2 rows used by Stage 3 (ok rows, exclusions removed)
    powerlaw_fits: dict[str, dict[str, Any]] = field(default_factory=dict)
    ratio_fits: dict[str, dict[str, Any]] = field(default_factory=dict)
    ratios: pd.DataFrame | None = None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe(name: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip()).strip("_")


def load_visit(stage3_dir: Path) -> VisitData:
    """Gather everything the interactive per-visit figures need from a Stage 3 output directory."""
    meta_path = next(stage3_dir.glob("*.stage3.metadata.json"))
    meta = _load_json(meta_path)
    prefix = meta_path.name[: -len(".stage3.metadata.json")]
    stage2 = meta.get("stage2", {})
    spectrum_csv = Path(stage2.get("spectrum", ""))
    stage2_meta = _load_json(Path(stage2["metadata"])) if stage2.get("metadata") and Path(stage2["metadata"]).is_file() else {}
    stage1_meta = None
    if stage2_meta.get("stage1_metadata"):
        candidate = spectrum_csv.parent.parent.parent / "stage1" / f"{meta['source']}.{meta['epoch']}" / stage2_meta["stage1_metadata"]
        stage1_json = candidate.with_name(candidate.name.replace(".model.json", ".stage1.metadata.json"))
        if stage1_json.is_file():
            stage1_meta = _load_json(stage1_json)
    layout = SpwLayout.from_metadata(stage2_meta, stage1_meta)

    frame = pd.read_csv(spectrum_csv)
    frame = frame.loc[frame["fit_status"].astype(str).str.lower() == "ok"].copy()
    excluded = set(int(i) for i in (meta.get("channel_exclusion", {}) or {}).get("effective_fit_indices_for_epoch", []) or [])
    if excluded and "fit_index" in frame.columns:
        frame = frame.loc[~frame["fit_index"].astype(int).isin(excluded)]
    frame = frame.sort_values("frequency_ghz").reset_index(drop=True)

    fits_dir = stage3_dir / "fits"
    powerlaw = {row["label"]: row for row in _load_json(fits_dir / f"{prefix}.powerlaw_fits.json")} if (fits_dir / f"{prefix}.powerlaw_fits.json").is_file() else {}
    ratio_fits = {row["label"]: row for row in _load_json(fits_dir / f"{prefix}.flux_ratio_fits.json")} if (fits_dir / f"{prefix}.flux_ratio_fits.json").is_file() else {}
    ratios_csv = fits_dir / f"{prefix}.measured_flux_ratios.csv"
    ratios = pd.read_csv(ratios_csv) if ratios_csv.is_file() else None

    return VisitData(
        prefix=prefix,
        epoch=str(meta.get("epoch")),
        mjd=meta.get("mjd"),
        reference_frequency_ghz=float(meta.get("reference_frequency_ghz", 15.0)),
        groups=list(meta.get("groups", [])),
        reference_group=meta.get("reference_group"),
        layout=layout,
        error_source=str((meta.get("uncertainty_model") or {}).get("requested", "rms")),
        spectrum=frame,
        powerlaw_fits=powerlaw,
        ratio_fits=ratio_fits,
        ratios=ratios,
    )


def _customdata(frame: pd.DataFrame, layout: SpwLayout, extra: list[Any] | None = None) -> list[list[Any]]:
    rows = []
    for i, (fit_index, first, last, rms) in enumerate(zip(
        frame["fit_index"], frame["first_channel"], frame["last_channel"], frame["rms_jy_per_beam"], strict=False
    )):
        rows.append([int(fit_index), spw_label(first, last, layout), float(rms) * 1e3, extra[i] if extra else None])
    return rows


def _layout(title: str, xaxis: str, yaxis: str) -> dict[str, Any]:
    return {
        "title": {"text": title, "font": {"size": 14}},
        "xaxis": {"title": xaxis, "ticks": "inside", "mirror": True, "showline": True, "zeroline": False},
        "yaxis": {"title": yaxis, "ticks": "inside", "mirror": True, "showline": True, "zeroline": False},
        "hovermode": "closest",
        "legend": {"orientation": "h", "y": 1.08},
        "margin": {"l": 60, "r": 20, "t": 50, "b": 50},
        "template": "plotly_white",
        "font": {"family": "STIXGeneral, Times New Roman, serif"},
    }


def spectrum_figure(visit: VisitData) -> dict[str, Any]:
    frame = visit.spectrum
    freq = frame["frequency_ghz"].astype(float).to_numpy()
    traces: list[dict[str, Any]] = []
    for index, group in enumerate(visit.groups):
        column = f"{_safe(group)}_jy"
        if column not in frame.columns:
            continue
        flux = pd.to_numeric(frame[column], errors="coerce").to_numpy() * 1e3
        if visit.error_source == "difmap" and f"{_safe(group)}_error_jy" in frame.columns:
            err = pd.to_numeric(frame[f"{_safe(group)}_error_jy"], errors="coerce").to_numpy() * 1e3
        else:
            err = pd.to_numeric(frame["rms_jy_per_beam"], errors="coerce").to_numpy() * 1e3
        colour = COLOURS[index % len(COLOURS)]
        traces.append({
            "type": "scatter", "mode": "markers", "name": group,
            "x": freq.tolist(), "y": flux.tolist(),
            "error_y": {"type": "data", "array": err.tolist(), "visible": True, "thickness": 1, "width": 0},
            "marker": {"size": 5, "color": colour},
            "customdata": _customdata(frame, visit.layout, err.tolist()),
            "hovertemplate": (
                f"<b>{group}</b><br>%{{x:.4f}} GHz<br>%{{y:.3f}} ± %{{customdata[3]:.3f}} mJy"
                "<br>fit %{customdata[0]} · %{customdata[1]}<br>rms %{customdata[2]:.3f} mJy/beam<extra></extra>"
            ),
        })
        fit = visit.powerlaw_fits.get(group)
        if fit and np.isfinite(fit.get("s_ref_jy", np.nan)) and freq.size:
            grid = np.linspace(float(np.nanmin(freq)), float(np.nanmax(freq)), 200)
            model = power_law(grid, fit["s_ref_jy"], fit["alpha"], visit.reference_frequency_ghz) * 1e3
            traces.append({
                "type": "scatter", "mode": "lines", "name": f"{group} fit", "showlegend": False,
                "x": grid.tolist(), "y": model.tolist(),
                "line": {"color": "black", "width": 1.2, "dash": "dash"},
                "hovertemplate": (
                    f"<b>{group}</b> power law<br>S<sub>{visit.reference_frequency_ghz:g}</sub> = "
                    f"{fit['s_ref_jy'] * 1e3:.3f} ± {fit['s_ref_error_jy'] * 1e3:.3f} mJy<br>"
                    f"α = {fit['alpha']:.3f} ± {fit['alpha_error']:.3f}<extra></extra>"
                ),
            })
    title = f"{visit.prefix}" + (f"  MJD {visit.mjd:.4f}" if visit.mjd else "")
    return {"data": traces, "layout": _layout(title, "Frequency [GHz]", "Flux density [mJy]"),
            "config": {"displaylogo": False, "responsive": True}}


def ratio_figure(visit: VisitData) -> dict[str, Any] | None:
    if visit.ratios is None or visit.ratios.empty:
        return None
    ratios = visit.ratios.copy()
    lookup = visit.spectrum.set_index(visit.spectrum["fit_index"].astype(int))
    ratios = ratios.loc[ratios["fit_index"].astype(int).isin(lookup.index)]
    if ratios.empty:
        return None
    rows = lookup.loc[ratios["fit_index"].astype(int)]
    freq = ratios["frequency_ghz"].astype(float).to_numpy()
    traces: list[dict[str, Any]] = []
    labels = [c[: -len("_ratio")] for c in ratios.columns if c.endswith("_ratio")]
    for index, label in enumerate(labels):
        values = pd.to_numeric(ratios[f"{label}_ratio"], errors="coerce").to_numpy()
        errors = pd.to_numeric(ratios[f"{label}_error"], errors="coerce").to_numpy()
        pretty = label.replace("_over_", "/")
        colour = COLOURS[(index + 1) % len(COLOURS)]
        traces.append({
            "type": "scatter", "mode": "markers", "name": pretty,
            "x": freq.tolist(), "y": values.tolist(),
            "error_y": {"type": "data", "array": errors.tolist(), "visible": True, "thickness": 1, "width": 0},
            "marker": {"size": 5, "color": colour},
            "customdata": _customdata(rows, visit.layout, errors.tolist()),
            "hovertemplate": (
                f"<b>{pretty}</b><br>%{{x:.4f}} GHz<br>%{{y:.4f}} ± %{{customdata[3]:.4f}}"
                "<br>fit %{customdata[0]} · %{customdata[1]}<br>rms %{customdata[2]:.3f} mJy/beam<extra></extra>"
            ),
        })
        fit = visit.ratio_fits.get(pretty)
        if fit and freq.size:
            mean = fit.get("weighted_mean_ratio")
            if mean is not None and np.isfinite(mean):
                traces.append({
                    "type": "scatter", "mode": "lines", "name": f"{pretty} mean", "showlegend": False,
                    "x": [float(np.nanmin(freq)), float(np.nanmax(freq))], "y": [mean, mean],
                    "line": {"color": "black", "width": 1.1, "dash": "dash"},
                    "hovertemplate": f"<b>{pretty}</b> weighted mean {mean:.4f} ± {fit.get('weighted_mean_error', float('nan')):.4f}<extra></extra>",
                })
    title = f"{visit.prefix} flux ratios" + (f"  MJD {visit.mjd:.4f}" if visit.mjd else "")
    return {"data": traces, "layout": _layout(title, "Frequency [GHz]", "Flux ratio"),
            "config": {"displaylogo": False, "responsive": True}}


def _fit_index_layout_from_combined(combined_dir: Path) -> tuple[SpwLayout, dict[int, tuple[int, int]]]:
    """Spectral-window layout and fit_index -> (first, last channel) map from the first epoch's Stage 2."""
    meta = next(combined_dir.glob("*.combined.stage3.metadata.json"), None)
    layout = SpwLayout(DEFAULT_CHANNELS_PER_SPW, None, "assumed")
    channels: dict[int, tuple[int, int]] = {}
    if meta is None:
        return layout, channels
    payload = _load_json(meta)
    for manifest_path in payload.get("stage2_manifests", []):
        manifest = Path(manifest_path)
        if not manifest.is_file():
            continue
        manifest_data = _load_json(manifest)
        directory = manifest.parent
        meta2 = directory / manifest_data.get("metadata", "")
        csv = directory / manifest_data.get("spectrum", "")
        if meta2.is_file():
            layout = SpwLayout.from_metadata(_load_json(meta2))
        if csv.is_file():
            frame = pd.read_csv(csv, usecols=["fit_index", "first_channel", "last_channel"])
            channels = {int(r.fit_index): (int(r.first_channel), int(r.last_channel)) for r in frame.itertuples()}
        break
    return layout, channels


def combined_figures(combined_dir: Path) -> dict[str, dict[str, Any]]:
    """Averaged spectra and ratios (hover: spw/channel) and the versus-MJD series (hover: epoch)."""
    tables = combined_dir / "tables"
    stem = next(combined_dir.glob("*.combined.stage3.metadata.json")).name[: -len(".combined.stage3.metadata.json")]
    layout, channels = _fit_index_layout_from_combined(combined_dir)
    figures: dict[str, dict[str, Any]] = {}

    def spw_for(fit_index: int) -> str:
        first, last = channels.get(int(fit_index), (int(fit_index), int(fit_index)))
        return spw_label(first, last, layout)

    avg = tables / f"{stem}.average_spectrum.csv"
    if avg.is_file():
        frame = pd.read_csv(avg)
        traces = []
        groups = [c[: -len("_flux_mjy")] for c in frame.columns if c.endswith("_flux_mjy")]
        for index, group in enumerate(groups):
            custom = [[int(f), spw_for(f), int(n)] for f, n in zip(frame["fit_index"], frame["n_epochs_contributing"], strict=False)]
            traces.append({
                "type": "scatter", "mode": "markers", "name": group,
                "x": frame["frequency_ghz"].tolist(), "y": frame[f"{group}_flux_mjy"].tolist(),
                "error_y": {"type": "data", "array": frame[f"{group}_flux_error_mjy"].tolist(), "visible": True, "thickness": 1, "width": 0},
                "marker": {"size": 5, "color": COLOURS[index % len(COLOURS)]}, "customdata": custom,
                "hovertemplate": f"<b>{group}</b><br>%{{x:.4f}} GHz<br>%{{y:.3f}} mJy (mean of %{{customdata[2]}} epochs)<br>fit %{{customdata[0]}} · %{{customdata[1]}}<extra></extra>",
            })
        figures["Average spectrum (all epochs)"] = {"data": traces, "layout": _layout("Average spectrum", "Frequency [GHz]", "Flux density [mJy]"), "config": {"displaylogo": False, "responsive": True}}

    avg_ratio = tables / f"{stem}.average_flux_ratios.csv"
    if avg_ratio.is_file():
        frame = pd.read_csv(avg_ratio)
        traces = []
        labels = [c[: -len("_ratio")] for c in frame.columns if c.endswith("_ratio")]
        for index, label in enumerate(labels):
            pretty = label.replace("_over_", "/")
            custom = [[int(f), spw_for(f)] for f in frame["fit_index"]]
            traces.append({
                "type": "scatter", "mode": "markers", "name": pretty,
                "x": frame["frequency_ghz"].tolist(), "y": frame[f"{label}_ratio"].tolist(),
                "error_y": {"type": "data", "array": frame[f"{label}_error"].tolist(), "visible": True, "thickness": 1, "width": 0},
                "marker": {"size": 5, "color": COLOURS[(index + 1) % len(COLOURS)]}, "customdata": custom,
                "hovertemplate": f"<b>{pretty}</b><br>%{{x:.4f}} GHz<br>%{{y:.4f}}<br>fit %{{customdata[0]}} · %{{customdata[1]}}<extra></extra>",
            })
        figures["Average flux ratios (all epochs)"] = {"data": traces, "layout": _layout("Average flux ratios", "Frequency [GHz]", "Flux ratio"), "config": {"displaylogo": False, "responsive": True}}

    def mjd_figure(csv: Path, title: str, ylabel: str, series: list[tuple[str, str, str]]) -> None:
        """One trace per (value column, error column, display name) against MJD."""
        if not csv.is_file():
            return
        frame = pd.read_csv(csv)
        traces = []
        for index, (column, err_col, name) in enumerate(series):
            if column not in frame.columns:
                continue
            has_err = err_col in frame.columns
            traces.append({
                "type": "scatter", "mode": "markers", "name": name,
                "x": frame["mjd"].tolist(), "y": pd.to_numeric(frame[column], errors="coerce").tolist(),
                "error_y": {
                    "type": "data",
                    "array": pd.to_numeric(frame[err_col], errors="coerce").tolist() if has_err else [],
                    "visible": has_err, "thickness": 1, "width": 0,
                },
                "marker": {"size": 7, "color": COLOURS[index % len(COLOURS)]},
                "customdata": [[str(e)] for e in frame["epoch"]],
                "hovertemplate": f"<b>{name}</b><br>epoch %{{customdata[0]}}<br>MJD %{{x:.4f}}<br>%{{y:.4f}}<extra></extra>",
            })
        if traces:
            figures[title] = {"data": traces, "layout": _layout(title, "MJD", ylabel), "config": {"displaylogo": False, "responsive": True}}

    ref_csv = tables / f"{stem}.reference_fluxes_vs_mjd.csv"
    if ref_csv.is_file():
        columns = [c for c in pd.read_csv(ref_csv, nrows=0).columns if c.endswith("_sref_mjy")]
        mjd_figure(ref_csv, "Reference-frequency flux vs MJD", "Flux density [mJy]",
                   [(c, c.replace("_sref_mjy", "_sref_error_mjy"), c[: -len("_sref_mjy")]) for c in columns])
    ratio_csv = tables / f"{stem}.weighted_flux_ratios_vs_mjd.csv"
    if ratio_csv.is_file():
        columns = [c for c in pd.read_csv(ratio_csv, nrows=0).columns
                   if c not in {"epoch", "mjd"} and not c.endswith("_error")]
        mjd_figure(ratio_csv, "Weighted flux ratios vs MJD", "Flux ratio",
                   [(c, f"{c}_error", c.replace("_over_", "/")) for c in columns])
    mjd_figure(tables / f"{stem}.rcusp_vs_mjd.csv", "R_cusp vs MJD", "R_cusp",
               [("rcusp_a1_a2_b", "rcusp_a1_a2_b_error", "R_cusp")])
    return figures
