"""Delete-one-group jackknife errors across Stage 2 products that each leave out one channel block.

A jackknife set is every Stage 2 product of one source made with the same base
extraction (the product tag before ``_x_``) and a ``stage2.exclude_channels``
spec that drops a block of channels inside the same spectral windows, e.g.
``if64_edge2_x_spw0-47_2-5``, ``if64_edge2_x_spw0-47_6-9``, ... Each product is
one sample. A spec that drops whole windows (``spw7``) never makes a sample.

For a quantity measured once per sample (n samples) the estimate is the mean
and its error ``sqrt((n - 1) / n * sum((x_i - mean)^2))``. Missing samples
(a visit absent from one product, a failed fit) are left out of that quantity,
and the ``n_samples`` columns say how many were used.

Output goes to ``stage3/<mode>/combined/<source>/<base>_x_<windows>_jackknife/``
with the usual ``plots/`` and ``tables/``; ``<stem>`` below is
``<source>.<base>_x_<windows>_jackknife``.
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from lenspipe.project import Layout
from lenspipe.stage3 import plotting
from lenspipe.stage3.analysis import (
    STAGE3_VERSION,
    EpochAnalysis,
    _combined_average_ratio_series,
    _combined_average_spectrum_series,
    write_csv,
    write_json,
)
from lenspipe.stage3.combined import fit_average_flux_ratios, fit_average_spectra
from lenspipe.stage3.exclusions import parse_exclusion_spec
from lenspipe.stage3.io import (
    available_stage2_products,
    discover_stage2_datasets,
    load_stage2_metadata,
    safe_label,
)
from lenspipe.stage3.jackknife_plots import (
    plot_jackknife_histograms,
    plot_jackknife_spectra_grid,
    plot_jackknife_stack,
)

__all__ = [
    "JackknifeSet", "find_jackknife_sets", "jackknife_estimate", "sample_key", "with_jackknife_samples",
    "write_jackknife_products",
]

JACKKNIFE_ERROR = "sqrt((n - 1) / n * sum((x_i - mean)^2)) over the n jackknife samples"


def jackknife_estimate(values: Any) -> tuple[float, float, int]:
    """Mean, jackknife error and number of finite samples (error NaN with fewer than two)."""
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n == 0:
        return float("nan"), float("nan"), 0
    mean = float(np.mean(x))
    if n < 2:
        return mean, float("nan"), n
    return mean, float(np.sqrt((n - 1) / n * np.sum((x - mean) ** 2))), n


def sample_key(product_tag: str, stage2_metadata: dict[str, Any]) -> tuple[str, str] | None:
    """``(base tag, windows token)`` when this product is a jackknife sample, else None.

    A sample's Stage 2 exclusion consists only of channel blocks inside
    windows (``spw0-47:2-5``); the windows token (``spw0-47``) keeps samples
    over different windows in different sets.
    """
    spec_text = ((stage2_metadata.get("channel_exclusion") or {}).get("exclude_channels") or "").strip()
    if not spec_text or "_x_" not in product_tag:
        return None
    try:
        spec = parse_exclusion_spec(spec_text, "stage2.exclude_channels")
    except ValueError:
        return None
    if spec.fit_indices or not spec.spw_items or any(item.channels is None for item in spec.spw_items):
        return None
    windows = "_".join(
        f"spw{item.spw}-{item.spw_last}" if item.spw_last is not None else f"spw{item.spw}" for item in spec.spw_items
    )
    return product_tag.split("_x_", 1)[0], windows


@dataclass
class JackknifeSet:
    source: str
    base_tag: str
    windows: str
    samples: dict[str, list[EpochAnalysis]] = field(default_factory=dict)  # product tag -> its visits
    specs: dict[str, str] = field(default_factory=dict)  # product tag -> stage2.exclude_channels

    @property
    def name(self) -> str:
        return f"{self.base_tag}_x_{self.windows}_jackknife"

    @property
    def n(self) -> int:
        return len(self.samples)


def find_jackknife_sets(analyses: list[EpochAnalysis]) -> list[JackknifeSet]:
    """Group the analysed visits into jackknife sets of two or more sample products."""
    sets: dict[tuple[str, str, str], JackknifeSet] = {}
    for analysis in analyses:
        key = sample_key(analysis.dataset.product_tag, analysis.metadata)
        if key is None:
            continue
        identity = (analysis.dataset.source, *key)
        found = sets.setdefault(identity, JackknifeSet(analysis.dataset.source, key[0], key[1]))
        found.samples.setdefault(analysis.dataset.product_tag, []).append(analysis)
        found.specs[analysis.dataset.product_tag] = analysis.metadata["channel_exclusion"]["exclude_channels"]
    return [item for _, item in sorted(sets.items()) if item.n >= 2]


def with_jackknife_samples(
    project_root: Path,
    requested_epochs: set[str] | None,
    requested_products: set[str] | None,
    calibration_mode: str,
) -> set[str]:
    """The sample products a jackknife run analyses; only these, never the base or other products.

    Nothing requested: every sample of every set of two or more. A requested
    sample selects its whole set; a requested base product (``if64_edge2``)
    selects every set made from it; other requested products select nothing.
    Empty when no set applies.
    """
    tags = {tag for found in available_stage2_products(project_root, requested_epochs, calibration_mode).values() for tag in found}
    if not tags:
        return set()
    families: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for dataset in discover_stage2_datasets(project_root, requested_epochs, tags, calibration_mode):
        key = sample_key(dataset.product_tag, load_stage2_metadata(dataset))
        if key is not None:
            families[(dataset.source, *key)].add(dataset.product_tag)
    sets = {key: members for key, members in families.items() if len(members) >= 2}
    wanted: set[str] = set()
    for (_, base, _), members in sets.items():
        if not requested_products or members & requested_products or base in requested_products:
            wanted |= members
    return wanted


def _per_epoch(jack: JackknifeSet) -> tuple[list[tuple[str, float]], dict[str, dict[str, EpochAnalysis]]]:
    """Epochs (label, MJD) in time order and, per product, each epoch's visit."""
    by_product = {tag: {a.dataset.epoch: a for a in visits} for tag, visits in jack.samples.items()}
    mjds: dict[str, list[float]] = defaultdict(list)
    for visits in by_product.values():
        for epoch, analysis in visits.items():
            mjds[epoch].append(analysis.mjd)
    epochs = sorted(((epoch, float(np.mean(values))) for epoch, values in mjds.items()), key=lambda item: item[1])
    return epochs, by_product


