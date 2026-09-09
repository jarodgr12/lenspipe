#!/usr/bin/env python3
"""Fits and variability statistics for combined Stage 3 products."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

import numpy as np

from stage3_fitting import PowerLawFit, fit_power_law


@dataclass
class ConstantFit:
    label: str
    value: float
    error: float
    chi_square: float
    reduced_chi_square: float
    degrees_of_freedom: int
    n_points: int
    fit_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalisedScatter:
    label: str
    sigma_fraction: float
    sigma_percent: float
    weighted_mean: float
    weighted_mean_error: float
    chi_square_about_unity: float
    reduced_chi_square_about_unity: float
    degrees_of_freedom: int
    n_points: int
    fit_status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fit_constant(label: str, values: Any, errors: Any) -> ConstantFit:
    """Fit a constant using inverse-variance weighting."""
    y = np.asarray(values, dtype=float)
    sigma = np.asarray(errors, dtype=float)
    mask = np.isfinite(y) & np.isfinite(sigma) & (sigma > 0)
    y = y[mask]
    sigma = sigma[mask]
    if y.size == 0:
        return ConstantFit(label, np.nan, np.nan, np.nan, np.nan, 0, 0, "no_valid_points")

    weights = 1.0 / np.square(sigma)
    value = float(np.sum(weights * y) / np.sum(weights))
    error = float(np.sqrt(1.0 / np.sum(weights)))
    chi_square = float(np.sum(np.square((y - value) / sigma)))
    dof = int(y.size - 1)
    return ConstantFit(
        label=label,
        value=value,
        error=error,
        chi_square=chi_square,
        reduced_chi_square=(chi_square / dof if dof > 0 else np.nan),
        degrees_of_freedom=dof,
        n_points=int(y.size),
        fit_status="ok",
    )


def fit_average_spectra(
    frequency_ghz: Any,
    averaged_flux_series: Mapping[str, Mapping[str, Any]],
    reference_frequency_ghz: float,
) -> dict[str, PowerLawFit]:
    """Fit a power law to each combined average grouped-image spectrum."""
    return {
        label: fit_power_law(
            label,
            frequency_ghz,
            series["values"],
            series["errors"],
            reference_frequency_ghz,
        )
        for label, series in averaged_flux_series.items()
    }


def fit_average_flux_ratios(
    averaged_ratio_series: Mapping[str, Mapping[str, Any]],
) -> dict[str, ConstantFit]:
    """Fit a constant to each channel-by-channel average flux-ratio spectrum."""
    return {
        label: fit_constant(label, series["values"], series["errors"])
        for label, series in averaged_ratio_series.items()
    }


def calculate_normalised_scatter(
    normalised_ratio_series: Mapping[str, Mapping[str, Any]],
) -> dict[str, NormalisedScatter]:
    """Measure epoch-to-epoch scatter of normalised ratios.

    ``sigma`` is the ordinary sample standard deviation of the normalised
    ratios about their sample mean (``ddof=1``), reported as both a fraction
    and a percentage. The chi-square statistics about unity are retained in
    the machine-readable outputs but are not displayed on the plots.
    """
    results: dict[str, NormalisedScatter] = {}
    for label, series in normalised_ratio_series.items():
        y = np.asarray(series["values"], dtype=float)
        sigma = np.asarray(series["errors"], dtype=float)
        mask = np.isfinite(y) & np.isfinite(sigma) & (sigma > 0)
        y = y[mask]
        sigma = sigma[mask]
        n = int(y.size)
        if n == 0:
            results[label] = NormalisedScatter(
                label, np.nan, np.nan, np.nan, np.nan,
                np.nan, np.nan, 0, 0, "no_valid_points",
            )
            continue
        weights = 1.0 / np.square(sigma)
        weighted_mean = float(np.sum(weights * y) / np.sum(weights))
        weighted_mean_error = float(np.sqrt(1.0 / np.sum(weights)))
        dof = max(n - 1, 0)
        sigma_fraction = (
            float(np.std(y, ddof=1)) if dof > 0 else 0.0
        )
        chi_square = float(np.sum(np.square((y - 1.0) / sigma)))
        results[label] = NormalisedScatter(
            label=label,
            sigma_fraction=sigma_fraction,
            sigma_percent=100.0 * sigma_fraction,
            weighted_mean=weighted_mean,
            weighted_mean_error=weighted_mean_error,
            chi_square_about_unity=chi_square,
            reduced_chi_square_about_unity=(chi_square / dof if dof > 0 else np.nan),
            degrees_of_freedom=dof,
            n_points=n,
            fit_status="ok",
        )
    return results
