"""Interactive Stage 3 figures carry spw and channel in every point's hover data."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.ui.interactive import (
    SpwLayout,
    combined_figures,
    load_visit,
    ratio_figure,
    spectrum_figure,
    spw_label,
)
from lenspipe.uvfits import get_uvfits_spw_layout


def test_spw_layout_from_synthetic_uvfits(project: Path) -> None:
    assert get_uvfits_spw_layout(project / "inputs" / "MG0414.A.uvfits") == {"n_spw": 2, "channels_per_spw": 4}


@pytest.mark.parametrize(
    "first, last, cps, expected",
    [
        (1, 1, 64, "spw 1 ch 1"),
        (64, 64, 64, "spw 1 ch 64"),
        (65, 65, 64, "spw 2 ch 1"),
        (3033, 3033, 64, "spw 48 ch 25"),
        (65, 128, 64, "spw 2 ch 1-64"),
        (67, 126, 64, "spw 2 ch 3-62"),
        (60, 70, 64, "spw 1 ch 60 to spw 2 ch 6"),
    ],
)
def test_spw_label(first: int, last: int, cps: int, expected: str) -> None:
    assert spw_label(first, last, SpwLayout(cps, None, "test")) == expected


def test_layout_sources_in_priority_order() -> None:
    assert SpwLayout.from_metadata({"spectral_windows": {"n_spw": 48, "channels_per_spw": 64}}).source == "stage2 metadata"
    assert SpwLayout.from_metadata({"mode": "if", "channels_per_if": 32}).channels_per_spw == 32
    assert SpwLayout.from_metadata({"mode": "channel"}, {"final_if_selfcal": {"channels_per_if": 16}}).channels_per_spw == 16
    assumed = SpwLayout.from_metadata({"mode": "channel"})
    assert assumed.channels_per_spw == 64 and assumed.source == "assumed"


@pytest.fixture
def stage3_project(project: Path, fake_difmap: Path) -> Path:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg.with_overrides(stage2={"mode": "channel", "shards": 2}), reporter=quiet, workers=1)
    assert run_stage3(project, cfg.with_overrides(stage3={"figure_formats": ["png"]}), reporter=quiet, workers=1).ok
    return project


def test_stage2_metadata_records_spectral_windows(stage3_project: Path) -> None:
    meta = json.loads((stage3_project / "stage2" / "MG0414.A" / "MG0414.A.channel.stage2.metadata.json").read_text())
    assert meta["spectral_windows"] == {"n_spw": 2, "channels_per_spw": 4}


def test_per_visit_figures_have_hover_with_spw_and_channel(stage3_project: Path) -> None:
    visit = load_visit(stage3_project / "stage3" / "MG0414.A" / "channel")
    assert visit.layout.source == "stage2 metadata" and visit.layout.channels_per_spw == 4
    assert visit.groups == ["A1", "A2", "B", "C"] and visit.reference_group == "A1"

    spectrum = spectrum_figure(visit)
    markers = [t for t in spectrum["data"] if t["mode"] == "markers"]
    lines = [t for t in spectrum["data"] if t["mode"] == "lines"]
    assert [t["name"] for t in markers] == ["A1", "A2", "B", "C"] and len(lines) == 4
    a1 = markers[0]
    assert len(a1["x"]) == 8 and len(a1["customdata"]) == 8
    assert a1["customdata"][4][:2] == [5, "spw 2 ch 1"]  # channel 5 of an 8-channel, 2-spw file
    assert "spw" in a1["hovertemplate"] or "customdata[1]" in a1["hovertemplate"]
    assert a1["error_y"]["visible"] is True and len(a1["error_y"]["array"]) == 8
    assert "power law" in lines[0]["hovertemplate"]

    ratios = ratio_figure(visit)
    assert ratios is not None
    ratio_markers = [t for t in ratios["data"] if t["mode"] == "markers"]
    assert [t["name"] for t in ratio_markers] == ["A2/A1", "B/A1", "C/A1"]
    assert ratio_markers[0]["customdata"][0][1] == "spw 1 ch 1"
    assert any("weighted mean" in t["hovertemplate"] for t in ratios["data"] if t["mode"] == "lines")


def test_combined_figures_cover_spectra_ratios_and_mjd_series(stage3_project: Path) -> None:
    figures = combined_figures(stage3_project / "stage3" / "combined" / "MG0414" / "channel")
    assert set(figures) >= {
        "Average spectrum (all epochs)", "Average flux ratios (all epochs)",
        "Reference-frequency flux vs MJD", "Weighted flux ratios vs MJD", "R_cusp vs MJD",
    }
    average = figures["Average spectrum (all epochs)"]["data"][0]
    assert average["customdata"][0][1] == "spw 1 ch 1" and average["customdata"][0][2] == 2  # 2 epochs
    mjd = figures["Reference-frequency flux vs MJD"]["data"][0]
    assert len(mjd["x"]) == 2 and mjd["customdata"] == [["A"], ["B"]]


def test_results_page_helper_dispatches(stage3_project: Path) -> None:
    from lenspipe.ui.pages_results import _interactive_figures

    per_visit = _interactive_figures(stage3_project / "stage3" / "MG0414.B" / "channel")
    assert set(per_visit) == {"Spectra", "Flux ratios"}
    combined = _interactive_figures(stage3_project / "stage3" / "combined" / "MG0414" / "channel")
    assert "R_cusp vs MJD" in combined
    assert _interactive_figures(stage3_project / "stage2" / "MG0414.A") == {}
