"""Per-spectral-window flag report from a UV-FITS file's weights.

DifMAP treats a visibility with weight <= 0 as flagged, and a fit on a selection
with no unflagged visibility fails with "There are no unflagged visibilities to
be inverted". This reads the weights of a calibrated (or raw) UV-FITS file and
counts, per IF and per channel, how many are unflagged, so a Stage 2 fit that
found nothing can be checked against the data rather than inferred from DifMAP's
output. The file is read in chunks of visibilities through a memory map, so a
multi-gigabyte file costs one sequential read and little memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from lenspipe.uvfits import header_axis_number

__all__ = ["FlagReport", "IfFlags", "flag_report"]


@dataclass
class IfFlags:
    if_number: int  # DifMAP numbering, from 1
    spw: int  # CASA numbering, from 0
    channels: int
    visibilities: int  # per channel: number of (visibility, stokes) weights
    unflagged_fraction: float  # over all channels and visibilities of this IF
    fully_flagged_channels: list[int] = field(default_factory=list)  # 0-based within the IF
    first_channel: int = 0  # DifMAP absolute channel numbers, from 1
    last_channel: int = 0

    @property
    def fully_flagged(self) -> bool:
        return self.unflagged_fraction == 0.0

    def to_dict(self) -> dict:
        return {
            "if_number": self.if_number,
            "spw": self.spw,
            "first_channel": self.first_channel,
            "last_channel": self.last_channel,
            "channels": self.channels,
            "unflagged_fraction": self.unflagged_fraction,
            "fully_flagged": self.fully_flagged,
            "fully_flagged_channels_0based": self.fully_flagged_channels,
        }


@dataclass
class FlagReport:
    path: Path
    n_visibilities: int
    n_if: int
    channels_per_if: int
    n_stokes: int
    ifs: list[IfFlags]

    @property
    def fully_flagged_ifs(self) -> list[IfFlags]:
        return [item for item in self.ifs if item.fully_flagged]

    @property
    def unflagged_fraction(self) -> float:
        if not self.ifs:
            return 0.0
        return float(np.mean([item.unflagged_fraction for item in self.ifs]))

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "n_visibilities": self.n_visibilities,
            "n_if": self.n_if,
            "channels_per_if": self.channels_per_if,
            "n_stokes": self.n_stokes,
            "unflagged_fraction": self.unflagged_fraction,
            "fully_flagged_ifs": [item.if_number for item in self.fully_flagged_ifs],
            "fully_flagged_spws": [item.spw for item in self.fully_flagged_ifs],
            "ifs": [item.to_dict() for item in self.ifs],
        }

    def lines(self) -> list[str]:
        """Human-readable summary: one line per IF, with the fully flagged ones first."""
        out = [
            f"{self.path.name}: {self.n_visibilities} visibilities, {self.n_if} IF(s) x {self.channels_per_if} "
            f"channel(s), {self.n_stokes} Stokes; {100 * self.unflagged_fraction:.1f}% unflagged overall"
        ]
        dead = self.fully_flagged_ifs
        if dead:
            out.append(
                "FULLY FLAGGED: "
                + ", ".join(f"IF {item.if_number} (spw {item.spw}, channels {item.first_channel}-{item.last_channel})" for item in dead)
                + "  -> a Stage 2 fit of these finds no data"
            )
        else:
            out.append("No IF is fully flagged.")
        for item in self.ifs:
            note = ""
            if item.fully_flagged:
                note = "  FULLY FLAGGED"
            elif item.fully_flagged_channels:
                runs = _runs(item.fully_flagged_channels)
                note = f"  {len(item.fully_flagged_channels)} channel(s) fully flagged: " + ", ".join(runs)
            out.append(f"  IF {item.if_number:>3} (spw {item.spw:>2})  {100 * item.unflagged_fraction:6.1f}% unflagged{note}")
        return out


def _runs(values: list[int]) -> list[str]:
    runs: list[str] = []
    start = prev = None
    for value in values:
        if start is None:
            start = prev = value
        elif value == prev + 1:
            prev = value
        else:
            runs.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = value
    if start is not None:
        runs.append(f"{start}-{prev}" if start != prev else str(start))
    return runs


def flag_report(path: Path, chunk_visibilities: int = 2048) -> FlagReport:
    """Count unflagged weights per IF and channel of a random-groups UV-FITS file."""
    path = Path(path)
    with fits.open(path, memmap=True) as hdul:
        hdu = hdul[0]
        header = hdu.header
        data = hdu.data
        if data is None or not hasattr(data, "data"):
            raise ValueError(f"{path}: not a random-groups UV-FITS file")
        array = data.data  # shape (nvis, ...regular axes in reverse FITS order...)
        naxis = int(header.get("NAXIS", 0))
        # Random groups: FITS axis 1 is the empty group axis, axes 2..NAXIS are the regular
        # axes. astropy exposes the per-group array as (ngroups, axis NAXIS, ..., axis 2),
        # so FITS axis k is numpy axis NAXIS - k + 1 (axis 0 is the group index).
        def np_axis(fits_axis: int | None) -> int | None:
            return None if fits_axis is None else naxis - fits_axis + 1

        complex_axis = np_axis(header_axis_number(header, "COMPLEX"))
        freq_axis = np_axis(header_axis_number(header, "FREQ"))
        if_axis = np_axis(header_axis_number(header, "IF"))
        stokes_axis = np_axis(header_axis_number(header, "STOKES"))
        if complex_axis is None or freq_axis is None:
            raise ValueError(f"{path}: cannot find the COMPLEX and FREQ axes in the header")
        shape = array.shape
        n_vis = int(shape[0])
        n_if = int(shape[if_axis]) if if_axis is not None else 1
        n_chan = int(shape[freq_axis])
        n_stokes = int(shape[stokes_axis]) if stokes_axis is not None else 1

        unflagged = np.zeros((n_if, n_chan), dtype=np.int64)
        for start in range(0, n_vis, chunk_visibilities):
            block = np.asarray(array[start:start + chunk_visibilities])
            weights = np.take(block, 2, axis=complex_axis)  # (re, im, weight) -> weight
            # Collapse every axis except visibility, IF and channel.
            positive = weights > 0
            keep = {freq_axis}
            if if_axis is not None:
                keep.add(if_axis)
            # axis numbers refer to the full array; after taking the COMPLEX axis they shift.
            def shifted(axis: int) -> int:
                return axis - 1 if axis > complex_axis else axis
            reduce_axes = tuple(
                axis for axis in range(1, positive.ndim)
                if axis not in {shifted(a) for a in keep}
            )
            counts = positive.sum(axis=reduce_axes) if reduce_axes else positive
            counts = counts.sum(axis=0)  # over the visibilities in this block
            if if_axis is None:
                unflagged[0] += counts.reshape(n_chan)
            else:
                if shifted(if_axis) > shifted(freq_axis):
                    counts = counts.T
                unflagged += counts.reshape(n_if, n_chan)

    per_cell = n_vis * n_stokes  # weights per (IF, channel)
    ifs: list[IfFlags] = []
    for index in range(n_if):
        row = unflagged[index]
        fraction = float(row.sum()) / float(per_cell * n_chan) if per_cell and n_chan else 0.0
        dead = [int(c) for c in np.flatnonzero(row == 0)]
        ifs.append(
            IfFlags(
                if_number=index + 1, spw=index, channels=n_chan, visibilities=per_cell,
                unflagged_fraction=fraction, fully_flagged_channels=dead,
                first_channel=index * n_chan + 1, last_channel=(index + 1) * n_chan,
            )
        )
    return FlagReport(path=path, n_visibilities=n_vis, n_if=n_if, channels_per_if=n_chan, n_stokes=n_stokes, ifs=ifs)
