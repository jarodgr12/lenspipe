"""Build ``lenspipe`` argv lists for jobs from the Run page's choices (pure, testable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lenspipe.jobs import _quote
from lenspipe.ui.state import ChainStep

__all__ = ["RunRequest", "build_steps", "calibrate_step", "command_line"]


@dataclass
class RunRequest:
    root: Path
    stages: list[int]
    epochs: list[str] = field(default_factory=list)
    overwrite: bool = False
    dry_run: bool = False
    workers: int | None = None
    stage2_mode: str | None = None
    stage2_shards: str | None = None
    stage2_channels_per_if: int | None = None
    stage2_channels: str | None = None
    stage3_error_source: str | None = None
    stage3_product: str | None = None
    stage3_workers: int | None = None


def command_line(argv: list[str]) -> str:
    """Same rendering as :attr:`JobRecord.command_line` so the preview matches the job."""
    return "lenspipe " + " ".join(_quote(arg) for arg in argv)


def _epoch_flags(epochs: list[str]) -> list[str]:
    flags: list[str] = []
    for epoch in epochs:
        flags += ["--epoch", epoch]
    return flags


def build_steps(request: RunRequest) -> tuple[list[ChainStep], list[str]]:
    """Return one step per selected stage (in order) plus human-readable notes."""
    root = str(request.root)
    steps: list[ChainStep] = []
    notes: list[str] = []
    epochs = _epoch_flags(request.epochs)
    suffix = " " + ",".join(request.epochs) if request.epochs else ""

    if 1 in request.stages:
        argv = ["stage1", root, *epochs]
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
        if request.stage2_mode != "channel" and request.stage2_channels_per_if:
            argv += ["--channels-per-if", str(request.stage2_channels_per_if)]
        if request.stage2_mode == "channel" and request.stage2_channels:
            argv += ["--channels", request.stage2_channels]
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