def _spectra(
    epochs: list[tuple[str, float]], by_product: dict[str, dict[str, EpochAnalysis]], groups: list[str]
) -> tuple[list[dict[str, Any]], list[tuple[float, dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]]]]:
    """Per-channel grouped image fluxes (A, B, ...) of every sample, aligned by Stage 2 fit index.

    The same per-channel fluxes Stage 3 fits, so a group is the sum of its
    model components, as on the standard spectra. Returns the table rows and,
    per epoch, ``(MJD, {image: (frequency_ghz, flux_mjy, error_mjy)})``.
    """
    rows: list[dict[str, Any]] = []
    grid: list[tuple[float, dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]]] = []
    for epoch, mjd in epochs:
        values: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        frequency: dict[int, list[float]] = defaultdict(list)
        for visits in by_product.values():
            analysis = visits.get(epoch)
            if analysis is None:
                continue
            for position, fit_index in enumerate(analysis.fit_indices):
                frequency[int(fit_index)].append(float(analysis.frequency_ghz[position]))
                for group in groups:
                    values[group][int(fit_index)].append(float(analysis.fluxes[group][position]) * 1000.0)
        panel: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for group in groups:
            indices = sorted(values[group])
            stats = [jackknife_estimate(values[group][i]) for i in indices]
            panel[group] = (
                np.array([np.mean(frequency[i]) for i in indices]),
                np.array([s[0] for s in stats]),
                np.array([s[1] for s in stats]),
            )
            rows.extend(
                {
                    "epoch": epoch, "mjd": mjd, "fit_index": i, "frequency_ghz": float(np.mean(frequency[i])),
                    "image": group, "flux_mjy": s[0], "flux_error_mjy": s[1], "n_samples": s[2],
                }
                for i, s in zip(indices, stats, strict=False)
            )
        grid.append((mjd, panel))
    return rows, grid


