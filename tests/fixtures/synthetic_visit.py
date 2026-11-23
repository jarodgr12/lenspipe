"""A synthetic VLA-like visit that a real DifMAP reads: several IFs, RR/LL/RL/LR, flags.

Four point sources in a lens-like arrangement with different spectral slopes, so
per-channel fits give distinct numbers. Used by the opt-in real-DifMAP test of
``stage2.split_inputs`` (``LENSPIPE_TEST_DIFMAP``); the fake DifMAP does not need it.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from astropy.io import fits

C = 299792458.0
MAS = np.pi / 180 / 3600 / 1000

# (flux Jy at 15 GHz, east offset mas, north offset mas, spectral index)
SOURCES = [(0.40, 0.0, 0.0, -0.6), (0.35, 380.0, 120.0, -0.5), (0.15, -590.0, 1930.0, -0.7), (0.06, 1320.0, 1640.0, -0.4)]


def write_visit(
    path: Path, *, n_ant: int = 10, n_int: int = 30, n_if: int = 6, n_chan: int = 8, n_pol: int = 4,
    seed: int = 1, flag_fraction: float = 0.0,
) -> None:
    """Write the visit; ``flag_fraction`` of the visibilities are flagged and a fifth as many deleted."""
    rng = np.random.default_rng(seed)
    ref_freq = 12.0e9
    chan_width = 2.0e6
    east = rng.uniform(-15e3, 15e3, n_ant)
    north = rng.uniform(-15e3, 15e3, n_ant)
    a1, a2 = np.triu_indices(n_ant, 1)
    n_base = a1.size
    dec = np.deg2rad(5.57)
    hour_angles = np.linspace(-0.4, 0.4, n_int)
    freqs = ref_freq + (np.arange(n_if)[:, None] * n_chan + np.arange(n_chan)[None, :]) * chan_width  # (if, chan)

    n_groups = n_int * n_base
    data = np.empty((n_groups, 1, 1, n_if, n_chan, n_pol, 3), dtype=">f4")
    uu = np.empty(n_groups, dtype=">f4")
    vv = np.empty(n_groups, dtype=">f4")
    ww = np.empty(n_groups, dtype=">f4")
    jd_frac = np.empty(n_groups, dtype=">f4")
    noise = 0.02
    for t, h in enumerate(hour_angles):
        be, bn = east[a2] - east[a1], north[a2] - north[a1]
        u = (be * np.cos(h) - bn * np.sin(dec) * np.sin(h)) / C  # seconds
        v = (be * np.sin(dec) * np.sin(h) + bn * np.cos(dec)) / C
        w = (be * np.cos(dec) * np.sin(h) - bn * np.sin(dec)) / C
        sl = slice(t * n_base, (t + 1) * n_base)
        uu[sl], vv[sl], ww[sl] = u, v, w
        jd_frac[sl] = 0.5 + t * 10.0 / 86400.0
        vis = np.zeros((n_base, n_if, n_chan), dtype=np.complex128)
        for flux, x, y, alpha in SOURCES:
            phase = 2 * np.pi * (u[:, None, None] * x * MAS + v[:, None, None] * y * MAS) * freqs[None]
            vis += flux * (freqs[None] / 15.0e9) ** alpha * np.exp(1j * phase)
        block = data[sl, 0, 0]
        for p in range(n_pol):
            signal = vis if p < 2 else 0.0
            block[..., p, 0] = np.real(signal) + rng.normal(0, noise, (n_base, n_if, n_chan))
            block[..., p, 1] = np.imag(signal) + rng.normal(0, noise, (n_base, n_if, n_chan))
            weight = np.full((n_base, n_if, n_chan), 1.0 / noise**2)
            draw = rng.random((n_base, n_if, n_chan))
            weight[draw < flag_fraction] *= -1.0  # flagged
            weight[draw > 1.0 - flag_fraction / 5] = 0.0  # deleted
            block[..., p, 2] = weight
    baseline = np.tile((256 * (a1 + 1) + (a2 + 1)).astype(">f4"), n_int)
    jd0 = 2459653.5
    parnames = ["UU---SIN", "VV---SIN", "WW---SIN", "BASELINE", "DATE", "DATE", "INTTIM"]
    pardata = [uu, vv, ww, baseline, np.full(n_groups, 0.0, dtype=">f4"), jd_frac, np.full(n_groups, 10.0, dtype=">f4")]
    primary = fits.GroupsHDU(fits.GroupData(data, parnames=parnames, pardata=pardata, bitpix=-32))
    h = primary.header
    for i in range(1, 8):
        h[f"PSCAL{i}"], h[f"PZERO{i}"] = 1.0, (jd0 if i == 5 else 0.0)
    h["OBJECT"], h["TELESCOP"], h["INSTRUME"], h["OBSERVER"] = "SYNTH", "VLA", "VLA", "TEST"
    h["DATE-OBS"] = "2022-03-15"
    h["BUNIT"], h["EPOCH"] = "JY", 2000.0
    h["OBSRA"], h["OBSDEC"] = 64.24, 5.57
    axes = [("COMPLEX", 1.0, 1.0), ("STOKES", -1.0, -1.0), ("FREQ", ref_freq, chan_width), ("IF", 1.0, 1.0), ("RA", 64.24, 1.0), ("DEC", 5.57, 1.0)]
    for i, (ctype, crval, cdelt) in enumerate(axes, start=2):
        h[f"CTYPE{i}"], h[f"CRVAL{i}"], h[f"CDELT{i}"], h[f"CRPIX{i}"], h[f"CROTA{i}"] = ctype, crval, cdelt, 1.0, 0.0

    fq = fits.BinTableHDU.from_columns(
        [
            fits.Column(name="FRQSEL", format="1J", array=np.array([1], dtype=">i4")),
            fits.Column(name="IF FREQ", format=f"{n_if}D", unit="HZ", array=(np.arange(n_if) * n_chan * chan_width)[None, :]),
            fits.Column(name="CH WIDTH", format=f"{n_if}E", unit="HZ", array=np.full((1, n_if), chan_width, dtype=">f4")),
            fits.Column(name="TOTAL BANDWIDTH", format=f"{n_if}E", unit="HZ", array=np.full((1, n_if), chan_width * n_chan, dtype=">f4")),
            fits.Column(name="SIDEBAND", format=f"{n_if}J", array=np.ones((1, n_if), dtype=">i4")),
        ],
        name="AIPS FQ",
    )
    fq.header["NO_IF"] = n_if
    fq.header["EXTVER"] = 1

    xyz = np.stack([north * 0 + 1.0, east, north], axis=1)
    an = fits.BinTableHDU.from_columns(
        [
            fits.Column(name="ANNAME", format="8A", array=np.array([f"ANT{i + 1:02d}" for i in range(n_ant)])),
            fits.Column(name="STABXYZ", format="3D", unit="METERS", array=xyz),
            fits.Column(name="ORBPARM", format="0D", array=np.zeros((n_ant, 0))),
            fits.Column(name="NOSTA", format="1J", array=np.arange(1, n_ant + 1, dtype=">i4")),
            fits.Column(name="MNTSTA", format="1J", array=np.zeros(n_ant, dtype=">i4")),
            fits.Column(name="STAXOF", format="1E", unit="METERS", array=np.zeros(n_ant, dtype=">f4")),
            fits.Column(name="POLTYA", format="1A", array=np.array(["R"] * n_ant)),
            fits.Column(name="POLAA", format="1E", unit="DEGREES", array=np.zeros(n_ant, dtype=">f4")),
            fits.Column(name="POLCALA", format="0E", array=np.zeros((n_ant, 0), dtype=">f4")),
            fits.Column(name="POLTYB", format="1A", array=np.array(["L"] * n_ant)),
            fits.Column(name="POLAB", format="1E", unit="DEGREES", array=np.zeros(n_ant, dtype=">f4")),
            fits.Column(name="POLCALB", format="0E", array=np.zeros((n_ant, 0), dtype=">f4")),
        ],
        name="AIPS AN",
    )
    ah = an.header
    ah["EXTVER"] = 1
    ah["ARRAYX"], ah["ARRAYY"], ah["ARRAYZ"] = -1601185.4, -5041977.5, 3554875.9
    ah["GSTIA0"], ah["DEGPDY"], ah["FREQ"] = 172.0, 360.9856, ref_freq
    ah["RDATE"], ah["POLARX"], ah["POLARY"] = "2022-03-15", 0.0, 0.0
    ah["UT1UTC"], ah["DATUTC"], ah["IATUTC"] = 0.0, 0.0, 37.0
    ah["TIMSYS"], ah["ARRNAM"], ah["NUMORB"], ah["NOPCAL"], ah["POLTYPE"] = "UTC", "VLA", 0, 0, " "
    fits.HDUList([primary, an, fq]).writeto(path, overwrite=True)


# The Stage 2 flux-only model of the same four components: flux free, position fixed.
MODEL_TEXT = "! flux-only test model\n" + "".join(
    f"{flux:.3f}v {math.hypot(x, y):.4f} {math.degrees(math.atan2(x, y)):.4f}\n" for flux, x, y, _ in SOURCES
)
