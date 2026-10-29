"""Stage 1: self-calibration, model fitting, and imaging of one epoch."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lenspipe import __version__
from lenspipe.channels import IfSelection
from lenspipe.config import LenspipeConfig
from lenspipe.difmap import (
    STAGE1_RMS_MARKER,
    difmap_version,
    extract_last_numeric,
    run_difmap,
    tagged_scalar,
)
from lenspipe.difmap.runner import DifmapNotFound
from lenspipe.difmap.scripts import imaging_commands, stage1_commands
from lenspipe.models import (
    ModelFormatError,
    ModelHierarchy,
    label_fitted_model,
    parse_master_model,
    write_fixed_spectral_index_model,
)
from lenspipe.progress import Reporter
from lenspipe.project import Layout, migrate_flat_layout, parse_epoch_filename, utc_now, write_json
from lenspipe.provenance import fingerprint

__all__ = [
    "STAGE_NUMBER",
    "Stage1Paths",
    "Stage1Result",
    "common_cell_mas",
    "discover_inputs",
    "image_geometry",
    "make_paths",
    "reimage_epoch",
    "run_epoch",
    "run_stage1",
    "same_grid",
]

STAGE_NUMBER = 1
SCHEMA_VERSION = "1.1"


@dataclass(frozen=True)
class Stage1Paths:
    project_root: Path
    uvfits: Path
    source: str
    epoch: str
    starting_model: Path
    output_directory: Path
    calibrated_uvfits: Path
    final_model: Path
    clean_image: Path
    residual_image: Path
    log_file: Path
    model_metadata: Path
    stage_metadata: Path
    manifest: Path
    calibration_mode: str
    native_clean_image: Path
    native_residual_image: Path
    reimage_log_file: Path
    fixed_index_model: Path
    per_if_models: tuple[Path, ...]

    @property
    def prefix(self) -> str:
        return f"{self.source}.{self.epoch}"

    def calibration_products(self) -> tuple[Path, ...]:
        """Written by the calibration pass (``run_epoch``)."""
        extra = (self.fixed_index_model, *self.per_if_models) if self.calibration_mode == "per-if" else ()
        return (
            self.calibrated_uvfits,
            self.final_model,
            self.native_clean_image,
            self.native_residual_image,
            self.log_file,
            self.model_metadata,
            self.stage_metadata,
            self.manifest,
            *extra,
        )

    def image_products(self) -> tuple[Path, ...]:
        """Written by the common-grid imaging pass (``reimage_epoch``)."""
        return (self.clean_image, self.residual_image, self.reimage_log_file)

    def products(self) -> tuple[Path, ...]:
        return (*self.calibration_products(), *self.image_products())


@dataclass
class Stage1Result:
    paths: Stage1Paths
    ok: bool
    status: str  # completed | skipped | dry_run | failed
    message: str = ""
    rms_jy_per_beam: float | None = None
    commands: str | None = None
    geometry: dict[str, Any] | None = None  # WCS of the map this pass wrote


def make_paths(
    project_root: Path, uvfits: Path, calibration_mode: str = "full-band", if_count: int = 0
) -> Stage1Paths:
    """Paths of one epoch's Stage 1 products under ``stage1/<calibration_mode>/``.

    ``if_count`` sizes the per-IF model list; it is only used in per-IF mode.
    """
    source, epoch = parse_epoch_filename(uvfits, ".uvfits")
    prefix = f"{source}.{epoch}"
    layout = Layout.at(project_root)
    out = (layout.stage_dir(1, calibration_mode) / prefix).resolve()
    per_if_models = tuple(out / f"{prefix}.if{n:03d}.gmod" for n in range(1, if_count + 1))
    # Inputs are often symlinks into an archive; keep the project-side path so
    # products stay described relative to the project, and DifMAP follows the link.
    return Stage1Paths(
        project_root=layout.root,
        uvfits=uvfits.absolute(),
        source=source,
        epoch=epoch,
        starting_model=(layout.inputs / f"{source}.gmod").absolute(),
        output_directory=out,
        calibrated_uvfits=out / f"{prefix}.cal.uvf",
        final_model=out / f"{prefix}.gmod",
        clean_image=out / f"{prefix}.cln.fits",
        residual_image=out / f"{prefix}.resid.fits",
        log_file=out / f"{prefix}.stage1.difmap.log",
        model_metadata=out / f"{prefix}.model.json",
        stage_metadata=out / f"{prefix}.stage1.metadata.json",
        manifest=out / f"{prefix}.stage1.manifest.json",
        calibration_mode=calibration_mode,
        native_clean_image=out / f"{prefix}.native.cln.fits",
        native_residual_image=out / f"{prefix}.native.resid.fits",
        reimage_log_file=out / f"{prefix}.stage1.reimage.difmap.log",
        fixed_index_model=out / f"{prefix}.fixed_index.gmod",
        per_if_models=per_if_models,
    )


def _paths_for_config(project_root: Path, uvfits: Path, config: LenspipeConfig) -> Stage1Paths:
    stage1 = config.stage1
    if_count = stage1.per_if.if_count if stage1.calibration_mode == "per-if" else 0
    return make_paths(project_root, uvfits, stage1.calibration_mode, if_count)


def discover_inputs(project_root: Path, pattern: str, epochs: set[str] | None) -> list[Path]:
    layout = Layout.at(project_root)
    files: list[Path] = []
    for path in sorted(layout.inputs.glob(pattern)):
        if not path.is_file():
            continue
        try:
            _, epoch = parse_epoch_filename(path, ".uvfits")
        except ValueError:
            continue
        if epochs and epoch not in epochs:
            continue
        files.append(path)
    return files


def _project_relative(path: Path, root: Path) -> str:
    """Path relative to the project when it lives inside it, else the absolute path."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _write_metadata(
    paths: Stage1Paths,
    hierarchy: ModelHierarchy,
    rms: float | None,
    selected_ranges: list[IfSelection],
    config: LenspipeConfig,
    commands: str,
    difmap_info: dict[str, str | None],
    duration_s: float,
    beam: dict[str, float] | None = None,
    native_geometry: dict[str, Any] | None = None,
) -> None:
    created = utc_now()
    stage1 = config.stage1
    per_if: dict[str, Any] | None = None
    if paths.calibration_mode == "per-if":
        layout = stage1.per_if
        per_if = {
            "if_count": layout.if_count,
            "channels_per_if": layout.channels_per_if,
            "edge_channels": layout.edge_channels,
            "total_channels": layout.total_channels,
            "exclude_channels_spec": layout.exclude_channels,
            "skipped_ifs": layout.selections()[1],
            "selected_ranges_one_based": [
                {
                    "if_number": s.if_number, "first_channel": s.first, "last_channel": s.last,
                    "channels": [list(run) for run in s.segments], "n_channels": len(s.channels),
                }
                for s in layout.selections()[0]
            ],
            "amplitude_calibration": "gscale",
            "spectral_index_fixed_at": 0,
            "fixed_index_model": paths.fixed_index_model.name,
            "per_if_models": [path.name for path in paths.per_if_models],
            "final_fit": "full band, master model reloaded, no further self-cal",
        }
    provenance = {
        "input_uvfits": fingerprint(paths.uvfits),
        "master_model": fingerprint(paths.starting_model),
    }
    write_json(
        paths.model_metadata,
        {
            "schema_version": "1.0",
            "stage": STAGE_NUMBER,
            "stage1_version": __version__,
            "source": paths.source,
            "epoch": paths.epoch,
            "master_model": paths.starting_model.name,
            "fitted_model": paths.final_model.name,
            "n_groups": len(hierarchy.groups),
            "n_components": len(hierarchy.components),
            "groups": hierarchy.groups,
            "components": hierarchy.to_records(),
            "created_utc": created,
        },
    )
    write_json(
        paths.stage_metadata,
        {
            "schema_version": SCHEMA_VERSION,
            "pipeline": "DifMAP Spectral Pipeline",
            "stage": STAGE_NUMBER,
            "version": __version__,
            "lenspipe_version": __version__,
            "source": paths.source,
            "epoch": paths.epoch,
            "project_root": str(paths.project_root),
            "input_uvfits": _project_relative(paths.uvfits, paths.project_root),
            "input_uvfits_resolved": str(paths.uvfits.resolve()),
            "input_sha256": provenance["input_uvfits"]["sha256"],
            "master_model": _project_relative(paths.starting_model, paths.project_root),
            "master_model_resolved": str(paths.starting_model.resolve()),
            "provenance": provenance,
            "calibration_mode": paths.calibration_mode,
            "per_if": per_if,
            "native_residual_rms_jy_per_beam": rms,
            "final_residual_rms_jy_per_beam": None,  # set by the common-grid imaging pass
            "native_clean_beam_deg": beam,  # BMAJ, BMIN, BPA of the native restored map
            "clean_beam_deg": None,  # set by the common-grid imaging pass
            "native_image_geometry": native_geometry,
            "common_image_geometry": None,  # set by the common-grid imaging pass
            "difmap_map_size": stage1.map_pixels,
            "fits_image_size": native_geometry["naxis1"] if native_geometry else None,
            "difmap": difmap_info,
            "command_script_sha256": hashlib.sha256(commands.encode("utf-8")).hexdigest(),
            "duration_s": round(duration_s, 3),
            "final_if_selfcal": {
                "enabled": stage1.final_if_selfcal.enabled,
                "n_ifs": stage1.final_if_selfcal.if_count if stage1.final_if_selfcal.enabled else 0,
                "n_ifs_calibrated": len(selected_ranges),
                "skipped_ifs": (
                    stage1.final_if_selfcal.selections()[1] if stage1.final_if_selfcal.enabled else []
                ),
                "channels_per_if": stage1.final_if_selfcal.channels_per_if,
                "edge_channels_excluded_per_side": stage1.final_if_selfcal.edge_channels,
                "retained_channels_per_if": (
                    stage1.final_if_selfcal.channels_per_if
                    - 2 * stage1.final_if_selfcal.edge_channels
                ),
                "amplitude_phase_interval_minutes": 3600.0,
                "phase_only_interval_minutes": 0.5,
                "modelfit_within_if_loop": False,
                "exclude_channels_spec": stage1.final_if_selfcal.exclude_channels,
                "selected_ranges_one_based": [
                    {
                        "if_number": s.if_number,
                        "first_channel": s.first,
                        "last_channel": s.last,
                        "channels": [list(run) for run in s.segments],  # absolute, inclusive runs
                        "n_channels": len(s.channels),
                    }
                    for s in selected_ranges
                ],
            },
            "config": stage1.model_dump(mode="json"),
            "created_utc": created,
        },
    )
    write_json(
        paths.manifest,
        {
            "schema_version": "1.0",
            "stage": STAGE_NUMBER,
            "source": paths.source,
            "epoch": paths.epoch,
            "metadata": paths.stage_metadata.name,
            "model_metadata": paths.model_metadata.name,
            "products": [p.name for p in paths.products() if p != paths.manifest and p.exists()],
        },
    )


