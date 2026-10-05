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
    real_save = plotting.save_figure

    def inspecting_save(fig, base_path, dpi=300, bbox_inches="tight"):
        fig.canvas.draw()
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
