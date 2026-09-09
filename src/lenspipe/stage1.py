"""Stage 1: self-calibration, model fitting, and imaging of one epoch."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from lenspipe import __version__
from lenspipe.config import LenspipeConfig
from lenspipe.difmap import (
    STAGE1_RMS_MARKER,
    difmap_version,
    extract_last_numeric,
    run_difmap,
    tagged_scalar,
)
from lenspipe.difmap.runner import DifmapNotFound
from lenspipe.difmap.scripts import stage1_commands
from lenspipe.models import ModelFormatError, ModelHierarchy, label_fitted_model, parse_master_model
from lenspipe.progress import Reporter
from lenspipe.project import Layout, parse_epoch_filename, utc_now, write_json
from lenspipe.provenance import fingerprint

__all__ = [
    "STAGE_NUMBER",
    "Stage1Paths",
    "Stage1Result",
    "discover_inputs",
    "make_paths",
    "run_epoch",
    "run_stage1",
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

    @property
    def prefix(self) -> str:
        return f"{self.source}.{self.epoch}"

    def products(self) -> tuple[Path, ...]:
        return (
            self.calibrated_uvfits,
            self.final_model,
            self.clean_image,
            self.residual_image,
            self.log_file,
            self.model_metadata,
            self.stage_metadata,
            self.manifest,
        )


@dataclass
class Stage1Result:
    paths: Stage1Paths
    ok: bool
    status: str  # completed | skipped | dry_run | failed
    message: str = ""
    rms_jy_per_beam: float | None = None
    commands: str | None = None


def make_paths(project_root: Path, uvfits: Path) -> Stage1Paths:
    source, epoch = parse_epoch_filename(uvfits, ".uvfits")
    prefix = f"{source}.{epoch}"
    layout = Layout.at(project_root)
    out = (layout.stage1 / prefix).resolve()
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
    )


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
    selected_ranges: list[tuple[int, int]],
    config: LenspipeConfig,
    commands: str,
    difmap_info: dict[str, str | None],
    duration_s: float,
) -> None:
    created = utc_now()
    stage1 = config.stage1
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
            "final_residual_rms_jy_per_beam": rms,
            "difmap": difmap_info,
            "command_script_sha256": hashlib.sha256(commands.encode("utf-8")).hexdigest(),
            "duration_s": round(duration_s, 3),
            "final_if_selfcal": {
                "enabled": bool(selected_ranges),
                "n_ifs": len(selected_ranges),
                "channels_per_if": stage1.final_if_selfcal.channels_per_if,
                "edge_channels_excluded_per_side": stage1.final_if_selfcal.edge_channels,
                "retained_channels_per_if": (
                    stage1.final_if_selfcal.channels_per_if
                    - 2 * stage1.final_if_selfcal.edge_channels
                ),
                "amplitude_phase_interval_minutes": 3600.0,
                "phase_only_interval_minutes": 0.5,
                "modelfit_within_if_loop": False,
                "selected_ranges_one_based": [
                    {"if_number": index, "first_channel": first, "last_channel": last}
                    for index, (first, last) in enumerate(selected_ranges, start=1)
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
            "products": [path.name for path in paths.products() if path != paths.manifest],
        },
    )


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

    try:
        commands, selected_ranges = stage1_commands(
            uvfits=paths.uvfits,
            starting_model=paths.starting_model,
            residual_image=paths.residual_image,
            clean_image=paths.clean_image,
            calibrated_uvfits=paths.calibrated_uvfits,
            final_model=paths.final_model,
            config=config.stage1,
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

    steps_total = len(config.stage1.selfcal)
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

    required = (paths.calibrated_uvfits, paths.final_model, paths.clean_image, paths.residual_image)
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

    try:
        label_fitted_model(paths.final_model, hierarchy)
        _write_metadata(
            paths,
            hierarchy,
            rms,
            selected_ranges,
            config,
            commands,
            difmap_version(config.project.difmap.executable),
            result.duration_s,
        )
    except (OSError, ModelFormatError, ValueError) as exc:
        reporter.clear(label)
        return Stage1Result(paths, False, "failed", f"post-processing error: {exc}")

    reporter.progress(label, steps_total, steps_total, "completed")
    return Stage1Result(paths, True, "completed", "completed", rms_jy_per_beam=rms, commands=commands)


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
) -> list[Stage1Result]:
    """Run Stage 1 for every matching input, up to ``workers`` epochs at once."""
    reporter = reporter or Reporter()
    layout = Layout.at(project_root)
    if not layout.inputs.is_dir():
        raise FileNotFoundError(f"inputs directory not found: {layout.inputs}")

    uvfits_files = discover_inputs(layout.root, config.stage1.input_pattern, epochs)
    if not uvfits_files:
        selection = f" for epoch(s) {sorted(epochs)}" if epochs else ""
        raise FileNotFoundError(f"no matching UV-FITS files found{selection} in {layout.inputs}")

    datasets: list[Stage1Paths] = []
    results: list[Stage1Result] = []
    for uvfits in uvfits_files:
        try:
            datasets.append(make_paths(layout.root, uvfits))
        except ValueError as exc:
            reporter.warn(f"skipped {uvfits.name}: {exc}")

    workers = max(1, workers or config.run.epoch_workers)
    if dry_run or workers == 1 or len(datasets) == 1:
        for paths in datasets:
            result = run_epoch(
                paths, config, overwrite=overwrite, dry_run=dry_run, reporter=reporter,
                cancel_event=cancel_event,
            )
            _report(reporter, result)
            results.append(result)
        return results

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
    return results


def _report(reporter: Reporter, result: Stage1Result) -> None:
    tag = result.status.upper()
    reporter.log(f"[stage1 {result.paths.prefix}] {tag}: {result.message}")
    if result.status == "completed":
        reporter.log(f"  calibrated data: {result.paths.calibrated_uvfits}")
        reporter.log(f"  final model:     {result.paths.final_model}")
        reporter.log(f"  residual RMS:    {result.rms_jy_per_beam}")
    elif result.status == "dry_run" and result.commands:
        reporter.log("--- DifMAP commands ---")
        reporter.log(result.commands.rstrip("\n"))
        reporter.log("--- End commands ---")
