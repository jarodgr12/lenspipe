"""Persistent shard plans so an interrupted Stage 2 run can be resumed.

The work directory ``<stage2 epoch dir>/.<product>.work/`` holds ``plan.json``
(the fit ranges, how they were split, and a fingerprint of everything the
result depends on), one DifMAP log per shard, a ``done`` record per finished
shard, and the fitted model files. A resumed run re-executes only shards
without a done record and refuses to continue if the fingerprint changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lenspipe.config import Stage2Config

__all__ = [
    "ShardPlan",
    "completed_shards",
    "mark_done",
    "plan_fingerprint",
    "shard_done_path",
    "shard_log_path",
    "work_directory_for",
]

PLAN_FILENAME = "plan.json"


def work_directory_for(output_directory: Path, product_prefix: str) -> Path:
    return output_directory / f".{product_prefix}.work"


def shard_log_path(work_dir: Path, index: int) -> Path:
    return work_dir / f"shard_{index:02d}.difmap.log"


def shard_done_path(work_dir: Path, index: int) -> Path:
    return work_dir / f"shard_{index:02d}.done.json"


def plan_fingerprint(
    calibrated_uvfits: Path,
    stage2_model: Path,
    config: Stage2Config,
    fit_ranges: list[tuple[int, int, int]],
) -> str:
    """Hash of everything a Stage 2 result depends on, cheap enough to run at start-up."""
    stat = os.stat(calibrated_uvfits)
    payload = {
        "calibrated_uvfits": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "stage2_model_sha256": hashlib.sha256(stage2_model.read_bytes()).hexdigest(),
        "mode": config.mode,
        "fit_ranges": fit_ranges,
        "modelfit_iterations": config.modelfit_iterations,
        "map_pixels": config.map_pixels,
        "cell_mas": config.cell_mas,
        "weighting": config.weighting,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


@dataclass
class ShardPlan:
    fingerprint: str
    mode: str
    fit_ranges: list[tuple[int, int, int]]
    blocks: list[list[tuple[int, int, int]]]
    created_utc: str

    def save(self, work_dir: Path) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / PLAN_FILENAME).write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")

    @classmethod
    def load(cls, work_dir: Path) -> ShardPlan | None:
        try:
            payload = json.loads((work_dir / PLAN_FILENAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls(
                fingerprint=str(payload["fingerprint"]),
                mode=str(payload["mode"]),
                fit_ranges=[tuple(int(v) for v in item) for item in payload["fit_ranges"]],
                blocks=[[tuple(int(v) for v in item) for item in block] for block in payload["blocks"]],
                created_utc=str(payload.get("created_utc", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def mark_done(work_dir: Path, index: int, record: dict[str, Any]) -> None:
    shard_done_path(work_dir, index).write_text(json.dumps(record, indent=1), encoding="utf-8")


def completed_shards(work_dir: Path, plan: ShardPlan) -> dict[int, dict[str, Any]]:
    """Shards with a done record and an intact log."""
    done: dict[int, dict[str, Any]] = {}
    for index in range(len(plan.blocks)):
        marker = shard_done_path(work_dir, index)
        if not marker.is_file() or not shard_log_path(work_dir, index).is_file():
            continue
        try:
            done[index] = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return done
