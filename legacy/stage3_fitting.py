#!/usr/bin/env python3
"""Spectral and flux-ratio fitting for DifMAP Spectral Pipeline Stage 3."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.optimize import curve_fit


@dataclass
class PowerLawFit:
    label: str
    fit_method: str
    reference_frequency_ghz: float
    s_ref_jy: float
    s_ref_error_jy: float
    s_ref_error_low_jy: float
    s_ref_error_high_jy: float
    alpha: float
    alpha_error: float
    alpha_error_low: float
    alpha_error_high: float
    covariance_sref_alpha: float
    correlation_sref_alpha: float
    chi_square: float
    reduced_chi_square: float
    degrees_of_freedom: int
    n_points: int
    fit_status: str
    acceptance_fraction: float | None = None
    autocorrelation_time_log_sref: float | None = None
    autocorrelation_time_alpha: float | None = None
    n_walkers: int | None = None
    n_steps: int | None = None
    burn_in: int | None = None
    thin: int | None = None
    n_posterior_samples: int | None = None
    prior_log_sref_min: float | None = None
    prior_log_sref_max: float | None = None
    prior_alpha_min: float | None = None
    prior_alpha_max: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class RatioFit:
    label: str
    numerator: str
    denominator: str
    reference_frequency_ghz: float
    ratio_ref: float
    ratio_ref_error: float
    delta_alpha: float
    delta_alpha_error: float
    covariance_ratio_delta_alpha: float
    correlation_ratio_delta_alpha: float
    chi_square: float
    reduced_chi_square: float
    degrees_of_freedom: int
    n_points: int
    weighted_mean_ratio: float
    weighted_mean_error: float
    constant_chi_square: float
    constant_reduced_chi_square: float
    fit_status: str

    def as_dict(self) -> dict:
        return asdict(self)


def power_law(frequency_ghz, s_ref, alpha, reference_frequency_ghz):
    frequency = np.asarray(frequency_ghz, dtype=float)
    return s_ref * (frequency / reference_frequency_ghz) ** alpha


def ratio_power_law(frequency_ghz, ratio_ref, delta_alpha, reference_frequency_ghz):
    frequency = np.asarray(frequency_ghz, dtype=float)
    return ratio_ref * (frequency / reference_frequency_ghz) ** delta_alpha


def _correlation(covariance: np.ndarray) -> float:
    denominator = np.sqrt(covariance[0, 0] * covariance[1, 1])
    if not np.isfinite(denominator) or denominator <= 0:
        return float("nan")
    return float(covariance[0, 1] / denominator)


def _valid_spectrum(frequency_ghz, flux_jy, uncertainty_jy):
    frequency_ghz = np.asarray(frequency_ghz, dtype=float)
    flux_jy = np.asarray(flux_jy, dtype=float)
    uncertainty_jy = np.asarray(uncertainty_jy, dtype=float)
    mask = (
        np.isfinite(frequency_ghz) & np.isfinite(flux_jy)
        & np.isfinite(uncertainty_jy) & (frequency_ghz > 0)
        & (flux_jy > 0) & (uncertainty_jy > 0)
    )
    return frequency_ghz[mask], flux_jy[mask], uncertainty_jy[mask]


def fit_power_law(label, frequency_ghz, flux_jy, uncertainty_jy,
                  reference_frequency_ghz=15.0) -> PowerLawFit:
    """RMS-weighted non-linear least-squares fit."""
    nu, flux, sigma = _valid_spectrum(frequency_ghz, flux_jy, uncertainty_jy)
    if len(nu) < 3:
        raise ValueError(f"{label}: at least three valid measurements are required.")

    initial_sref = float(np.interp(reference_frequency_ghz, np.sort(nu), flux[np.argsort(nu)]))
    if not np.isfinite(initial_sref) or initial_sref <= 0:
        initial_sref = float(np.nanmedian(flux))
    log_slope = np.polyfit(np.log(nu), np.log(flux), 1)[0]
    model = lambda x, s_ref, alpha: power_law(x, s_ref, alpha, reference_frequency_ghz)
    popt, pcov = curve_fit(
        model, nu, flux, p0=(initial_sref, float(log_slope)), sigma=sigma,
        absolute_sigma=True, maxfev=100000,
    )
    prediction = model(nu, *popt)
    chi_square = float(np.sum(((flux - prediction) / sigma) ** 2))
    dof = len(nu) - 2
    errors = np.sqrt(np.diag(pcov))
    return PowerLawFit(
        label=label, fit_method="least_squares",
        reference_frequency_ghz=reference_frequency_ghz,
        s_ref_jy=float(popt[0]), s_ref_error_jy=float(errors[0]),
        s_ref_error_low_jy=float(errors[0]), s_ref_error_high_jy=float(errors[0]),
        alpha=float(popt[1]), alpha_error=float(errors[1]),
        alpha_error_low=float(errors[1]), alpha_error_high=float(errors[1]),
        covariance_sref_alpha=float(pcov[0, 1]),
        correlation_sref_alpha=_correlation(pcov), chi_square=chi_square,
        reduced_chi_square=(chi_square / dof if dof > 0 else float("nan")),
        degrees_of_freedom=dof, n_points=len(nu), fit_status="ok",
    )


def fit_power_law_emcee(
    label, frequency_ghz, flux_jy, uncertainty_jy,
    reference_frequency_ghz=15.0, n_walkers=32, n_steps=4000,
    burn_in=1000, thin=10, seed=12345, alpha_min=-3.0, alpha_max=2.0,
):
    """Sample the power-law posterior with optional dependency ``emcee``.

    Returns ``(fit_result, posterior)``. Posterior columns are
    ``log_s_ref_jy, alpha, s_ref_jy``.
    """
    try:
        import emcee
    except ImportError as exc:
        raise RuntimeError(
            "Bayesian fitting requested but emcee is not installed. "
            "Install it with: python -m pip install emcee"
        ) from exc

    nu, flux, sigma = _valid_spectrum(frequency_ghz, flux_jy, uncertainty_jy)
    if len(nu) < 3:
        raise ValueError(f"{label}: at least three valid measurements are required.")
    if n_walkers < 8 or n_walkers % 2:
        raise ValueError("--emcee-walkers must be an even integer >= 8")
    if not (0 <= burn_in < n_steps):
        raise ValueError("--emcee-burn-in must satisfy 0 <= burn-in < steps")
    if thin < 1:
        raise ValueError("--emcee-thin must be >= 1")

    ls = fit_power_law(label, nu, flux, sigma, reference_frequency_ghz)
    log_s0 = float(np.log(ls.s_ref_jy))
    # Broad, data-scaled log-uniform normalization prior (factor 100 each side).
    log_s_min = float(np.log(np.nanmin(flux) / 100.0))
    log_s_max = float(np.log(np.nanmax(flux) * 100.0))

    def log_prior(theta):
        log_s_ref, alpha = theta
        if log_s_min < log_s_ref < log_s_max and alpha_min < alpha < alpha_max:
            return 0.0
        return -np.inf

    def log_likelihood(theta):
        log_s_ref, alpha = theta
        model = power_law(nu, np.exp(log_s_ref), alpha, reference_frequency_ghz)
        residual = (flux - model) / sigma
        return -0.5 * float(np.sum(residual**2 + np.log(2.0 * np.pi * sigma**2)))

    def log_probability(theta):
        lp = log_prior(theta)
        if not np.isfinite(lp):
            return -np.inf
        return lp + log_likelihood(theta)

    rng = np.random.default_rng(seed)
    scale_log_s = max(ls.s_ref_error_jy / ls.s_ref_jy, 1.0e-4)
    scale_alpha = max(ls.alpha_error, 1.0e-4)
    initial = np.column_stack([
        rng.normal(log_s0, scale_log_s, n_walkers),
        rng.normal(ls.alpha, scale_alpha, n_walkers),
    ])
    initial[:, 0] = np.clip(initial[:, 0], log_s_min + 1e-8, log_s_max - 1e-8)
    initial[:, 1] = np.clip(initial[:, 1], alpha_min + 1e-8, alpha_max - 1e-8)

    sampler = emcee.EnsembleSampler(n_walkers, 2, log_probability)
    sampler.run_mcmc(initial, n_steps, progress=False)
    flat = sampler.get_chain(discard=burn_in, thin=thin, flat=True)
    if len(flat) == 0:
        raise RuntimeError(f"{label}: no posterior samples remain after burn-in/thinning")

    s_samples = np.exp(flat[:, 0])
    a_samples = flat[:, 1]
    s16, s50, s84 = np.percentile(s_samples, [16, 50, 84])
    a16, a50, a84 = np.percentile(a_samples, [16, 50, 84])
    covariance = np.cov(np.column_stack([s_samples, a_samples]), rowvar=False)
    prediction = power_law(nu, s50, a50, reference_frequency_ghz)
    chi_square = float(np.sum(((flux - prediction) / sigma) ** 2))
    dof = len(nu) - 2

    tau_log_s = tau_alpha = float("nan")
    try:
        tau = sampler.get_autocorr_time(discard=burn_in, tol=0)
        tau_log_s, tau_alpha = map(float, tau)
    except Exception:
        pass

    fit = PowerLawFit(
        label=label, fit_method="emcee",
        reference_frequency_ghz=reference_frequency_ghz,
        s_ref_jy=float(s50), s_ref_error_jy=float(0.5 * ((s50-s16)+(s84-s50))),
        s_ref_error_low_jy=float(s50-s16), s_ref_error_high_jy=float(s84-s50),
        alpha=float(a50), alpha_error=float(0.5 * ((a50-a16)+(a84-a50))),
        alpha_error_low=float(a50-a16), alpha_error_high=float(a84-a50),
        covariance_sref_alpha=float(covariance[0, 1]),
        correlation_sref_alpha=_correlation(covariance), chi_square=chi_square,
        reduced_chi_square=(chi_square / dof if dof > 0 else float("nan")),
        degrees_of_freedom=dof, n_points=len(nu), fit_status="ok",
        acceptance_fraction=float(np.mean(sampler.acceptance_fraction)),
        autocorrelation_time_log_sref=tau_log_s,
        autocorrelation_time_alpha=tau_alpha,
        n_walkers=n_walkers, n_steps=n_steps, burn_in=burn_in, thin=thin,
        n_posterior_samples=len(flat), prior_log_sref_min=log_s_min,
        prior_log_sref_max=log_s_max, prior_alpha_min=alpha_min,
        prior_alpha_max=alpha_max,
    )
    posterior = {
        "log_s_ref_jy": flat[:, 0],
        "alpha": a_samples,
        "s_ref_jy": s_samples,
        "log_probability": sampler.get_log_prob(discard=burn_in, thin=thin, flat=True),
    }
    return fit, posterior


def calculate_ratio(numerator_flux, denominator_flux, numerator_error, denominator_error):
    """Calculate flux ratios and independent-error propagation safely.

    Invalid or zero flux densities are returned as NaN and are subsequently
    excluded by the ratio-fitting and plotting masks.
    """
    numerator_flux = np.asarray(numerator_flux, dtype=float)
    denominator_flux = np.asarray(denominator_flux, dtype=float)
    numerator_error = np.asarray(numerator_error, dtype=float)
    denominator_error = np.asarray(denominator_error, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        ratio = numerator_flux / denominator_flux
        ratio_error = np.abs(ratio) * np.sqrt(
            (numerator_error / numerator_flux) ** 2
            + (denominator_error / denominator_flux) ** 2
        )
    valid = (
        np.isfinite(numerator_flux) & np.isfinite(denominator_flux)
        & np.isfinite(numerator_error) & np.isfinite(denominator_error)
        & (numerator_flux != 0.0) & (denominator_flux != 0.0)
        & (numerator_error > 0.0) & (denominator_error > 0.0)
    )
    ratio = np.where(valid, ratio, np.nan)
    ratio_error = np.where(valid, ratio_error, np.nan)
    return ratio, ratio_error


def fit_ratio_power_law(numerator, denominator, frequency_ghz, ratio, ratio_error,
                        reference_frequency_ghz=15.0) -> RatioFit:
    label = f"{numerator}/{denominator}"
    mask = (
        np.isfinite(frequency_ghz) & np.isfinite(ratio) & np.isfinite(ratio_error)
        & (frequency_ghz > 0) & (ratio > 0) & (ratio_error > 0)
    )
    nu = np.asarray(frequency_ghz[mask], dtype=float)
    values = np.asarray(ratio[mask], dtype=float)
    sigma = np.asarray(ratio_error[mask], dtype=float)
    if len(nu) < 3:
        raise ValueError(f"{label}: at least three valid ratios are required.")
    weights = 1.0 / sigma**2
    weighted_mean = float(np.sum(weights * values) / np.sum(weights))
    weighted_mean_error = float(np.sqrt(1.0 / np.sum(weights)))
    constant_chi_square = float(np.sum(((values - weighted_mean) / sigma) ** 2))
    constant_dof = len(values) - 1
    model = lambda x, ratio_ref, delta_alpha: ratio_power_law(x, ratio_ref, delta_alpha, reference_frequency_ghz)
    log_slope = np.polyfit(np.log(nu), np.log(values), 1)[0]
    popt, pcov = curve_fit(model, nu, values, p0=(weighted_mean, float(log_slope)),
                           sigma=sigma, absolute_sigma=True, maxfev=100000)
    chi_square = float(np.sum(((values - model(nu, *popt)) / sigma) ** 2))
    dof = len(values) - 2
    errors = np.sqrt(np.diag(pcov))
    return RatioFit(
        label=label, numerator=numerator, denominator=denominator,
        reference_frequency_ghz=reference_frequency_ghz,
        ratio_ref=float(popt[0]), ratio_ref_error=float(errors[0]),
        delta_alpha=float(popt[1]), delta_alpha_error=float(errors[1]),
        covariance_ratio_delta_alpha=float(pcov[0, 1]),
        correlation_ratio_delta_alpha=_correlation(pcov), chi_square=chi_square,
        reduced_chi_square=(chi_square / dof if dof > 0 else float("nan")),
        degrees_of_freedom=dof, n_points=len(values), weighted_mean_ratio=weighted_mean,
        weighted_mean_error=weighted_mean_error, constant_chi_square=constant_chi_square,
        constant_reduced_chi_square=(constant_chi_square / constant_dof if constant_dof > 0 else float("nan")),
        fit_status="ok",
    )