def _scatter(values: np.ndarray, errors: np.ndarray) -> tuple[float, float]:
    """Per-cent scatter across epochs: plain (std / mean) and inverse-variance weighted."""
    mask = np.isfinite(values) & np.isfinite(errors) & (errors > 0)
    if mask.sum() < 2:
        return float("nan"), float("nan")
    x, w = values[mask], 1.0 / errors[mask] ** 2
    plain = float(np.std(x) / np.mean(x) * 100.0)
    weighted_mean = float(np.sum(w * x) / np.sum(w))
    weighted = float(np.sqrt(np.sum(w * (x - weighted_mean) ** 2) / np.sum(w)) / weighted_mean * 100.0)
    return plain, weighted


def write_jackknife_products(
    jack: JackknifeSet,
    reference_frequency_ghz: float,
    overwrite: bool,
    analysis_tag: str | None,
) -> Path | None:
    """Compute the set's jackknife estimates and write figures, tables and metadata. None when skipped."""
    first = next(iter(jack.samples.values()))[0]
    final_output = Layout.at(first.dataset.project_root).stage_dir(3, first.dataset.calibration_mode)
    final_output = final_output / "combined" / jack.source / jack.name
    if analysis_tag:
        final_output = final_output / analysis_tag
    if final_output.exists() and not overwrite:
        print(f"SKIPPED JACKKNIFE: {jack.source}.{jack.name}; output already exists.")
        return None
    final_output.parent.mkdir(parents=True, exist_ok=True)
    working = final_output.parent / f".{final_output.name}.jackknife-{STAGE3_VERSION}.tmp"
    if working.exists():
        shutil.rmtree(working)
    plots_dir, tables_dir = working / "plots", working / "tables"
    plots_dir.mkdir(parents=True)
    tables_dir.mkdir(parents=True)
    stem = f"{jack.source}.{jack.name}"
    groups = list(first.spectral_fits)
    ratios = list(first.ratio_fits)
    products = sorted(jack.samples)

    try:
        # One all-visit average spectrum and average flux ratio per sample, as the combined product makes them.
        sample_rows: list[dict[str, Any]] = []
        average_s_ref = {g: [] for g in groups}
        average_alpha = {g: [] for g in groups}
        average_ratio = {r: [] for r in ratios}
        for tag in products:
            visits = sorted(jack.samples[tag], key=lambda a: a.mjd)
            _, frequency, _, spectrum_series = _combined_average_spectrum_series(visits)
            spectrum_fits = fit_average_spectra(frequency, spectrum_series, reference_frequency_ghz)
            _, _, _, ratio_series = _combined_average_ratio_series(visits)
            ratio_fits = fit_average_flux_ratios(ratio_series) if ratio_series else {}
            row: dict[str, Any] = {"product": tag, "exclude_channels": jack.specs[tag], "n_visits": len(visits)}
            for g in groups:
                fit = spectrum_fits.get(g)
                average_s_ref[g].append(fit.s_ref_jy * 1000.0 if fit else np.nan)
                average_alpha[g].append(fit.alpha if fit else np.nan)
                row[f"{safe_label(g)}_s_ref_mjy"] = average_s_ref[g][-1]
                row[f"{safe_label(g)}_alpha"] = average_alpha[g][-1]
            for r in ratios:
                fit = ratio_fits.get(r)
                average_ratio[r].append(fit.value if fit else np.nan)
                row[f"{r.replace('/', '_over_')}_ratio"] = average_ratio[r][-1]
            sample_rows.append(row)
        write_csv(tables_dir / f"{stem}.samples.csv", sample_rows)

        average_rows: list[dict[str, Any]] = []
        for quantity, label, parameter, values in (
            *[("average_spectrum", g, "s_ref_mjy", average_s_ref[g]) for g in groups],
            *[("average_spectrum", g, "alpha", average_alpha[g]) for g in groups],
            *[("average_flux_ratio", r, "value", average_ratio[r]) for r in ratios],
        ):
            mean, error, n = jackknife_estimate(values)
            average_rows.append({
                "product": quantity, "label": label, "parameter": parameter,
                "value": mean, "jackknife_error": error, "n_samples": n,
            })

        # Per epoch: each visit's own power-law and ratio fits, one value per sample.
        epochs, by_product = _per_epoch(jack)
        mjd = np.array([m for _, m in epochs])

        def per_epoch(get) -> np.ndarray:
            return np.array([
                [get(by_product[tag][epoch]) if epoch in by_product[tag] else np.nan for tag in products]
                for epoch, _ in epochs
            ], dtype=float)

        flux = {g: per_epoch(lambda a, g=g: a.spectral_fits[g].s_ref_jy * 1000.0) for g in groups}
        alpha = {g: per_epoch(lambda a, g=g: a.spectral_fits[g].alpha) for g in groups}
        ratio = {r: per_epoch(lambda a, r=r: a.ratio_fits[r].ratio_ref) for r in ratios}

        def estimates(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
            """Per epoch (row): jackknife mean, error and number of samples."""
            stats = np.array([jackknife_estimate(row) for row in matrix], dtype=float).reshape(-1, 3)
            return stats[:, 0], stats[:, 1], stats[:, 2]

        flux_est = {g: estimates(flux[g]) for g in groups}
        alpha_est = {g: estimates(alpha[g]) for g in groups}
        # Offsets from the reference image's alpha of the same visit, differenced within each sample and then
        # jackknifed, so the error includes the correlation between the two alphas.
        reference = groups[0]
        offset_est = {g: estimates(alpha[g] - alpha[reference]) for g in groups[1:]}
        ratio_est = {r: estimates(ratio[r]) for r in ratios}

        epoch_rows: list[dict[str, Any]] = []
        for i, (epoch, value) in enumerate(epochs):
            row = {"epoch": epoch, "mjd": value}
            for g in groups:
                key = safe_label(g)
                row |= {
                    f"{key}_flux_mjy": flux_est[g][0][i], f"{key}_flux_err_mjy": flux_est[g][1][i],
                    f"{key}_alpha": alpha_est[g][0][i], f"{key}_alpha_err": alpha_est[g][1][i],
                    **(
                        {f"{key}_alpha_offset": offset_est[g][0][i], f"{key}_alpha_offset_err": offset_est[g][1][i]}
                        if g in offset_est else {}
                    ),
                    f"{key}_n_samples": int(flux_est[g][2][i]),
                }
            for r in ratios:
                key = r.replace("/", "_over_")
                row |= {f"{key}_ratio": ratio_est[r][0][i], f"{key}_ratio_err": ratio_est[r][1][i]}
            epoch_rows.append(row)
        write_csv(tables_dir / f"{stem}.errors_per_epoch.csv", epoch_rows)

        for kind, estimates_by_label in (("epoch_flux", flux_est), ("epoch_flux_ratio", ratio_est)):
            for label, (values, errors, _) in estimates_by_label.items():
                plain, weighted = _scatter(values, errors)
                average_rows.extend([
                    {"product": kind, "label": label, "parameter": "std_percent", "value": plain,
                     "jackknife_error": None, "n_samples": len(values)},
                    {"product": kind, "label": label, "parameter": "weighted_std_percent", "value": weighted,
                     "jackknife_error": None, "n_samples": len(values)},
                ])
        write_csv(tables_dir / f"{stem}.average_fits.csv", average_rows)

        spectra_rows, spectra_grid = _spectra(epochs, by_product, groups)
        write_csv(tables_dir / f"{stem}.spectra.csv", spectra_rows)

        # Figures, all in the _mnras style.
        plot_jackknife_histograms(
            average_ratio, {r: r for r in ratios}, plots_dir / f"{stem}.average_flux_ratios_hist", colour_offset=1,
        )
        plot_jackknife_histograms(
            average_s_ref, {g: f"{g} [mJy]" for g in groups}, plots_dir / f"{stem}.average_fluxes_hist",
        )
        plot_jackknife_stack(
            mjd, {g: flux_est[g][:2] for g in groups}, {g: f"{g} [mJy]" for g in groups},
            plots_dir / f"{stem}.fluxes_vs_mjd",
        )
        plot_jackknife_stack(
            mjd, {g: alpha_est[g][:2] for g in groups}, {g: rf"$\alpha_{{\rm {g}}}$" for g in groups},
            plots_dir / f"{stem}.alphas_vs_mjd",
        )
        if offset_est:
            plot_jackknife_stack(
                mjd, {g: offset_est[g][:2] for g in offset_est},
                {g: rf"$\alpha_{{\rm {g}}} - \alpha_{{\rm {reference}}}$" for g in offset_est},
                plots_dir / f"{stem}.alpha_offsets_vs_mjd", colour_offset=1,
            )
        if ratios:
            plot_jackknife_stack(
                mjd, {r: ratio_est[r][:2] for r in ratios}, {r: r for r in ratios},
                plots_dir / f"{stem}.flux_ratios_vs_mjd", colour_offset=1,
            )
            normalised = {}
            for r in ratios:
                mean = float(np.nanmean(ratio_est[r][0]))
                normalised[r] = (ratio_est[r][0] / mean, ratio_est[r][1] / mean)
            plot_jackknife_stack(
                mjd, normalised, {r: r for r in ratios},
                plots_dir / f"{stem}.normalised_flux_ratios_vs_mjd", colour_offset=1, centre=1.0,
            )
        plot_jackknife_spectra_grid(spectra_grid, plots_dir / f"{stem}.spectra_all_epochs")

        write_json(
            working / f"{stem}.combined.stage3.metadata.json",
            {
                "schema_version": "1.0",
                "stage": 3,
                "stage3_version": STAGE3_VERSION,
                "created_utc": datetime.now(UTC).isoformat(),
                "kind": "jackknife",
                "source": jack.source,
                "product_tag": jack.name,
                "calibration_mode": first.dataset.calibration_mode,
                "analysis_tag": analysis_tag,
                "n_visits": len(epochs),
                "epochs": [epoch for epoch, _ in epochs],
                "mjds": [float(value) for value in mjd],
                "n_samples": jack.n,
                "samples": {tag: jack.specs[tag] for tag in products},
                "jackknife_error": JACKKNIFE_ERROR,
                "definitions": {
                    "samples.csv": "Each sample's all-visit average spectrum fit (S_ref, alpha) and average flux ratios.",
                    "average_fits.csv": (
                        "Jackknife mean and error of those per-sample values; epoch_flux and epoch_flux_ratio rows "
                        "give the scatter across epochs of the per-epoch jackknife means: std_percent = population "
                        "std / mean, weighted_std_percent = inverse-variance (1/jackknife error^2) weighted."
                    ),
                    "errors_per_epoch.csv": "Per epoch, the jackknife mean and error of each visit's own S_ref, alpha and ratio_ref.",
                    "spectra.csv": (
                        "Per epoch and Stage 2 fit, the jackknife mean and error of each grouped image's flux "
                        "(the sum of its model components, as Stage 3 fits it)."
                    ),
                    "alpha_offsets_vs_mjd": (
                        f"alpha of each image minus alpha_{reference} of the same visit, differenced within each "
                        "sample and then jackknifed; columns <image>_alpha_offset in errors_per_epoch.csv."
                    ),
                    "normalised_flux_ratios_vs_mjd": "ratio and its error divided by the plain mean over epochs.",
                },
                "stage2_manifests": [str(a.dataset.manifest_json) for a in jack.samples[products[0]]],
                "figure_formats": list(plotting.FIGURE_FORMATS),
            },
        )
        if final_output.exists():
            shutil.rmtree(final_output)
        working.replace(final_output)
    except Exception:
        if working.exists():
            shutil.rmtree(working)
        raise
    print(f"COMPLETED JACKKNIFE: {stem} ({jack.n} samples, {len(epochs)} epochs)")
    return final_output
