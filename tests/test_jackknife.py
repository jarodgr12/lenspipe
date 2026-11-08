"""stage3.jackknife: errors across Stage 2 products that each leave out one channel block."""

from __future__ import annotations

import io
import json
import math
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import plotting, run_stage3
from lenspipe.stage3.jackknife import jackknife_estimate, sample_key, with_jackknife_samples
from lenspipe.ui.commands import RunRequest, build_steps
from tests.conftest import FIXTURES, write_synthetic_uvfits

BLOCKS = ["spw0-3:0", "spw0-3:1", "spw0-3:2", "spw0-3:3"]  # one left-out channel in every window per sample
SAMPLE_TAGS = [f"if4_edge0_x_spw0-3_{n}" for n in range(4)]
SET_NAME = "if4_edge0_x_spw0-3_jackknife"


def test_jackknife_estimate_matches_the_delete_one_formula() -> None:
    mean, error, n = jackknife_estimate([1.0, 2.0, 3.0, 4.0])
    assert (mean, n) == (2.5, 4)
    assert error == pytest.approx(math.sqrt(3 / 4 * (1.5**2 + 0.5**2 + 0.5**2 + 1.5**2)))
    assert jackknife_estimate([1.0, float("nan"), 3.0])[2] == 2  # a missing sample is left out
    assert math.isnan(jackknife_estimate([5.0])[1]) and jackknife_estimate([])[2] == 0


@pytest.mark.parametrize(
    ("tag", "spec", "expected"),
    [
        ("if64_edge2_x_spw0-47_2-5", "spw0-47:2-5", ("if64_edge2", "spw0-47")),
        ("channel_x_spw3_0-3", "spw3:0-3", ("channel", "spw3")),
        ("if64_edge3_x_spw7", "spw7", None),  # a whole window left out is not a sample
        ("channel_x_1-4", "1-4", None),  # plain fit indices are not a channel block
        ("if64_edge2", None, None),
    ],
)
def test_sample_key_recognises_channel_block_products(tag: str, spec: str | None, expected) -> None:
    assert sample_key(tag, {"channel_exclusion": {"exclude_channels": spec}}) == expected


@pytest.fixture
def four_if_project(tmp_path: Path) -> Path:
    root = tmp_path / "MG0414"
    (root / "inputs").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "MG0414.gmod", root / "inputs" / "MG0414.gmod")
    write_synthetic_uvfits(root / "inputs" / "MG0414.A.uvfits", n_if=4, n_chan=4, seed=1)
    write_synthetic_uvfits(root / "inputs" / "MG0414.B.uvfits", n_if=4, n_chan=4, seed=2, date_obs="2022-05-02T09:10:11.0")
    return root


