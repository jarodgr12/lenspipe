"""Build ``lenspipe`` argv lists for jobs from the Run page's choices (pure, testable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lenspipe.jobs import JobRecord, _quote
from lenspipe.ui.state import ChainStep

__all__ = ["RunRequest", "build_steps", "calibrate_step", "can_resume", "command_line", "resume_step"]


@dataclass
class RunRequest:
    """Per-run choices. ``None`` means "leave it to lenspipe.toml" and emits no flag."""

    root: Path
    stages: list[int]
    epochs: list[str] = field(default_factory=list)
    overwrite: bool = False
    dry_run: bool = False
    workers: int | None = None
    stage1_final_if_selfcal: bool | None = None
    stage2_mode: str | None = None
    stage2_shards: str | None = None
    stage2_channels_per_if: int | None = None
    stage2_channels: str | None = None
    stage2_exclude_edge_channels: int | None = None
    stage2_modelfit_iterations: int | None = None
    stage2_unflag: bool | None = None
    stage2_keep_models: bool | None = None
    stage2_plots: bool | None = None
    stage2_error_bars: bool | None = None
    stage3_error_source: str | None = None
    stage3_product: str | None = None
    stage3_workers: int | None = None
    stage3_fit_method: str | None = None
    stage3_reference_frequency: float | None = None
    stage3_exclude_channels: str | None = None
    stage3_exclude_epoch_channels: list[str] = field(default_factory=list)
    stage3_annotations: bool | None = None
    stage3_error_bars: bool | None = None
    stage3_formats: str | None = None


def command_line(argv: list[str]) -> str:
    """Same rendering as :attr:`JobRecord.command_line` so the preview matches the job."""
    return "lenspipe " + " ".join(_quote(arg) for arg in argv)


def _epoch_flags(epochs: list[str]) -> list[str]:
    flags: list[str] = []
    for epoch in epochs:
        flags += ["--epoch", epoch]
    return flags


def _toggle(value: bool | None, on: str, off: str) -> list[str]:
    if value is None:
        return []
    return [on if value else off]


def _number(value: float | int | None, flag: str) -> list[str]:
    if value is None:
        return []
    text = str(value)
    if isinstance(value, float) and text.endswith(".0"):
        text = text[:-2]
    return [flag, text]


def build_steps(request: RunRequest) -> tuple[list[ChainStep], list[str]]:
    """Return one step per selected stage (in order) plus human-readable notes."""
    root = str(request.root)
    steps: list[ChainStep] = []
    notes: list[str] = []
    epochs = _epoch_flags(request.epochs)
    suffix = " " + ",".join(request.epochs) if request.epochs else ""

    if 1 in request.stages:
        argv = ["stage1", root, *epochs]
        argv += _toggle(request.stage1_final_if_selfcal, "--final-if-selfcal", "--no-final-if-selfcal")
        if request.workers:
            argv += ["--workers", str(request.workers)]
        if request.overwrite:
            argv.append("--overwrite")
        if request.dry_run:
            argv.append("--dry-run")
        steps.append(ChainStep(argv, f"Stage 1{suffix}", "stage1"))

    if 2 in request.stages:
        argv = ["stage2", root, *epochs]
        if request.stage2_mode:
            argv += ["--mode", request.stage2_mode]
        if request.stage2_mode != "channel":
            argv += _number(request.stage2_channels_per_if, "--channels-per-if")
            argv += _number(request.stage2_exclude_edge_channels, "--exclude-edge-channels")
        if request.stage2_mode == "channel" and request.stage2_channels:
            argv += ["--channels", request.stage2_channels]
        argv += _number(request.stage2_modelfit_iterations, "--modelfit-iterations")
        argv += _toggle(request.stage2_unflag, "--unflag", "--no-unflag")
        argv += _toggle(request.stage2_keep_models, "--keep-models", "--no-keep-models")
        argv += _toggle(request.stage2_plots, "--plots", "--no-plots")
        argv += _toggle(request.stage2_error_bars, "--error-bars", "--no-error-bars")
        if request.stage2_shards:
            argv += ["--shards", request.stage2_shards]
        if request.workers:
            argv += ["--workers", str(request.workers)]
        if request.overwrite:
            argv.append("--overwrite")
        if request.dry_run:
            argv.append("--dry-run")
        steps.append(ChainStep(argv, f"Stage 2{suffix}", "stage2"))

    if 3 in request.stages:
        if request.dry_run:
            notes.append("stage3 has no --dry-run; it is left out of a dry run.")
        else:
            argv = ["stage3", root, *epochs]
            if request.stage3_product:
                argv += ["--product", request.stage3_product]
            if request.stage3_error_source:
                argv += ["--error-source", request.stage3_error_source]
            if request.stage3_fit_method:
                argv += ["--fit-method", request.stage3_fit_method]
            argv += _number(request.stage3_reference_frequency, "--reference-frequency")
            if request.stage3_exclude_channels:
                argv += ["--exclude-channels", request.stage3_exclude_channels]
            for item in request.stage3_exclude_epoch_channels:
                argv += ["--exclude-epoch-channels", item]
            argv += _toggle(request.stage3_annotations, "--annotations", "--no-annotations")
            argv += _toggle(request.stage3_error_bars, "--error-bars", "--no-error-bars")
            if request.stage3_formats:
                argv += ["--formats", request.stage3_formats]
            if request.stage3_workers:
                argv += ["--workers", str(request.stage3_workers)]
            if request.overwrite:
                argv.append("--overwrite")
            steps.append(ChainStep(argv, f"Stage 3{suffix}", "stage3"))

    if len(steps) > 1:
        notes.append("Later stages are submitted only after the previous job completes.")
    return steps, notes


def calibrate_step(root: Path, steps: str | None, list_steps: bool) -> ChainStep:
    argv = ["calibrate", str(root)]
    if steps:
        argv += ["--steps", steps]
    if list_steps:
        argv.append("--list-steps")
    return ChainStep(argv, "Calibrate (CASA)" + (f" {steps}" if steps else ""), "calibrate")


_NOT_RESUMABLE = {"--dry-run", "--recover-from-log"}


def can_resume(record: JobRecord) -> bool:
    """A Stage 2 fit that stopped short can continue from its shard checkpoints."""
    if record.stage != "stage2" or record.status not in {"failed", "cancelled"}:
        return False
    return not (_NOT_RESUMABLE & set(record.argv))


def resume_step(record: JobRecord) -> ChainStep:
    """The same Stage 2 command with ``--resume``; ``--overwrite`` would discard the checkpoints."""
    argv = [arg for arg in record.argv if arg not in {"--overwrite", "--resume"}]
    argv.append("--resume")
    title = record.title if record.title.endswith("(resume)") else f"{record.title} (resume)"
    return ChainStep(argv, title, "stage2")
