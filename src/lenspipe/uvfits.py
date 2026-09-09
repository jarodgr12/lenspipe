"""Read the little we need from UV-FITS headers: frequencies and observation date."""

from __future__ import annotations

import hashlib
from pathlib import Path

from astropy.io import fits
from astropy.time import Time

__all__ = [
    "file_sha256",
    "get_observation_mjd",
    "get_uvfits_frequencies",
    "header_axis_number",
    "representative_channel_width",
]


def header_axis_number(header: fits.Header, ctype_fragment: str) -> int | None:
    """Find a FITS axis whose CTYPE contains the requested fragment."""
    naxis = int(header.get("NAXIS", 0))
    for axis in range(1, naxis + 1):
        ctype = str(header.get(f"CTYPE{axis}", "")).upper()
        if ctype_fragment.upper() in ctype:
            return axis
    return None


def get_uvfits_frequencies(path: Path) -> list[float]:
    """Return concatenated channel frequencies in Hz.

    Supports the common arrangement where the random-groups primary header
    describes the intra-IF frequency axis and an AIPS FQ table supplies IF
    frequency offsets. The first frequency-setup row is used.
    """
    with fits.open(path, memmap=True) as hdul:
        header = hdul[0].header
        freq_axis = header_axis_number(header, "FREQ")
        if freq_axis is None:
            raise ValueError("Could not locate a FREQ axis in the UV-FITS header.")

        nchan = int(header.get(f"NAXIS{freq_axis}", 1))
        crval = float(header.get(f"CRVAL{freq_axis}", 0.0))
        crpix = float(header.get(f"CRPIX{freq_axis}", 1.0))
        cdelt = float(header.get(f"CDELT{freq_axis}", 0.0))
        channel_offsets = [((index + 1) - crpix) * cdelt for index in range(nchan)]

        fq_hdu = None
        for hdu in hdul[1:]:
            extname = str(hdu.header.get("EXTNAME", "")).strip().upper()
            if extname in {"AIPS FQ", "FQ"}:
                fq_hdu = hdu
                break

        if fq_hdu is None or fq_hdu.data is None:
            return [crval + offset for offset in channel_offsets]

        names = {name.upper(): name for name in fq_hdu.columns.names}
        if_name = names.get("IF FREQ")
        if if_name is None:
            raise ValueError("The AIPS FQ table has no 'IF FREQ' column.")

        raw_if_freq = fq_hdu.data[if_name][0]
        try:
            if_offsets = [float(value) for value in raw_if_freq]
        except TypeError:
            if_offsets = [float(raw_if_freq)]

        return [
            crval + if_offset + channel_offset
            for if_offset in if_offsets
            for channel_offset in channel_offsets
        ]


def get_observation_mjd(path: Path) -> float | None:
    """Read MJD-OBS or convert DATE-OBS from the UV-FITS primary header."""
    with fits.open(path, memmap=True) as hdul:
        header = hdul[0].header
        if header.get("MJD-OBS") is not None:
            return float(header["MJD-OBS"])
        date_obs = header.get("DATE-OBS")
        if date_obs:
            try:
                return float(Time(str(date_obs), format="isot", scale="utc").mjd)
            except ValueError:
                return float(Time(str(date_obs), scale="utc").mjd)
    return None


def representative_channel_width(frequencies: list[float]) -> float | None:
    """Median spacing between adjacent distinct concatenated frequencies."""
    spacings = [abs(b - a) for a, b in zip(frequencies, frequencies[1:], strict=False) if b != a]
    if not spacings:
        return None
    spacings.sort()
    return spacings[len(spacings) // 2]


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