def test_stage3_jackknife_end_to_end(four_if_project: Path, fake_difmap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = four_if_project
    quiet = Reporter(stream=io.StringIO())
    base = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}},
        stage2={"mode": "if", "channels_per_if": 4, "shards": 1},
        stage3={"figure_formats": ["png"], "images": {"enabled": False}},
    )
    run_stage1(project, base, reporter=quiet, workers=1)
    for spec in [None, *BLOCKS, "spw3"]:  # the base product and a whole-window product are not samples
        results = run_stage2(project, base.with_overrides(stage2={"exclude_channels": spec}), reporter=quiet, workers=1)
        assert all(r.ok for r in results), [r.message for r in results]

    figures: dict[str, dict] = {}
    real_save = plotting.save_figure

    def recording_save(fig, base_path, dpi=300, bbox_inches="tight", transparent=False):
        fig.canvas.draw()
        figures[Path(base_path).name] = {
            "titles": [ax.get_title() for ax in fig.axes],
            "texts": [t.get_text() for ax in fig.axes for t in ax.texts] + [t.get_text() for t in fig.texts],
            "xtick": [ax.get_xticklabels()[0].get_fontsize() for ax in fig.axes if ax.get_xticklabels()],
        }
        real_save(fig, base_path, dpi=dpi, bbox_inches=bbox_inches, transparent=transparent)

    monkeypatch.setattr(plotting, "save_figure", recording_save)
    monkeypatch.setattr(plotting, "_save_figure", recording_save)
    summary = run_stage3(project, base.with_overrides(stage3={"jackknife": True}), reporter=quiet, workers=1)
    assert summary.ok

    stage3 = project / "stage3" / "full-band"
    out = stage3 / "combined" / "MG0414" / SET_NAME
    stem = f"MG0414.{SET_NAME}"
    meta = json.loads((out / f"{stem}.combined.stage3.metadata.json").read_text())
    assert meta["kind"] == "jackknife" and meta["n_samples"] == 4
    assert sorted(meta["samples"]) == SAMPLE_TAGS  # neither the base nor the whole-window product

    # Per epoch: recompute from each sample's own powerlaw_fits.csv, as the collaborators' script does.
    per_epoch = pd.read_csv(out / "tables" / f"{stem}.errors_per_epoch.csv")
    assert list(per_epoch["epoch"]) == ["A", "B"]
    for epoch in ("A", "B"):
        s_ref = []
        for tag in SAMPLE_TAGS:
            fits = pd.read_csv(stage3 / f"MG0414.{epoch}" / tag / "fits" / f"MG0414.{epoch}.{tag}.powerlaw_fits.csv")
            s_ref.append(1000 * float(fits.loc[fits.iloc[:, 0] == "A1", "s_ref_jy"].iloc[0]))
        s_ref = np.array(s_ref)
        expected_error = np.sqrt(3 / 4 * np.sum((s_ref - s_ref.mean()) ** 2))
        row = per_epoch.loc[per_epoch["epoch"] == epoch].iloc[0]
        assert row["A1_flux_mjy"] == pytest.approx(s_ref.mean())
        assert row["A1_flux_err_mjy"] == pytest.approx(expected_error) and expected_error > 0
        assert row["A1_n_samples"] == 4

    # All visits: recompute from each sample's combined_fits.csv.
    s_ref = []
    for tag in SAMPLE_TAGS:
        combined = pd.read_csv(stage3 / "combined" / "MG0414" / tag / "tables" / f"MG0414.{tag}.combined_fits.csv")
        pick = (combined["product"] == "average_spectrum") & (combined["label"] == "A1") & (combined["parameter"] == "s_ref_jy")
        s_ref.append(1000 * float(combined.loc[pick, "value"].iloc[0]))
    average = pd.read_csv(out / "tables" / f"{stem}.average_fits.csv")
    row = average.loc[(average["label"] == "A1") & (average["parameter"] == "s_ref_mjy")].iloc[0]
    assert row["value"] == pytest.approx(np.mean(s_ref))
    assert row["jackknife_error"] == pytest.approx(np.sqrt(3 / 4 * np.sum((np.array(s_ref) - np.mean(s_ref)) ** 2)))

    # Every figure written, all in the _mnras style: no titles, no annotations, larger tick labels.
    expected = {
        "average_flux_ratios_hist", "average_fluxes_hist", "fluxes_vs_mjd", "alphas_vs_mjd",
        "alpha_offsets_vs_mjd", "flux_ratios_vs_mjd", "normalised_flux_ratios_vs_mjd", "spectra_all_epochs",
    }
    assert {p.name[len(stem) + 1 : -len(".png")] for p in (out / "plots").glob("*.png")} == expected
    standard_tick = figures[f"MG0414.{SAMPLE_TAGS[0]}.average_spectrum"]["xtick"][0]
    for name in (f"{stem}.{figure}" for figure in expected):
        record = figures[name]
        assert set(record["titles"]) == {""}, name
        assert not [t for t in record["texts"] if t not in {"Frequency [GHz]", "Flux density [mJy]"}], name
        assert record["xtick"] and all(size == standard_tick + 2 for size in record["xtick"]), name
    # The Run page names one product: its jackknife set joins the run, other products do not.
    widen = lambda requested: with_jackknife_samples(project, None, requested, "full-band")  # noqa: E731
    assert widen({"if4_edge0"}) == {"if4_edge0", *SAMPLE_TAGS}
    assert widen({SAMPLE_TAGS[2]}) == set(SAMPLE_TAGS)
    assert widen({"if4_edge0_x_spw3"}) == {"if4_edge0_x_spw3"}
    assert widen(None) == set(SAMPLE_TAGS)

    assert {t.name for t in (out / "tables").glob("*.csv")} == {
        f"{stem}.{table}.csv" for table in ("samples", "average_fits", "errors_per_epoch", "spectra")
    }


def test_jackknife_off_writes_nothing_and_run_page_passes_the_switch(tmp_path: Path) -> None:
    assert LenspipeConfig().stage3.jackknife is False
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[3], stage3_jackknife=True))
    assert "--jackknife" in steps[0].argv
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[3]))
    assert "--jackknife" not in steps[0].argv and "--no-jackknife" not in steps[0].argv
