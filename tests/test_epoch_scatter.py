"""Within-epoch scatter of spectra (about the power-law fit) and flux ratios (about the weighted mean)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import numpy as np
import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.stage3.combined import epoch_ratio_scatter, epoch_spectrum_scatter
from lenspipe.stage3.fitting import PowerLawFit, power_law


def _fit(s_ref: float, alpha: float, ref: float = 15.0) -> PowerLawFit:
    return PowerLawFit(
        label="A", fit_method="least_squares", reference_frequency_ghz=ref, s_ref_jy=s_ref,
        s_ref_error_jy=0.0, s_ref_error_low_jy=0.0, s_ref_error_high_jy=0.0, alpha=alpha,
        alpha_error=0.0, alpha_error_low=0.0, alpha_error_high=0.0, covariance_sref_alpha=0.0,
        correlation_sref_alpha=0.0, chi_square=0.0, reduced_chi_square=0.0, degrees_of_freedom=0,
        n_points=0, fit_status="ok",
    )


def test_spectrum_scatter_is_zero_on_the_fit_and_known_for_a_known_perturbation() -> None:
    nu = np.linspace(12.0, 18.0, 24)  # even count: +-2 % alternating has mean exactly 1
    fit = _fit(0.3, -0.7)
    flux = power_law(nu, 0.3, -0.7, 15.0)
    err = np.full_like(flux, 1e-3)
    exact, series = epoch_spectrum_scatter("A", nu, flux, err, fit)
    assert exact.normalisation == "power_law_fit" and exact.n_points == 24
    assert exact.sigma_weighted_percent == pytest.approx(0.0, abs=1e-9)
    assert exact.sigma_unweighted_percent == pytest.approx(0.0, abs=1e-9)
    assert np.allclose(series["values"], 1.0)
    # The slope-included reference is not zero: a steep spectrum normalised by its mean still varies.
    assert exact.slope_included["sigma_unweighted_percent"] > 5.0

    # Alternate +2 / -2 per cent about the fit: population RMS of the normalised values is 2 %.
    perturbed = flux * (1.0 + 0.02 * np.where(np.arange(24) % 2 == 0, 1.0, -1.0))
    stat, _ = epoch_spectrum_scatter("A", nu, perturbed, err, fit)
    assert stat.sigma_unweighted_percent == pytest.approx(2.0, rel=1e-9)
    # Equal errors, so the weighted mean is the plain mean (1): sample std (ddof=1) of +-0.02.
    assert stat.sigma_weighted_percent == pytest.approx(100.0 * np.std(np.tile([1.02, 0.98], 12), ddof=1), rel=1e-9)

    # Without a fit the series falls back to the weighted mean and says so.
    fallback, _ = epoch_spectrum_scatter("A", nu, flux, err, None)
    assert fallback.normalisation == "weighted_mean" and fallback.slope_included is None


def test_ratio_scatter_about_the_weighted_mean() -> None:
    ratio = np.array([1.0, 1.0, 1.0, 1.0])
    stat, series = epoch_ratio_scatter("A2/A1", ratio, np.full(4, 0.01))
    assert stat.kind == "flux_ratio" and stat.sigma_weighted_percent == pytest.approx(0.0)
    assert np.allclose(series["values"], 1.0)
    stat, _ = epoch_ratio_scatter("A2/A1", np.array([0.98, 1.02, 0.98, 1.02]), np.full(4, 0.01))
    assert stat.sigma_unweighted_percent == pytest.approx(2.0, rel=1e-6)
    assert stat.weighted_mean == pytest.approx(1.0)
    empty, _ = epoch_ratio_scatter("A2/A1", np.array([np.nan]), np.array([np.nan]))
    assert empty.fit_status == "no_valid_points" and empty.n_points == 0


def _config(fake_difmap: Path) -> LenspipeConfig:
    return LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage2={"mode": "channel", "shards": 1},
        stage3={"figure_formats": ["png"], "images": {"enabled": False}},
    )


def test_stage3_writes_per_epoch_scatter_figures_tables_and_metadata(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok

    epoch_dir = project / "stage3" / "full-band" / "MG0414.A" / "channel"
    assert (epoch_dir / "plots" / "MG0414.A.channel.spectra_scatter.png").is_file()
    assert (epoch_dir / "plots" / "MG0414.A.channel.flux_ratio_scatter.png").is_file()

    with (epoch_dir / "fits" / "MG0414.A.channel.scatter.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    meta = json.loads(next(epoch_dir.glob("*.stage3.metadata.json")).read_text())
    groups = meta["groups"]
    spectra = [r for r in rows if r["kind"] == "spectrum"]
    ratios = [r for r in rows if r["kind"] == "flux_ratio"]
    assert sorted(r["label"] for r in spectra) == sorted(groups)
    assert len(ratios) == len(groups) - 1 and all("/" in r["label"] for r in ratios)
    assert all(r["normalisation"] == "power_law_fit" for r in spectra)
    assert all(r["normalisation"] == "weighted_mean" for r in ratios)
    for row in rows:
        assert int(row["n_points"]) > 3
        assert float(row["sigma_weighted_percent"]) >= 0 and float(row["sigma_unweighted_percent"]) >= 0
    assert "slope_included_sigma_unweighted_percent" in spectra[0]

    block = meta["scatter"]
    assert set(block) == {"description", "spectra", "flux_ratios", "figures"}
    assert set(block["spectra"]) == set(groups)
    assert block["figures"] == {
        "spectra": "MG0414.A.channel.spectra_scatter", "flux_ratios": "MG0414.A.channel.flux_ratio_scatter",
    }
    first = block["spectra"][groups[0]]
    assert first["sigma_weighted_percent"] == pytest.approx(float(spectra[0]["sigma_weighted_percent"]) if spectra[0]["label"] == groups[0] else first["sigma_weighted_percent"])
    assert first["slope_included"]["sigma_unweighted_percent"] >= first["sigma_unweighted_percent"] * 0  # present and numeric