BEAM_KEYWORDS = ("BMAJ", "BMIN", "BPA")


def copy_beam_keywords(clean_image: Path, residual_image: Path) -> dict[str, float] | None:
    """Stamp the restored map's beam onto the residual map, which DifMAP writes without one.

    ``wdmap`` records no BMAJ/BMIN/BPA because a residual map has no restoring
    beam of its own, but downstream tools (and people) want the same beam on
    both images. Returns the beam in degrees, or None if the clean map has none.
    """
    from astropy.io import fits

    with fits.open(clean_image, memmap=False) as hdul:
        header = hdul[0].header
        if not all(key in header for key in BEAM_KEYWORDS):
            return None
        beam = {key: float(header[key]) for key in BEAM_KEYWORDS}
        comments = {key: header.comments[key] for key in BEAM_KEYWORDS}
    with fits.open(residual_image, mode="update", memmap=False) as hdul:
        target = hdul[0].header
        for key in BEAM_KEYWORDS:
            target[key] = (beam[key], comments[key] or f"{key} copied from the restored map")
        hdul.flush()
    return {"bmaj": beam["BMAJ"], "bmin": beam["BMIN"], "bpa": beam["BPA"]}


def run_epoch(
    paths: Stage1Paths,
    config: LenspipeConfig,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    reporter: Reporter | None = None,
    cancel_event: threading.Event | None = None,
) -> Stage1Result:
    reporter = reporter or Reporter()
    label = f"stage1 {paths.prefix}"

    if not paths.starting_model.is_file():
        return Stage1Result(paths, False, "failed", f"master model not found: {paths.starting_model}")
    try:
        hierarchy = parse_master_model(paths.starting_model)
    except (OSError, ModelFormatError) as exc:
        return Stage1Result(paths, False, "failed", f"invalid master model: {exc}")

    existing = [path for path in paths.products() if path.exists()]
    if existing and not overwrite:
        return Stage1Result(
            paths, False, "skipped", "Stage 1 products already exist; use --overwrite to replace."
        )

    per_if = paths.calibration_mode == "per-if"
    try:
        commands, selected_ranges = stage1_commands(
            uvfits=paths.uvfits,
            starting_model=paths.starting_model,
            residual_image=paths.native_residual_image,
            clean_image=paths.native_clean_image,
            calibrated_uvfits=paths.calibrated_uvfits,
            final_model=paths.final_model,
            config=config.stage1,
            fixed_index_model=paths.fixed_index_model if per_if else None,
            per_if_models=list(paths.per_if_models) if per_if else None,
        )
    except ValueError as exc:
        return Stage1Result(paths, False, "failed", f"invalid IF self-calibration setup: {exc}")

    reporter.log(
        f"[{label}] {len(hierarchy.groups)} group(s), {len(hierarchy.components)} component(s): "
        + "; ".join(f"{g}: {', '.join(c)}" for g, c in hierarchy.groups.items())
    )
    if dry_run:
        return Stage1Result(paths, True, "dry_run", "commands generated", commands=commands)

    paths.output_directory.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for path in paths.products():
            if path.exists():
                path.unlink()
    if per_if:
        try:
            pinned = write_fixed_spectral_index_model(paths.starting_model, paths.fixed_index_model)
        except (OSError, ModelFormatError) as exc:
            return Stage1Result(paths, False, "failed", f"could not write the fixed-index model: {exc}")
        reporter.log(
            f"[{label}] per-IF mode: {config.stage1.per_if.if_count} IFs x "
            f"{config.stage1.per_if.channels_per_if} channels, edge {config.stage1.per_if.edge_channels}; "
            f"spectral index fixed at 0 for {pinned} component(s)"
        )

    steps_total = len(config.stage1.selfcal) * (config.stage1.per_if.if_count if per_if else 1)
    done = {"n": 0}

    def on_line(line: str) -> None:
        # DifMAP echoes nothing structured for selfcal; count modelfit summaries loosely.
        if line.lstrip().startswith("Iteration") and "Chi-squared" in line:
            return
        if "Applying" in line and "selfcal" in line.lower():
            done["n"] = min(steps_total, done["n"] + 1)
            reporter.progress(label, done["n"], steps_total, "self-calibration")

    reporter.progress(label, 0, steps_total, "starting DifMAP")
    try:
        result = run_difmap(
            config.project.difmap.executable,
            commands,
            paths.log_file,
            on_line=on_line,
            cancel_event=cancel_event,
            stream=config.project.difmap.stream,
            nice=config.run.nice,
            cwd=paths.output_directory,  # DifMAP writes its own difmap.log_N in its cwd
        )
    except (DifmapNotFound, OSError) as exc:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"could not run DifMAP: {exc}")

    if result.cancelled:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", "cancelled")
    if result.returncode != 0:
        reporter.clear(label)
        return Stage1Result(
            paths, False, "failed", f"DifMAP returned status {result.returncode}; see {paths.log_file}"
        )

    calibrated_ifs = {s.if_number for s in config.stage1.per_if.selections()[0]} if per_if else set()
    required = (
        paths.calibrated_uvfits, paths.final_model, paths.native_clean_image, paths.native_residual_image,
        *(m for n, m in enumerate(paths.per_if_models, start=1) if n in calibrated_ifs),
    )
    missing = [path for path in required if not path.exists()]
    if missing:
        reporter.clear(label)
        return Stage1Result(
            paths,
            False,
            "failed",
            "DifMAP exited normally but outputs are missing: " + ", ".join(str(m) for m in missing),
        )

    rms = tagged_scalar(result.log_text, STAGE1_RMS_MARKER)
    if rms is None:
        rms = extract_last_numeric(result.log_text)
    for own_log in paths.output_directory.glob("difmap.log*"):  # duplicate of our captured log
        own_log.unlink(missing_ok=True)

    beam = _stamp_beam(paths.native_clean_image, paths.native_residual_image, reporter, label)

    try:
        label_fitted_model(paths.final_model, hierarchy)
        for model in paths.per_if_models if per_if else ():
            if model.is_file():
                label_fitted_model(model, hierarchy)
        native_geometry = image_geometry(paths.native_clean_image)
        _write_metadata(
            paths,
            hierarchy,
            rms,
            selected_ranges,
            config,
            commands,
            difmap_version(config.project.difmap.executable),
            result.duration_s,
            beam=beam,
            native_geometry=native_geometry,
        )
    except (OSError, ModelFormatError, ValueError) as exc:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"post-processing error: {exc}")

    reporter.progress(label, steps_total, steps_total, "completed")
    return Stage1Result(
        paths, True, "completed", "completed", rms_jy_per_beam=rms, commands=commands, geometry=native_geometry
    )


