"""Plain-text fit reports with the full covariance matrix, per visit and for the combined product."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import numpy as np

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.stage3.fit_report import format_fit_report
from lenspipe.stage3.fitting import fit_power_law, fit_ratio_power_law
from lenspipe.ui.pages_results import Target


def test_fit_results_carry_the_full_covariance_matrix() -> None:
    nu = np.linspace(12, 18, 12)
    flux = 0.05 * (nu / 15.0) ** -0.7 * (1 + 0.01 * np.cos(nu))
    sigma = np.full_like(nu, 0.001)
    fit = fit_power_law("A", nu, flux, sigma, 15.0)
    cov = np.asarray(fit.covariance_matrix)
    assert cov.shape == (2, 2) and np.allclose(cov, cov.T)
    assert np.isclose(cov[0, 0], fit.s_ref_error_jy**2) and np.isclose(cov[1, 1], fit.alpha_error**2)
    assert cov[0, 1] == fit.covariance_sref_alpha
    ratio = fit_ratio_power_law("B", "A", nu, flux / 0.3, sigma / 0.3, 15.0)
    rcov = np.asarray(ratio.covariance_matrix)
    assert rcov.shape == (2, 2) and rcov[0, 1] == ratio.covariance_ratio_delta_alpha


def test_report_text_prints_matrix_and_goodness_of_fit() -> None:
    nu = np.linspace(12, 18, 8)
    flux = 0.05 * (nu / 15.0) ** -0.7
    fit = fit_power_law("A1", nu, flux, np.full_like(nu, 0.001), 15.0).as_dict()
    ratio = fit_ratio_power_law("A2", "A1", nu, flux * 0.8, np.full_like(nu, 0.001), 15.0).as_dict()
    text = format_fit_report("T", [fit], [ratio], notes=["note line"])
    assert text.startswith("T\n=\n\nnote line\n")
    assert "[A1]  power law  S(nu) = S_ref (nu / 15 GHz)^alpha   method: least_squares" in text
    assert "covariance matrix (Jy^2, Jy, 1), order (S_ref (Jy), alpha):" in text
    assert re.search(r"S_ref \(Jy\)\s+\S+\s+\S+\n\s+alpha\s+\S+\s+\S+", text)
    assert "cov(S_ref, alpha) =" in text and "reduced chi^2 =" in text
    assert "[A2/A1]  flux ratio" in text and "order (R_ref, delta_alpha)" in text
    assert "constant-ratio alternative" in text
    assert "not rescaled by the reduced chi^2" in text


def test_stage3_writes_reports_per_visit_and_combined_and_results_page_lists_them(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}}, stage3={"figure_formats": ["png"]}
    )
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg.with_overrides(stage2={"mode": "channel"}), reporter=quiet, workers=1)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok
    visit = project / "stage3" / "full-band" / "MG0414.A" / "channel"
    report = visit / "fits" / "MG0414.A.channel.fits.txt"
    assert report.is_file()
    text = report.read_text()
    meta = json.loads(next(visit.glob("*.stage3.metadata.json")).read_text())
    for group in meta["groups"]:
        assert f"[{group}]  power law" in text
    assert "MJD" in text.splitlines()[0] and "Error source for the per-channel points: rms" in text
    assert "flux ratio" in text
    combined = project / "stage3" / "full-band" / "combined" / "MG0414" / "channel"
    ctext = (combined / "tables" / "MG0414.channel.combined_fits.txt").read_text()
    assert "all-visit average spectrum" in ctext.splitlines()[0] and "covariance matrix" in ctext
    # The Results page target lists them beside the CSVs.
    assert [p.name for p in Target(visit).reports()] == ["MG0414.A.channel.fits.txt"]
    assert [p.name for p in Target(combined).reports()] == ["MG0414.channel.combined_fits.txt"]
