"""Stage 3 figures keep legends, fit annotations and statistics out of the data area."""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

from lenspipe.config import LenspipeConfig  # noqa: E402
from lenspipe.progress import Reporter  # noqa: E402
from lenspipe.stage1 import run_stage1  # noqa: E402
from lenspipe.stage2 import run_stage2  # noqa: E402
from lenspipe.stage3 import plotting, run_stage3  # noqa: E402
from lenspipe.stage3.plotting import caption_below, legend_outside  # noqa: E402


def test_legend_outside_sits_to_the_right_of_the_axes() -> None:
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.plot([0, 1], [0, 1], label="A1")
    legend = legend_outside(ax)
    fig.canvas.draw()
    axes_box = ax.get_window_extent()
    legend_box = legend.get_window_extent()
    assert legend_box.x0 >= axes_box.x1  # entirely beside the data area
    plt.close(fig)


def test_caption_below_sits_under_the_axes_and_skips_empty_lines() -> None:
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_xlabel("Frequency [GHz]")
    text = caption_below(ax, ["A1: fit", "", "B: fit"])
    fig.canvas.draw()
    axes_box = ax.get_window_extent()
    text_box = text.get_window_extent()
    assert text_box.y1 <= axes_box.y0  # entirely under the data area
    assert text.get_text() == "A1: fit\nB: fit"
    assert caption_below(ax, []) is None
    plt.close(fig)


def test_no_stage3_figure_draws_text_inside_its_axes(project: Path, fake_difmap: Path, monkeypatch) -> None:
    """Record every text/legend placement while Stage 3 runs and check none is inside the axes."""
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage2={"mode": "channel", "shards": 1},
        stage3={"figure_formats": ["png"], "images": {"enabled": False}},
    )
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)

    inside: list[str] = []
    mnras: dict[str, dict] = {}
    standard: dict[str, dict] = {}
    real_save = plotting.save_figure

    def inspecting_save(fig, base_path, dpi=300, bbox_inches="tight"):
        fig.canvas.draw()
        name = Path(base_path).name
        bottom = fig.axes[-1]  # stacked panels: the bottom one carries the x-axis label and ticks
        record = {
            "title": "".join(ax.get_title() for ax in fig.axes),
            "texts": [t.get_text() for ax in fig.axes for t in ax.texts],
            "xlabel": bottom.xaxis.label.get_fontsize(), "xtick": bottom.get_xticklabels()[0].get_fontsize(),
            "ylabels": [ax.get_ylabel() for ax in fig.axes],
            "legend_hits": [hit for ax in fig.axes for hit in _legend_hits(ax)],
            "legend": any(ax.get_legend() is not None for ax in fig.axes),
        }
        (mnras if name.endswith("_mnras") else standard)[name] = record
        if name.endswith("_mnras"):
            real_save(fig, base_path, dpi=dpi, bbox_inches=bbox_inches)
            return
        for ax in fig.axes:
            box = ax.get_window_extent()
            for text in ax.texts:
                tb = text.get_window_extent()
                if tb.x0 > box.x0 and tb.x1 < box.x1 and tb.y0 > box.y0 and tb.y1 < box.y1:
                    inside.append(f"{Path(base_path).name}: text {text.get_text()[:30]!r}")
            legend = ax.get_legend()
            if legend is not None:
                lb = legend.get_window_extent()
                if lb.x0 < box.x1 and lb.x1 > box.x0 and lb.y0 < box.y1 and lb.y1 > box.y0:
                    inside.append(f"{Path(base_path).name}: legend")
        real_save(fig, base_path, dpi=dpi, bbox_inches=bbox_inches)

    monkeypatch.setattr(plotting, "save_figure", inspecting_save)
    monkeypatch.setattr(plotting, "_save_figure", inspecting_save)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok
    assert inside == []

    # The _mnras versions: no title or annotations, axis fonts 2 pt larger, the key inside but on no data.
    assert sorted(name.split(".")[-1] for name in mnras) == [
        "average_flux_ratios_mnras", "average_spectrum_mnras", "normalised_weighted_flux_ratios_vs_mjd_mnras",
        "rcusp_vs_mjd_mnras",
    ]
    for name, record in mnras.items():
        plain = standard[name[: -len("_mnras")]]
        assert record["title"] == "" and record["texts"] == [], name
        assert record["xlabel"] == plain["xlabel"] + 2 and record["xtick"] == plain["xtick"] + 2, name
        assert record["legend_hits"] == [], f"{name}: key covers {record['legend_hits']}"
    assert mnras[next(n for n in mnras if "average_spectrum" in n)]["legend"]
    assert mnras[next(n for n in mnras if "average_flux_ratios" in n)]["legend"]
    # Normalised ratios: no scatter annotations (checked above), each panel named by its ratio alone.
    normalised = next(n for n in mnras if "normalised_weighted" in n)
    assert mnras[normalised]["ylabels"] == ["A2/A1", "B/A1", "C/A1"]
    assert standard[normalised[: -len("_mnras")]]["texts"]  # the standard figure keeps its sigma annotations


def _legend_hits(ax) -> list[str]:
    """Data artists the legend box (inside the axes) touches: markers, lines or error bars."""
    legend = ax.get_legend()
    if legend is None:
        return []
    box = legend.get_window_extent()
    axes_box = ax.get_window_extent()
    if not (box.x0 >= axes_box.x0 and box.x1 <= axes_box.x1 and box.y0 >= axes_box.y0 and box.y1 <= axes_box.y1):
        return ["outside the axes"]
    hits = []
    for line in ax.get_lines():
        path = line.get_transform().transform_path(line.get_path())
        joined = line.get_linestyle() not in {"None", "none", "", " "}
        if joined and path.intersects_bbox(box, filled=False):
            hits.append(f"line {line.get_label()}")
        elif not joined and any(box.contains(x, y) for x, y in path.vertices):
            hits.append(f"markers {line.get_label()}")
    for collection in ax.collections:
        for path in collection.get_paths():
            if collection.get_transform().transform_path(path).intersects_bbox(box, filled=False):
                hits.append("error bar")
                break
    return hits


def test_legend_inside_clear_finds_room_when_every_corner_holds_data() -> None:
    """Data in every corner and the middle: the key goes into new headroom above the data, touching nothing."""
    import numpy as np

    from lenspipe.stage3.plotting import legend_inside_clear

    fig, ax = plt.subplots(figsize=(8, 8))
    x = np.linspace(12.0, 18.0, 60)
    for offset, label in ((0.0, "A1"), (0.5, "A2"), (1.0, "B")):
        ax.errorbar(x, np.full_like(x, 1.0 + offset), yerr=0.1, fmt="o", ms=4.5, label=label)
    ax.plot(x, 1.0 + (x - 12.0) / 6.0 * 1.0, color="black", linestyle="--")  # a diagonal through the middle
    ax.set_xlim(11.7, 18.3)
    ax.set_ylim(0.85, 2.15)  # the data reach every edge: no inside position is free
    legend = legend_inside_clear(ax)
    fig.canvas.draw()
    assert legend is not None and ax.get_ylim()[1] > 2.15  # room was made above the data
    assert _legend_hits(ax) == []
    plt.close(fig)

    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])  # nothing labelled: no key at all
    assert legend_inside_clear(ax) is None
    plt.close(fig)
