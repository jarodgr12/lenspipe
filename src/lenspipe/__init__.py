"""lenspipe: DifMAP spectral pipeline for multi-epoch lensed-image spectra."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("lenspipe")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
