"""Shared fixtures: a fake DifMAP, a synthetic UV-FITS, and a project skeleton."""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from astropy.io import fits

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
LEGACY_DIR = REPO_ROOT / "legacy"

LEGACY_STAGE1 = LEGACY_DIR / "run_difmap_stage1_v1_1.py"
LEGACY_STAGE2 = LEGACY_DIR / "run_difmap_stage2_v1_4.py"
LEGACY_STAGE3 = LEGACY_DIR / "run_difmap_stage3_v1_3_38.py"


def load_legacy_module(name: str, path: Path) -> ModuleType:
    """Import a legacy script as a module without running its main()."""
    if str(LEGACY_DIR) not in sys.path:
        sys.path.insert(0, str(LEGACY_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def legacy_stage1() -> ModuleType:
    return load_legacy_module("legacy_stage1", LEGACY_STAGE1)


@pytest.fixture(scope="session")
def legacy_stage2() -> ModuleType:
    return load_legacy_module("legacy_stage2", LEGACY_STAGE2)


@pytest.fixture(scope="session")
def fake_difmap() -> Path:
    path = FIXTURES / "fake_difmap.py"
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def write_synthetic_uvfits(
    path: Path,
    *,
    n_if: int = 2,
    n_chan: int = 4,
    reference_frequency_hz: float = 15.0e9,
    channel_width_hz: float = 2.0e6,
    date_obs: str = "2022-03-15T13:28:04.0",
    seed: int = 0,
) -> None:
    """Write a small random-groups UV-FITS file with an AIPS FQ table."""
    rng = np.random.default_rng(seed)
    n_groups = 6
    data = rng.normal(size=(n_groups, 1, 1, n_if, n_chan, 1, 3)).astype(">f4")
    data[..., 2] = 1.0  # weights
    parnames = ["UU", "VV", "WW", "BASELINE", "DATE"]
    pardata = [
        rng.normal(size=n_groups).astype(">f4") * 1e-6,
        rng.normal(size=n_groups).astype(">f4") * 1e-6,
        rng.normal(size=n_groups).astype(">f4") * 1e-6,
        np.array([258, 259, 260, 261, 262, 263], dtype=">f4"),
        np.full(n_groups, 2459653.5, dtype=">f4"),
    ]
    group_data = fits.GroupData(data, parnames=parnames, pardata=pardata, bitpix=-32)
    primary = fits.GroupsHDU(group_data)
    header = primary.header
    header["OBJECT"] = "MG0414"
    header["TELESCOP"] = "EVLA"
    header["DATE-OBS"] = date_obs
    header["CTYPE2"] = "COMPLEX"
    header["CRVAL2"] = 1.0
    header["CDELT2"] = 1.0
    header["CRPIX2"] = 1.0
    header["CTYPE3"] = "STOKES"
    header["CRVAL3"] = 1.0
    header["CDELT3"] = 1.0
    header["CRPIX3"] = 1.0
    header["CTYPE4"] = "FREQ"
    header["CRVAL4"] = reference_frequency_hz
    header["CDELT4"] = channel_width_hz
    header["CRPIX4"] = 1.0
    header["CTYPE5"] = "IF"
    header["CRVAL5"] = 1.0
    header["CDELT5"] = 1.0
    header["CRPIX5"] = 1.0
    header["CTYPE6"] = "RA"
    header["CRVAL6"] = 64.24
    header["CTYPE7"] = "DEC"
    header["CRVAL7"] = 5.57

    if_offsets = np.array([[i * n_chan * channel_width_hz for i in range(n_if)]], dtype=">f8")
    columns = fits.ColDefs(
        [
            fits.Column(name="FRQSEL", format="1J", array=np.array([1], dtype=">i4")),
            fits.Column(name="IF FREQ", format=f"{n_if}D", unit="HZ", array=if_offsets),
            fits.Column(
                name="CH WIDTH",
                format=f"{n_if}E",
                unit="HZ",
                array=np.full((1, n_if), channel_width_hz, dtype=">f4"),
            ),
            fits.Column(
                name="TOTAL BANDWIDTH",
                format=f"{n_if}E",
                unit="HZ",
                array=np.full((1, n_if), channel_width_hz * n_chan, dtype=">f4"),
            ),
            fits.Column(name="SIDEBAND", format=f"{n_if}J", array=np.ones((1, n_if), dtype=">i4")),
        ]
    )
    fq = fits.BinTableHDU.from_columns(columns, name="AIPS FQ")
    fq.header["NO_IF"] = n_if
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList([primary, fq]).writeto(path, overwrite=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with a master model and two epochs (A, B) of synthetic UV data."""
    root = tmp_path / "MG0414"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "MG0414.gmod", inputs / "MG0414.gmod")
    write_synthetic_uvfits(inputs / "MG0414.A.uvfits", seed=1, date_obs="2022-03-15T13:28:04.0")
    write_synthetic_uvfits(inputs / "MG0414.B.uvfits", seed=2, date_obs="2022-05-02T09:10:11.0")
    return root


@pytest.fixture
def fake_difmap_env(fake_difmap: Path) -> dict[str, str]:
    """Environment where ``difmap`` on PATH resolves to the fake."""
    bin_dir = fake_difmap.parent / "_bin"
    bin_dir.mkdir(exist_ok=True)
    link = bin_dir / "difmap"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(fake_difmap)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["MPLBACKEND"] = "Agg"
    return env
