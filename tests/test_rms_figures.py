"""Stage 3 RMS diagnostics: residual RMS against frequency over the whole band, one marker per channel."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lenspipe.stage3 import plotting
from lenspipe.stage3.analysis import plot_epoch_rms_diagnostic, write_combined_rms_diagnostics


def _visit(epoch: str, mjd: float, scale: float) -> SimpleNamespace:
    frequency = np.array([12.5, 13.5, 14.5, 15.5, 16.5, 17.5])
    return SimpleNamespace(
        dataset=SimpleNamespace(epoch=epoch), mjd=mjd, fit_indices=np.arange(1, 7),
        frequency_ghz=frequency, rms_jy_per_beam=scale * np.array([1.0, 1.2, 0.9, 1.1, 1.0, 1.3]) * 1e-4,
    )


def test_rms_figures_use_frequency_and_unjoined_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, object] = {}

    def keep(fig, base_path, dpi=300, bbox_inches="tight", transparent=False):
        saved[Path(base_path).name] = fig

    monkeypatch.setattr(plotting, "save_figure", keep)
    visits = [_visit("A", 59653.5, 1.0), _visit("B", 59701.4, 1.5)]
    plot_epoch_rms_diagnostic(visits[0], tmp_path / "MG0414.A.channel.rms_vs_channel")
    write_combined_rms_diagnostics(visits, tmp_path, tmp_path, "MG0414.channel")
    assert len(saved) == 5

    for name, fig in saved.items():
        ax = fig.axes[0]
        assert ax.get_xlabel() == "Frequency [GHz]", name
        assert ax.get_xlim() == pytest.approx(plotting.SUMMARY_FREQUENCY_LIMITS_GHZ), name
        data = [line for line in ax.get_lines() if line.get_marker() == "o"]
        assert data, name
        for line in data:
            assert line.get_linestyle() == "None", name  # individual markers, nothing joining them
            assert list(line.get_xdata()) == pytest.approx(list(visits[0].frequency_ghz)), name
        references = [line for line in ax.get_lines() if line.get_marker() != "o"]
        assert all(line.get_linestyle() == "--" for line in references), name  # only the dashed 1 or 0 guide