def _stamp_beam(clean: Path, residual: Path, reporter: Reporter, label: str) -> dict[str, float] | None:
    try:
        beam = copy_beam_keywords(clean, residual)
    except (OSError, ValueError) as exc:
        reporter.warn(f"[{label}] could not copy the clean beam onto {residual.name}: {exc}")
        return None
    if beam is None:
        reporter.warn(f"[{label}] {clean.name} carries no BMAJ/BMIN/BPA; residual map left as written")
    return beam


# ----------------------------------------------------------------------------
# Common-grid imaging (second pass)

GRID_KEYS = ("cdelt1_deg", "cdelt2_deg", "crpix1", "crpix2", "crval1_deg", "crval2_deg")


def image_geometry(path: Path) -> dict[str, Any]:
    """Pixel grid, WCS and beam of a DifMAP FITS map, as recorded in the metadata."""
    from astropy.io import fits

    header = fits.getheader(path)
    for key in ("NAXIS1", "NAXIS2", "CRPIX1", "CRPIX2", "CRVAL1", "CRVAL2", "CTYPE1", "CTYPE2"):
        if key not in header:
            raise ValueError(f"{path}: missing WCS keyword {key}")
    for key in ("CD1_2", "CD2_1", "PC1_2", "PC2_1", "CROTA2"):
        if abs(float(header.get(key, 0.0))) > 1e-14:
            raise ValueError(f"{path}: rotated or skewed WCS ({key}) is not supported for the common grid")
    dx = header.get("CD1_1", header.get("CDELT1"))
    dy = header.get("CD2_2", header.get("CDELT2"))
    if not dx or not dy:
        raise ValueError(f"{path}: no pixel scale (CDELT/CD) in header")
    return {
        "naxis1": int(header["NAXIS1"]),
        "naxis2": int(header["NAXIS2"]),
        "cdelt1_deg": float(dx),
        "cdelt2_deg": float(dy),
        "cell_mas": min(abs(float(dx)), abs(float(dy))) * 3.6e6,
        "crpix1": float(header["CRPIX1"]),
        "crpix2": float(header["CRPIX2"]),
        "crval1_deg": float(header["CRVAL1"]),
        "crval2_deg": float(header["CRVAL2"]),
        "ctype1": str(header["CTYPE1"]),
        "ctype2": str(header["CTYPE2"]),
        "bmaj_deg": float(header["BMAJ"]) if "BMAJ" in header else None,
        "bmin_deg": float(header["BMIN"]) if "BMIN" in header else None,
        "bpa_deg": float(header["BPA"]) if "BPA" in header else None,
    }


