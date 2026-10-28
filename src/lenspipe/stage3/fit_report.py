"""A plain-text report of every spectral fit: parameters, errors, covariance, goodness of fit.

Written next to the CSV/JSON fit tables so a fit can be read, quoted or pasted
without a spreadsheet. One block per fit; the covariance matrix is printed in
full with its parameter order, and the correlation is given beside it so the
off-diagonal term can be read either way.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["format_fit_report", "write_fit_report"]


def _fmt(value: Any, digits: int = 6) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(number):
        return "nan"
    return f"{number:.{digits}g}"


def _matrix_lines(names: list[str], matrix: list[list[float]] | None, unit_note: str) -> list[str]:
    if matrix is None:
        return ["  covariance matrix: not available"]
    width = max(len(n) for n in names) + 2
    lines = [f"  covariance matrix {unit_note}, order ({', '.join(names)}):"]
    lines.append("  " + " " * width + "".join(f"{n:>16}" for n in names))
    for name, row in zip(names, matrix, strict=True):
        lines.append("  " + f"{name:<{width}}" + "".join(f"{_fmt(v):>16}" for v in row))
    return lines


def _power_law_block(fit: dict[str, Any]) -> list[str]:
    ref = fit.get("reference_frequency_ghz")
    s = fit.get("s_ref_jy")
    se = fit.get("s_ref_error_jy")
    a = fit.get("alpha")
    ae = fit.get("alpha_error")
    method = fit.get("fit_method", "least_squares")
    lines = [
        f"[{fit.get('label')}]  power law  S(nu) = S_ref (nu / {_fmt(ref, 4)} GHz)^alpha   method: {method}",
        f"  S_ref  = {_fmt(s)} Jy  +/- {_fmt(se)} Jy   ({_fmt(float(s) * 1e3 if s is not None else None, 5)} +/- {_fmt(float(se) * 1e3 if se is not None else None, 3)} mJy)",
        f"  alpha  = {_fmt(a)}  +/- {_fmt(ae)}",
    ]
    if method == "emcee":
        lines.append(
            f"  asymmetric: S_ref -{_fmt(fit.get('s_ref_error_low_jy'))} +{_fmt(fit.get('s_ref_error_high_jy'))} Jy, "
            f"alpha -{_fmt(fit.get('alpha_error_low'))} +{_fmt(fit.get('alpha_error_high'))}  (16th-84th percentiles)"
        )
    lines += _matrix_lines(["S_ref (Jy)", "alpha"], fit.get("covariance_matrix"), "(Jy^2, Jy, 1)")
    lines.append(
        f"  cov(S_ref, alpha) = {_fmt(fit.get('covariance_sref_alpha'))}   corr = {_fmt(fit.get('correlation_sref_alpha'), 4)}"
    )
    lines.append(
        f"  chi^2 = {_fmt(fit.get('chi_square'))}   dof = {fit.get('degrees_of_freedom')}   "
        f"reduced chi^2 = {_fmt(fit.get('reduced_chi_square'), 4)}   points = {fit.get('n_points')}   status = {fit.get('fit_status')}"
    )
    if method == "emcee":
        lines.append(
            f"  emcee: walkers {fit.get('n_walkers')}, steps {fit.get('n_steps')}, burn-in {fit.get('burn_in')}, thin {fit.get('thin')}, "
            f"samples {fit.get('n_posterior_samples')}, acceptance {_fmt(fit.get('acceptance_fraction'), 3)}"
        )
    return lines


def _ratio_block(fit: dict[str, Any]) -> list[str]:
    ref = fit.get("reference_frequency_ghz")
    lines = [
        f"[{fit.get('label')}]  flux ratio  R(nu) = R_ref (nu / {_fmt(ref, 4)} GHz)^delta_alpha",
        f"  R_ref        = {_fmt(fit.get('ratio_ref'))}  +/- {_fmt(fit.get('ratio_ref_error'))}",
        f"  delta_alpha  = {_fmt(fit.get('delta_alpha'))}  +/- {_fmt(fit.get('delta_alpha_error'))}",
    ]
    lines += _matrix_lines(["R_ref", "delta_alpha"], fit.get("covariance_matrix"), "(1)")
    lines.append(
        f"  cov(R_ref, delta_alpha) = {_fmt(fit.get('covariance_ratio_delta_alpha'))}   corr = {_fmt(fit.get('correlation_ratio_delta_alpha'), 4)}"
    )
    lines.append(
        f"  chi^2 = {_fmt(fit.get('chi_square'))}   dof = {fit.get('degrees_of_freedom')}   "
        f"reduced chi^2 = {_fmt(fit.get('reduced_chi_square'), 4)}   points = {fit.get('n_points')}   status = {fit.get('fit_status')}"
    )
    lines.append(
        f"  constant-ratio alternative: weighted mean = {_fmt(fit.get('weighted_mean_ratio'))} +/- {_fmt(fit.get('weighted_mean_error'))}, "
        f"chi^2 = {_fmt(fit.get('constant_chi_square'))}, reduced chi^2 = {_fmt(fit.get('constant_reduced_chi_square'), 4)}"
    )
    return lines


def format_fit_report(
    title: str,
    power_law_fits: Iterable[dict[str, Any]],
    ratio_fits: Iterable[dict[str, Any]] = (),
    *,
    notes: Iterable[str] = (),
) -> str:
    """The report text. ``power_law_fits`` and ``ratio_fits`` are fit dicts (``as_dict()``)."""
    out: list[str] = [title, "=" * len(title), ""]
    for note in notes:
        out.append(note)
    if notes:
        out.append("")
    out.append("Errors are 1-sigma from the fit covariance (least squares: absolute_sigma=True, i.e. the per-point")
    out.append("uncertainties are taken at face value, not rescaled by the reduced chi^2; emcee: posterior sample covariance).")
    out.append("")
    power = list(power_law_fits)
    if power:
        out.append("Spectral fits")
        out.append("-" * 13)
        for fit in power:
            out += _power_law_block(fit)
            out.append("")
    ratios = list(ratio_fits)
    if ratios:
        out.append("Flux-ratio fits")
        out.append("-" * 15)
        for fit in ratios:
            out += _ratio_block(fit)
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def write_fit_report(path: Path, title: str, power_law_fits, ratio_fits=(), *, notes=()) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_fit_report(title, power_law_fits, ratio_fits, notes=notes), encoding="utf-8")
    return path
