"""Per-shard Stage 2 inputs: a UV-FITS file holding only what one DifMAP process fits.

DifMAP's ``observe`` streams the whole input into a scratch copy in the process's
working directory, and ``select i`` then reads all of it, whatever the process goes
on to fit. With several shards per epoch every process therefore copied the full
calibrated file to fit a fraction of its channels. ``write_subset`` writes the IFs
one shard needs, and only the parallel hands when cross hands are present (Stokes I
is formed from RR and LL, or XX and YY), so the copy shrinks to that share.

The visibilities, weights, flags, u, v, w and times are copied bit for bit, the FREQ
reference of the header is kept and the AIPS FQ table keeps the offsets of the IFs
that remain, so every channel has the frequency it had in the full file. Channel
``c`` of the full file is channel ``c - channel_offset`` of the subset.

Checked against DifMAP 2.5q: per-channel ``modelfit`` models and residual RMS from
subset files equal those from the full file (flagged and deleted visibilities
included, down to one IF per file).
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

__all__ = ["SUBSET_HISTORY", "SubsetInfo", "UvfitsLayout", "if_span", "read_layout", "write_subset"]

BLOCK = 2880
CHUNK_BYTES = 64 << 20
# Stokes codes of the two parallel hands, as the first two planes of the STOKES axis.
PARALLEL_HANDS = ((-1.0, -2.0), (-5.0, -6.0))
SUBSET_HISTORY = "LENSPIPE SUBSET"


class SubsetCancelled(Exception):
    """The run was cancelled while a shard input was being written."""


@dataclass(frozen=True)
class UvfitsLayout:
    """What ``write_subset`` needs to know about a random-groups UV-FITS file."""

    n_if: int
    channels_per_if: int
    n_pol: int
    cross_hands: bool  # more than the two parallel hands are present and can be dropped

    @property
    def polarisation_fraction(self) -> float:
        return 2 / self.n_pol if self.cross_hands else 1.0


@dataclass(frozen=True)
class SubsetInfo:
    path: Path
    if_first: int  # 0-based, inclusive
    if_last: int
    n_pol: int
    channel_offset: int
    size_bytes: int


def _axis(header: fits.Header, ctype: str) -> int | None:
    for axis in range(2, int(header["NAXIS"]) + 1):
        if str(header.get(f"CTYPE{axis}", "")).strip().upper() == ctype:
            return axis
    return None


def _layout(header: fits.Header) -> UvfitsLayout:
    if int(header.get("BITPIX", 0)) != -32 or not header.get("GROUPS", False):
        raise ValueError("not a 32-bit floating-point random-groups file")
    if float(header.get("BSCALE", 1.0)) != 1.0 or float(header.get("BZERO", 0.0)) != 0.0:
        raise ValueError("scaled data (BSCALE/BZERO)")
    if_axis, freq_axis, stokes_axis = _axis(header, "IF"), _axis(header, "FREQ"), _axis(header, "STOKES")
    if if_axis is None or freq_axis is None:
        raise ValueError("no IF or FREQ axis")
    n_pol, cross_hands = 1, False
    if stokes_axis is not None:
        n_pol = int(header[f"NAXIS{stokes_axis}"])
        crval = float(header.get(f"CRVAL{stokes_axis}", 1.0))
        cdelt = float(header.get(f"CDELT{stokes_axis}", 1.0))
        crpix = float(header.get(f"CRPIX{stokes_axis}", 1.0))
        first_two = tuple(crval + (plane + 1 - crpix) * cdelt for plane in range(min(2, n_pol)))
        cross_hands = n_pol > 2 and first_two in PARALLEL_HANDS
    return UvfitsLayout(
        n_if=int(header[f"NAXIS{if_axis}"]),
        channels_per_if=int(header[f"NAXIS{freq_axis}"]),
        n_pol=n_pol,
        cross_hands=cross_hands,
    )


def read_layout(path: Path) -> tuple[UvfitsLayout | None, str | None]:
    """The layout of ``path``, or ``(None, reason)`` when it cannot be subset safely."""
    try:
        with open(path, "rb") as raw:
            header = fits.Header.fromfile(raw)
        return _layout(header), None
    except (OSError, ValueError, KeyError) as exc:
        return None, str(exc)


def if_span(block: list[tuple[int, int, int]], channels_per_if: int) -> tuple[int, int]:
    """First and last IF (0-based) touched by the ``(fit_index, first, last)`` ranges of a shard."""
    first = min(first_channel for _, first_channel, _ in block)
    last = max(last_channel for _, _, last_channel in block)
    return (first - 1) // channels_per_if, (last - 1) // channels_per_if


def _table_bytes(hdu: Any, n_if: int, if_first: int, if_last: int) -> bytes:
    """The AIPS FQ table with its per-IF columns cut down to the kept IFs."""
    columns = []
    for column in hdu.columns:
        values = np.asarray(hdu.data[column.name])
        fmt = column.format
        if values.ndim == 2 and values.shape[1] == n_if:
            values = values[:, if_first : if_last + 1]
            fmt = f"{values.shape[1]}{str(column.format)[-1]}"
        columns.append(fits.Column(name=column.name, format=fmt, unit=column.unit, array=values))
    table = fits.BinTableHDU.from_columns(columns, name=hdu.header["EXTNAME"])
    for key, value in hdu.header.items():
        if key == "NO_IF":
            table.header[key] = if_last - if_first + 1
        elif key in {"EXTVER", "EXTLEVEL"}:
            table.header[key] = value
    buffer = io.BytesIO()
    table.writeto(buffer)  # astropy prepends an empty primary HDU of exactly one block
    return buffer.getvalue()[BLOCK:]


def write_subset(
    source: Path,
    destination: Path,
    if_first: int,
    if_last: int,
    *,
    cancel_event: threading.Event | None = None,
) -> SubsetInfo:
    """Write IFs ``if_first..if_last`` (0-based, inclusive) of ``source`` to ``destination``.

    Cross-hand polarisations are left out when present. The file is streamed in
    chunks; memory use does not grow with its size. Raises ``ValueError`` for a
    file whose structure is not the one this was verified on.
    """
    with open(source, "rb") as raw:
        header = fits.Header.fromfile(raw)
        data_offset = raw.tell()
    layout = _layout(header)
    if not 0 <= if_first <= if_last < layout.n_if:
        raise ValueError(f"IF range {if_first + 1}-{if_last + 1} is outside the {layout.n_if} IFs of the file")
    naxis = int(header["NAXIS"])
    pcount, gcount = int(header["PCOUNT"]), int(header["GCOUNT"])
    dims = tuple(int(header[f"NAXIS{axis}"]) for axis in range(naxis, 1, -1))  # numpy order: last FITS axis first
    if_axis, stokes_axis = _axis(header, "IF"), _axis(header, "STOKES")
    assert if_axis is not None  # _layout checked it

    index: list[slice] = [slice(None)] * len(dims)
    index[naxis - if_axis] = slice(if_first, if_last + 1)
    new_header = header.copy()
    new_header[f"NAXIS{if_axis}"] = if_last - if_first + 1
    n_pol = layout.n_pol
    if layout.cross_hands and stokes_axis is not None:
        index[naxis - stokes_axis] = slice(0, 2)
        new_header[f"NAXIS{stokes_axis}"] = n_pol = 2
    channel_offset = if_first * layout.channels_per_if
    new_header.add_history(
        f"{SUBSET_HISTORY} IF {if_first + 1}-{if_last + 1} OF {layout.n_if} CHANOFF {channel_offset}"
    )

    cells = int(np.prod(dims))
    floats_per_group = pcount + cells
    out_dims = [len(range(*piece.indices(size))) for piece, size in zip(index, dims, strict=True)]
    out_cells = int(np.prod(out_dims))
    groups_per_chunk = max(1, CHUNK_BYTES // (floats_per_group * 4))
    data = np.memmap(source, dtype=">f4", mode="r", offset=data_offset, shape=(gcount, floats_per_group))
    try:
        with open(destination, "wb") as out:
            out.write(new_header.tostring(sep="", endcard=True, padding=True).encode("ascii"))
            written = 0
            for start in range(0, gcount, groups_per_chunk):
                if cancel_event is not None and cancel_event.is_set():
                    raise SubsetCancelled
                chunk = data[start : start + groups_per_chunk]
                body = chunk[:, pcount:].reshape((chunk.shape[0], *dims))[(slice(None), *index)]
                # numpy returns native byte order from concatenate; FITS is big-endian.
                merged = np.concatenate(
                    [chunk[:, :pcount], body.reshape(chunk.shape[0], out_cells)], axis=1
                ).astype(">f4", copy=False)
                out.write(merged.tobytes())
                written += merged.nbytes
            if written % BLOCK:
                out.write(b"\0" * (BLOCK - written % BLOCK))
            with fits.open(source, memmap=True) as hdul, open(source, "rb") as raw:
                for hdu in hdul[1:]:
                    name = str(hdu.header.get("EXTNAME", "")).strip().upper()
                    if name in {"AIPS FQ", "FQ"}:
                        out.write(_table_bytes(hdu, layout.n_if, if_first, if_last))
                        continue
                    info = hdu.fileinfo()
                    raw.seek(info["hdrLoc"])
                    remaining = info["datLoc"] + info["datSpan"] - info["hdrLoc"]
                    while remaining > 0:
                        piece = raw.read(min(remaining, 8 << 20))
                        if not piece:
                            raise ValueError(f"{source.name} ends inside the {name or 'extension'} table")
                        out.write(piece)
                        remaining -= len(piece)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        del data
    return SubsetInfo(destination, if_first, if_last, n_pol, channel_offset, destination.stat().st_size)