def same_grid(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same pixel grid: size, projection, scale and reference pixel/coordinate (beams may differ)."""
    if any(a[key] != b[key] for key in ("naxis1", "naxis2", "ctype1", "ctype2")):
        return False
    return all(math.isclose(a[key], b[key], rel_tol=1e-9, abs_tol=1e-11) for key in GRID_KEYS)


def common_cell_mas(geometries: Iterable[dict[str, Any]], requested: float | None = None) -> float:
    """The imaging cell shared by all epochs: ``stage1.cell_mas`` if set, else the finest native cell.

    Raises ValueError when the native maps disagree in projection or axis sense,
    since those cannot be put on one grid by choosing a cell.
    """
    items = list(geometries)
    if requested is not None:
        return float(requested)
    if not items:
        raise ValueError("no native maps to derive a common cell from")
    first = items[0]
    for g in items[1:]:
        if (g["ctype1"], g["ctype2"]) != (first["ctype1"], first["ctype2"]):
            raise ValueError("native maps use different projections")
        if g["cdelt1_deg"] * first["cdelt1_deg"] <= 0 or g["cdelt2_deg"] * first["cdelt2_deg"] <= 0:
            raise ValueError("native maps have opposite axis directions")
    return min(g["cell_mas"] for g in items)


def reimage_epoch(
    paths: Stage1Paths,
    config: LenspipeConfig,
    cell_mas: float,
    *,
    reporter: Reporter | None = None,
    cancel_event: threading.Event | None = None,
    dry_run: bool = False,
) -> Stage1Result:
    """Image one calibrated epoch on the common grid and record the result in its metadata."""
    reporter = reporter or Reporter()
    label = f"stage1 {paths.prefix} reimage"
    commands = imaging_commands(
        calibrated_uvfits=paths.calibrated_uvfits,
        model=paths.final_model,
        residual_image=paths.residual_image,
        clean_image=paths.clean_image,
        cell_mas=cell_mas,
        config=config.stage1,
    )
    if dry_run:
        return Stage1Result(paths, True, "dry_run", "imaging commands generated", commands=commands)
    missing = [p for p in (paths.calibrated_uvfits, paths.final_model, paths.stage_metadata) if not p.is_file()]
    if missing:
        return Stage1Result(paths, False, "failed", "cannot re-image, missing: " + ", ".join(m.name for m in missing))
    for path in paths.image_products():
        path.unlink(missing_ok=True)

    reporter.progress(label, 0, 1, f"imaging on a {cell_mas:.6g} mas cell")
    try:
        result = run_difmap(
            config.project.difmap.executable,
            commands,
            paths.reimage_log_file,
            cancel_event=cancel_event,
            stream=config.project.difmap.stream,
            nice=config.run.nice,
            cwd=paths.output_directory,
        )
    except (DifmapNotFound, OSError) as exc:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"could not run DifMAP: {exc}")
    if result.cancelled:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", "cancelled")
    if result.returncode != 0 or not (paths.clean_image.exists() and paths.residual_image.exists()):
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"common-grid imaging failed; see {paths.reimage_log_file}")
    for own_log in paths.output_directory.glob("difmap.log*"):
        own_log.unlink(missing_ok=True)

    rms = tagged_scalar(result.log_text, STAGE1_RMS_MARKER)
    if rms is None:
        rms = extract_last_numeric(result.log_text)
    beam = _stamp_beam(paths.clean_image, paths.residual_image, reporter, label)
    try:
        geometry = image_geometry(paths.clean_image)
        if not math.isclose(geometry["cell_mas"], cell_mas, rel_tol=1e-5):
            raise ValueError(
                f"DifMAP wrote a {geometry['cell_mas']:.6g} mas cell, not the requested {cell_mas:.6g} mas"
            )
        payload = json.loads(paths.stage_metadata.read_text(encoding="utf-8"))
        payload.update(
            {
                "final_residual_rms_jy_per_beam": rms,
                "clean_beam_deg": beam,
                "common_image_geometry": geometry,
                "common_cell_mas": cell_mas,
                "fits_image_size": geometry["naxis1"],
                "reimage_command_script_sha256": hashlib.sha256(commands.encode("utf-8")).hexdigest(),
                "reimage_duration_s": round(result.duration_s, 3),
                "reimaged_utc": utc_now(),
            }
        )
        write_json(paths.stage_metadata, payload)
        manifest = json.loads(paths.manifest.read_text(encoding="utf-8")) if paths.manifest.is_file() else {}
        manifest["products"] = [p.name for p in paths.products() if p != paths.manifest and p.exists()]
        if manifest:
            write_json(paths.manifest, manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"post-processing error after re-imaging: {exc}")
    reporter.progress(label, 1, 1, "completed")
    return Stage1Result(paths, True, "reimaged", f"imaged on {cell_mas:.6g} mas cell", rms_jy_per_beam=rms, commands=commands, geometry=geometry)


def _current_grid(paths: Stage1Paths) -> dict[str, Any] | None:
    if not paths.clean_image.is_file():
        return None
    try:
        return image_geometry(paths.clean_image)
    except (OSError, ValueError):
        return None


def _common_grid_pass(
    layout: Layout,
    config: LenspipeConfig,
    just_calibrated: set[str],
    *,
    overwrite: bool,
    reimage_only: bool,
    workers: int,
    reporter: Reporter,
    cancel_event: threading.Event | None,
) -> list[Stage1Result]:
    """Put every calibrated epoch of this mode on one pixel grid.

    The cell is ``stage1.cell_mas`` or the finest native cell among all epochs
    in the tree, not only the ones calibrated in this run, so a new epoch with a
    finer native cell re-images the older ones too.
    """
    candidates: list[Stage1Paths] = []
    for uvfits in discover_inputs(layout.root, config.stage1.input_pattern, None):
        try:
            paths = _paths_for_config(layout.root, uvfits, config)
        except ValueError:
            continue
        if paths.native_clean_image.is_file() and paths.calibrated_uvfits.is_file():
            candidates.append(paths)
    if not candidates:
        if reimage_only:
            reporter.warn("[stage1] nothing to re-image: no calibrated epochs with native maps found")
        return []
    try:
        natives = {p.prefix: image_geometry(p.native_clean_image) for p in candidates}
        cell = common_cell_mas(natives.values(), config.stage1.cell_mas)
    except (OSError, ValueError) as exc:
        reporter.error(f"[stage1] common grid not possible: {exc}")
        return [Stage1Result(p, False, "failed", f"common grid not possible: {exc}") for p in candidates]

    def needs_imaging(paths: Stage1Paths) -> bool:
        if overwrite or reimage_only or paths.prefix in just_calibrated:
            return True
        current = _current_grid(paths)
        return current is None or not math.isclose(current["cell_mas"], cell, rel_tol=1e-5)

    todo = [p for p in candidates if needs_imaging(p)]
    reporter.log(
        f"[stage1] common imaging cell {cell:.6g} mas on {config.stage1.map_pixels} pixels; "
        f"{len(todo)} of {len(candidates)} epoch(s) to image"
    )
    results: list[Stage1Result] = []
    if len(todo) <= 1 or workers == 1:
        for paths in todo:
            results.append(reimage_epoch(paths, config, cell, reporter=reporter, cancel_event=cancel_event))
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stage1-reimage") as pool:
            futures = [
                pool.submit(reimage_epoch, paths, config, cell, reporter=reporter, cancel_event=cancel_event)
                for paths in todo
            ]
            results = [future.result() for future in as_completed(futures)]
    results.sort(key=lambda item: (item.paths.source, item.paths.epoch))

    grids = {p.prefix: _current_grid(p) for p in candidates}
    present = {k: v for k, v in grids.items() if v is not None}
    if present:
        reference = next(iter(present.values()))
        odd = [k for k, v in present.items() if not same_grid(reference, v)]
        if odd:
            reporter.warn(f"[stage1] final maps are NOT on one grid; differing epochs: {', '.join(odd)}")
        else:
            reporter.log(f"[stage1] {len(present)} epoch(s) share one {reference['naxis1']}x{reference['naxis2']} grid")
    return results


def run_stage1(
    project_root: Path,
    config: LenspipeConfig,
    *,
    epochs: set[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    workers: int | None = None,
    reporter: Reporter | None = None,
    cancel_event: threading.Event | None = None,
    reimage_only: bool = False,
) -> list[Stage1Result]:
    """Run Stage 1 for every matching input, up to ``workers`` epochs at once.

    Two passes: calibration (self-cal, model fit, native maps) per epoch, then
    common-grid imaging of every calibrated epoch of the mode. ``reimage_only``
    skips the first pass.
    """
    reporter = reporter or Reporter()
    layout = Layout.at(project_root)
    if not layout.inputs.is_dir():
        raise FileNotFoundError(f"inputs directory not found: {layout.inputs}")
    for note in migrate_flat_layout(layout.root):
        reporter.log(f"[stage1] layout: {note}")

    uvfits_files = discover_inputs(layout.root, config.stage1.input_pattern, epochs)
    if not uvfits_files:
        selection = f" for epoch(s) {sorted(epochs)}" if epochs else ""
        raise FileNotFoundError(f"no matching UV-FITS files found{selection} in {layout.inputs}")

    datasets: list[Stage1Paths] = []
    results: list[Stage1Result] = []
    for uvfits in uvfits_files:
        try:
            datasets.append(_paths_for_config(layout.root, uvfits, config))
        except ValueError as exc:
            reporter.warn(f"skipped {uvfits.name}: {exc}")

    workers = max(1, workers or config.run.epoch_workers)
    if dry_run:
        for paths in datasets:
            result = run_epoch(paths, config, overwrite=overwrite, dry_run=True, reporter=reporter)
            _report(reporter, result)
            results.append(result)
            example = reimage_epoch(paths, config, config.stage1.cell_mas or 0.1, reporter=reporter, dry_run=True)
            example.message = (
                "common-grid imaging commands (cell shown is "
                + ("stage1.cell_mas" if config.stage1.cell_mas else "an example; the run uses the finest native cell")
                + ")"
            )
            _report(reporter, example)
        return results

    if not reimage_only:
        if workers == 1 or len(datasets) == 1:
            for paths in datasets:
                result = run_epoch(
                    paths, config, overwrite=overwrite, dry_run=False, reporter=reporter,
                    cancel_event=cancel_event,
                )
                _report(reporter, result)
                results.append(result)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stage1") as pool:
                futures = {
                    pool.submit(
                        run_epoch, paths, config, overwrite=overwrite, dry_run=False,
                        reporter=reporter, cancel_event=cancel_event,
                    ): paths
                    for paths in datasets
                }
                for future in as_completed(futures):
                    result = future.result()
                    _report(reporter, result)
                    results.append(result)
        results.sort(key=lambda item: (item.paths.source, item.paths.epoch))
        if cancel_event is not None and cancel_event.is_set():
            return results
        if any(r.status == "failed" for r in results):
            reporter.warn("[stage1] calibration incomplete; common-grid imaging not started")
            return results

    just_calibrated = {r.paths.prefix for r in results if r.status == "completed"}
    imaging = _common_grid_pass(
        layout, config, just_calibrated, overwrite=overwrite, reimage_only=reimage_only,
        workers=workers, reporter=reporter, cancel_event=cancel_event,
    )
    for result in imaging:
        _report(reporter, result)
    return results + imaging


def _report(reporter: Reporter, result: Stage1Result) -> None:
    tag = result.status.upper()
    reporter.log(f"[stage1 {result.paths.prefix}] {tag}: {result.message}")
    if result.status == "completed":
        reporter.log(f"  calibrated data: {result.paths.calibrated_uvfits}")
        reporter.log(f"  final model:     {result.paths.final_model}")
        reporter.log(f"  native maps:     {result.paths.native_clean_image.name}, {result.paths.native_residual_image.name}")
        reporter.log(f"  native RMS:      {result.rms_jy_per_beam}")
    elif result.status == "reimaged":
        reporter.log(f"  final maps:      {result.paths.clean_image.name}, {result.paths.residual_image.name}")
        reporter.log(f"  residual RMS:    {result.rms_jy_per_beam}")
    elif result.status == "dry_run" and result.commands:
        reporter.log("--- DifMAP commands ---")
        reporter.log(result.commands.rstrip("\n"))
        reporter.log("--- End commands ---")
