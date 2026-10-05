"""The normalised-ratio scatter statistic, applied to each image's reference flux over time."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.ui.interactive import combined_figures
from tests.conftest import write_synthetic_uvfits


@pytest.fixture
def combined(project: Path, fake_difmap: Path) -> Path:
    # Three visits so the sample standard deviation has two degrees of freedom.
    write_synthetic_uvfits(project / "inputs" / "MG0414.C.uvfits", seed=3, date_obs="2022-07-19T04:45:00.0")
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage2={"mode": "channel", "shards": 1},
        stage3={"figure_formats": ["png"], "images": {"enabled": False}},
    )
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok
    return project / "stage3" / "combined" / "MG0414" / "channel"


def test_normalised_reference_fluxes_table_plot_and_sigma(combined: Path) -> None:
    tables = combined / "tables"
    assert (combined / "plots" / "MG0414.channel.normalised_reference_fluxes_vs_mjd.png").is_file()
    normalised = pd.read_csv(tables / "MG0414.channel.normalised_reference_fluxes_vs_mjd.csv")
    reference = pd.read_csv(tables / "MG0414.channel.reference_fluxes_vs_mjd.csv")
    assert list(normalised["epoch"]) == list(reference["epoch"]) == ["A", "B", "C"]

    for group in ("A1", "A2", "B", "C"):
        values = reference[f"{group}_sref_jy"].to_numpy()
        errors = reference[f"{group}_sref_error_jy"].to_numpy()
        weights = 1.0 / errors**2
        weighted_mean = np.sum(weights * values) / np.sum(weights)
        expected_normalised = values / weighted_mean
        assert np.allclose(normalised[f"{group}_normalised"], expected_normalised)
        assert np.isclose(normalised[f"{group}_all_epoch_weighted_mean_jy"].iloc[0], weighted_mean)
        # The quoted sigma is the sample standard deviation (ddof=1) across visits, in per cent,
        # with every visit counting equally: the same definition as for the normalised ratios.
        expected_sigma = 100.0 * np.std(expected_normalised, ddof=1)
        assert np.isclose(normalised[f"{group}_sigma_percent"].iloc[0], expected_sigma)
        assert np.isclose(normalised[f"{group}_sigma_weighted_percent"].iloc[0], expected_sigma)
        plain_mean = values.mean()
        expected_unweighted = 100.0 * np.sqrt(np.sum((values - plain_mean) ** 2) / (values.size * plain_mean))
        assert np.isclose(normalised[f"{group}_sigma_unweighted_percent"].iloc[0], expected_unweighted)
        assert np.isclose(normalised[f"{group}_unweighted_mean_jy"].iloc[0], plain_mean)
        assert normalised[f"{group}_sigma_percent"].nunique() == 1

    payload = json.loads((tables / "MG0414.channel.combined_fits.json").read_text())
    scatter = payload["normalised_reference_flux_scatter"]
    assert set(scatter) == {"A1", "A2", "B", "C"}
    assert scatter["A1"]["n_points"] == 3 and scatter["A1"]["fit_status"] == "ok"
    rows = pd.read_csv(tables / "MG0414.channel.combined_fits.csv")
    flux_rows = rows[rows["product"] == "normalised_reference_flux"]
    assert sorted(flux_rows["label"]) == sorted(["A1", "A2", "B", "C"] * 2)
    assert sorted(set(flux_rows["parameter"])) == ["sigma_unweighted_percent", "sigma_weighted_percent"]
    ratio_rows = rows[rows["product"] == "normalised_flux_ratio"]
    assert sorted(set(ratio_rows["parameter"])) == ["sigma_unweighted_percent", "sigma_weighted_percent"]

    meta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert "ddof=1" in meta["normalised_reference_flux_definition"]


def test_weighted_sigma_is_the_sample_standard_deviation_of_the_normalised_values() -> None:
    """Synthetic visits are identical, so pin the definition with hand-made values too."""
    from lenspipe.stage3.combined import calculate_normalised_scatter

    series = {"A1": {"values": [1.02, 0.97, 1.01, 1.00], "errors": [0.001, 0.1, 0.001, 0.001]}}
    result = calculate_normalised_scatter(series)["A1"]
    expected = 100.0 * np.std([1.02, 0.97, 1.01, 1.00], ddof=1)
    assert np.isclose(result.sigma_weighted_percent, expected)
    assert result.sigma_percent == result.sigma_weighted_percent  # alias for older readers
    assert result.n_points == 4 and result.degrees_of_freedom == 3
    # The large error on the second visit does not reduce its weight in sigma,
    # but it does enter the chi-square about unity.
    assert result.chi_square_about_unity < 1e3


def test_unweighted_sigma_follows_the_collaboration_formula() -> None:
    """sigma = sqrt(sum((R_i - mean)^2) / (N * mean)), plain mean, errors ignored."""
    from lenspipe.stage3.combined import calculate_normalised_scatter, unweighted_scatter

    raw = np.array([0.80, 0.84, 0.78, 0.82])
    mean = raw.mean()
    expected = np.sqrt(np.sum((raw - mean) ** 2) / (raw.size * mean))
    sigma, reported_mean = unweighted_scatter(raw)
    assert np.isclose(sigma, expected) and np.isclose(reported_mean, mean)

    # Through the normalised-series path the raw values are recovered from the weighted mean.
    errors = np.array([0.01, 0.05, 0.01, 0.01])
    weights = 1.0 / errors**2
    weighted_mean = np.sum(weights * raw) / np.sum(weights)
    series = {"A2/A1": {"values": raw / weighted_mean, "errors": errors / weighted_mean,
                        "all_epoch_weighted_mean": weighted_mean}}
    result = calculate_normalised_scatter(series)["A2/A1"]
    assert np.isclose(result.sigma_unweighted_percent, 100.0 * expected)
    assert np.isclose(result.unweighted_mean, mean)
    # Changing one error bar changes the weighted sigma but not the unweighted one.
    errors2 = errors.copy()
    errors2[1] = 0.5
    weighted_mean2 = np.sum((1 / errors2**2) * raw) / np.sum(1 / errors2**2)
    series2 = {"A2/A1": {"values": raw / weighted_mean2, "errors": errors2 / weighted_mean2,
                         "all_epoch_weighted_mean": weighted_mean2}}
    result2 = calculate_normalised_scatter(series2)["A2/A1"]
    assert np.isclose(result2.sigma_unweighted_percent, result.sigma_unweighted_percent)
    assert not np.isclose(result2.sigma_weighted_percent, result.sigma_weighted_percent)


def test_interactive_combined_view_shows_normalised_fluxes_with_sigma(combined: Path) -> None:
    figures = combined_figures(combined)
    figure = figures["Normalised reference flux vs MJD"]
    names = [trace["name"] for trace in figure["data"]]
    assert len(names) == 4 and all("σw = " in name and "σu = " in name and name.endswith("%") for name in names)
    assert names[0].startswith("A1")
    assert len(figure["data"][0]["x"]) == 3
