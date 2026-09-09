"""Driving DifMAP: command scripts, the subprocess runner, and log parsing."""

from __future__ import annotations

from lenspipe.difmap.logparse import (
    STAGE1_RMS_MARKER,
    STAGE2_RMS_MARKER,
    extract_last_numeric,
    modelfit_flux_measurements_by_fit,
    tagged_scalar,
    tagged_values,
)
from lenspipe.difmap.runner import DifmapResult, difmap_version, run_difmap

__all__ = [
    "STAGE1_RMS_MARKER",
    "STAGE2_RMS_MARKER",
    "DifmapResult",
    "difmap_version",
    "extract_last_numeric",
    "modelfit_flux_measurements_by_fit",
    "run_difmap",
    "tagged_scalar",
    "tagged_values",
]
