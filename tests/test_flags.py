"""lenspipe flags: which spectral windows are fully flagged, read from the UV-FITS weights."""

from __future__ import annotations

import json
from pathlib import Path

from astropy.io import fits
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.flags import flag_report
from tests.conftest import write_synthetic_uvfits


def _flag(path: Path, if_index: int, channels: slice | None = None) -> None:
    """Set weights <= 0 (DifMAP's flag) for one IF, optionally only some channels."""
    with fits.open(path, mode="update", memmap=False) as hdul:
        data = hdul[0].data.data  # (nvis, 1, 1, n_if, n_chan, 1, 3)
        sel = (slice(None), 0, 0, if_index, channels if channels is not None else slice(None), 0, 2)
        data[sel] = -1.0
        hdul.flush()


def test_flag_report_counts_per_if_and_channel(tmp_path: Path) -> None:
    uv = tmp_path / "x.uvfits"
    write_synthetic_uvfits(uv, n_if=4, n_chan=8, seed=3)
    _flag(uv, if_index=2)  # IF 3 entirely
    _flag(uv, if_index=0, channels=slice(0, 2))  # IF 1, first two channels
    report = flag_report(uv, chunk_visibilities=4)  # two chunks, exercises the accumulation
    assert (report.n_if, report.channels_per_if, report.n_stokes, report.n_visibilities) == (4, 8, 1, 6)
    assert [item.if_number for item in report.fully_flagged_ifs] == [3]
    assert report.ifs[2].spw == 2 and report.ifs[2].first_channel == 17 and report.ifs[2].last_channel == 24
    assert report.ifs[0].unflagged_fraction == 0.75 and report.ifs[0].fully_flagged_channels == [0, 1]
    assert report.ifs[1].unflagged_fraction == 1.0 and report.ifs[3].unflagged_fraction == 1.0
    lines = report.lines()
    assert "FULLY FLAGGED: IF 3 (spw 2, channels 17-24)" in lines[1]
    assert any("IF   1 (spw  0)    75.0% unflagged  2 channel(s) fully flagged: 0-1" in line for line in lines)
    payload = report.to_dict()
    assert payload["fully_flagged_ifs"] == [3] and payload["fully_flagged_spws"] == [2]


def test_flag_report_clean_file(tmp_path: Path) -> None:
    uv = tmp_path / "clean.uvfits"
    write_synthetic_uvfits(uv, n_if=2, n_chan=4)
    report = flag_report(uv)
    assert not report.fully_flagged_ifs and report.unflagged_fraction == 1.0
    assert report.lines()[1] == "No IF is fully flagged."


def test_flags_cli_on_a_project_and_a_file(project: Path, fake_difmap: Path) -> None:
    runner = CliRunner()
    raw = project / "inputs" / "MG0414.B.uvfits"
    _flag(raw, if_index=1)
    # --raw inspects inputs/; exit 3 flags the fully flagged window for scripts.
    result = runner.invoke(app, ["flags", str(project), "--raw"])
    assert result.exit_code == 3, result.output
    assert "MG0414.A.uvfits" in result.output and "No IF is fully flagged." in result.output
    assert "MG0414.B.uvfits" in result.output and "FULLY FLAGGED: IF 2 (spw 1, channels 5-8)" in result.output
    # one file, JSON
    result = runner.invoke(app, ["flags", "--file", str(raw), "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload[0]["fully_flagged_ifs"] == [2]
    # No calibrated data yet: a clean error, not a traceback.
    result = runner.invoke(app, ["flags", str(project)])
    assert result.exit_code == 1 and "no UV-FITS files found" in result.output
    # A clean file exits 0.
    result = runner.invoke(app, ["flags", "--file", str(project / "inputs" / "MG0414.A.uvfits")])
    assert result.exit_code == 0 and "No IF is fully flagged." in result.output
