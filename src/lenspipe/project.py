"""Project directory layout, dataset naming, JSON products, and the inventory."""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lenspipe.config import CONFIG_FILENAME

__all__ = [
    "CALIBRATION_MODES",
    "Layout",
    "migrate_flat_layout",
    "ShardDecision",
    "auto_shards",
    "decide_shards",
    "inventory",
    "json_safe",
    "parse_epoch_filename",
    "physical_memory_bytes",
    "free_disk_bytes",
    "DISK_RESERVE_BYTES",
    "SCRATCH_PER_PROCESS",
    "utc_now",
    "write_json",
]


CALIBRATION_MODES: tuple[str, ...] = ("full-band", "per-if")
"""Stage 1 calibration modes; each has its own tree under stage1/, stage2/ and stage3/."""


@dataclass(frozen=True)
class Layout:
    """Directory layout of a project.

    Since 2.1 every stage directory holds one sub-tree per calibration mode::

        stage1/<mode>/<source>.<epoch>/...
        stage2/<mode>/<source>.<epoch>/...
        stage3/<mode>/<source>.<epoch>/<product>/...  and  stage3/<mode>/combined/<source>/...

    ``stage1``, ``stage2`` and ``stage3`` are the parents of those trees; use
    ``stage_dir(stage, mode)`` for the tree of one mode. Projects written before
    2.1 (no mode level) are moved into ``full-band/`` by ``migrate_flat_layout``.
    """

    root: Path

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def stage1(self) -> Path:
        return self.root / "stage1"

    @property
    def stage2(self) -> Path:
        return self.root / "stage2"

    @property
    def stage3(self) -> Path:
        return self.root / "stage3"

    def stage_dir(self, stage: int, mode: str) -> Path:
        """The tree of one calibration mode: ``<root>/stage<N>/<mode>``."""
        if mode not in CALIBRATION_MODES:
            raise ValueError(f"unknown calibration mode {mode!r}; expected one of {CALIBRATION_MODES}")
        if stage not in (1, 2, 3):
            raise ValueError(f"stage must be 1, 2 or 3, not {stage!r}")
        return self.root / f"stage{stage}" / mode

    def modes_present(self, stage: int) -> list[str]:
        """Calibration modes that have a tree under ``stage<N>/``."""
        parent = self.root / f"stage{stage}"
        return [mode for mode in CALIBRATION_MODES if (parent / mode).is_dir()]

    @property
    def jobs(self) -> Path:
        return self.root / ".jobs"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @classmethod
    def at(cls, root: Path | str) -> Layout:
        return cls(Path(root).expanduser().resolve())


def _flat_entries(parent: Path, marker_glob: str) -> list[Path]:
    """Pre-2.1 per-epoch directories directly under a stage directory."""
    if not parent.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name in CALIBRATION_MODES or child.name.startswith("."):
            continue
        if child.name == "combined" or next(child.glob(marker_glob), None) is not None:
            found.append(child)
    return found


def migrate_flat_layout(root: Path | str) -> list[str]:
    """Move pre-2.1 outputs (``stage1/<prefix>`` ...) into the ``full-band`` trees.

    Idempotent and rename-only, so it is instant on the same filesystem. A
    directory whose destination already exists is left alone and reported. The
    returned lines describe what moved; empty means the project was already laid
    out per mode.
    """
    layout = Layout.at(root)
    notes: list[str] = []
    markers = {1: "*.stage1.metadata.json", 2: "*.stage2.metadata.json", 3: "**/*.stage3.metadata.json"}
    for stage, marker in markers.items():
        parent = layout.root / f"stage{stage}"
        for entry in _flat_entries(parent, marker):
            destination = parent / "full-band" / entry.name
            if destination.exists():
                notes.append(f"left {entry} in place: {destination} already exists")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            entry.rename(destination)
            notes.append(f"moved {entry.relative_to(layout.root)} -> {destination.relative_to(layout.root)}")
    return notes


def parse_epoch_filename(path: Path, suffix: str) -> tuple[str, str]:
    """Parse ``<source>.<epoch><suffix>``; source may itself contain periods."""
    if not path.name.endswith(suffix):
        raise ValueError(f"Filename does not end in '{suffix}': {path.name}")
    stem = path.name[: -len(suffix)]
    try:
        source, epoch = stem.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError(f"Expected '<source>.<epoch>{suffix}', received '{path.name}'") from exc
    if not source or not epoch:
        raise ValueError(f"Expected '<source>.<epoch>{suffix}', received '{path.name}'")
    return source, epoch


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def json_safe(value: Any) -> Any:
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        np = None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if np is not None and isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(payload), handle, indent=2)
        handle.write("\n")


def physical_memory_bytes() -> int | None:
    """Total physical RAM, or None when the platform does not expose it."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return int(pages) * int(page_size)


SCRATCH_PER_PROCESS = 1.1
"""Disk a DifMAP process needs, as a multiple of its input: ``observe`` streams the whole
file into a hidden ``uvdata.scr`` in its working directory (same visibility layout, so about
the same size), plus small per-IF paging files."""

DISK_RESERVE_BYTES = 5 * (1 << 30)
"""Free space the shard planner never allocates to scratch copies."""


def free_disk_bytes(path: Path | str) -> int | None:
    """Free bytes on the filesystem that holds ``path`` (or its nearest existing ancestor)."""
    candidate = Path(path).expanduser()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return None


@dataclass(frozen=True)
class ShardDecision:
    shards: int
    cpu_cap: int
    memory_cap: int | None
    requested: int | str
    estimated_bytes_per_process: int | None
    memory_budget_bytes: int | None
    memory_multiple: float | None = None
    memory_multiple_source: str | None = None
    disk_cap: int | None = None  # processes whose scratch copies fit in free disk; 0 = not even one
    free_disk_bytes: int | None = None
    scratch_bytes_per_process: int | None = None

    @property
    def disk_short(self) -> bool:
        """True when the free disk cannot hold even one process's scratch copy."""
        return self.disk_cap is not None and self.disk_cap < 1

    def describe(self) -> str:
        parts = [f"shards={self.shards}", f"requested={self.requested}", f"cpu_cap={self.cpu_cap}"]
        if self.memory_cap is not None:
            parts.append(f"memory_cap={self.memory_cap}")
        if self.disk_cap is not None:
            parts.append(f"disk_cap={self.disk_cap}")
        if self.memory_multiple is not None:
            parts.append(f"memory_multiple={self.memory_multiple:g} [{self.memory_multiple_source}]")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shards": self.shards,
            "requested": self.requested,
            "cpu_cap": self.cpu_cap,
            "memory_cap": self.memory_cap,
            "disk_cap": self.disk_cap,
            "estimated_bytes_per_process": self.estimated_bytes_per_process,
            "memory_budget_bytes": self.memory_budget_bytes,
            "memory_multiple": self.memory_multiple,
            "memory_multiple_source": self.memory_multiple_source,
            "free_disk_bytes": self.free_disk_bytes,
            "scratch_bytes_per_process": self.scratch_bytes_per_process,
        }


def decide_shards(
    requested: int | str,
    fit_count: int,
    *,
    input_bytes: int | None = None,
    epoch_workers: int = 1,
    memory_fraction: float = 0.5,
    memory_multiple: float | str = 3.0,
    total_memory_bytes: int | None = None,
    cpu_count: int | None = None,
    free_disk_bytes: int | None = None,
    disk_reserve_bytes: int = DISK_RESERVE_BYTES,
) -> ShardDecision:
    """Resolve the ``shards`` setting against CPUs, memory, disk and the fit count.

    Every shard is its own DifMAP process. In memory each is estimated at
    ``memory_multiple`` times the input, sharing ``memory_fraction`` of physical RAM
    divided across concurrent epochs. On disk each keeps a hidden scratch copy of
    the input in its working directory (``SCRATCH_PER_PROCESS`` times the input),
    sharing the free space minus a reserve, again divided across concurrent
    epochs. An explicit shard count is honoured even when it exceeds a cap;
    ``auto`` never exceeds any of them, but always runs at least one process.
    """
    cores = cpu_count or os.cpu_count() or 2
    # Leave one core for the console and the OS, and share the rest between the
    # epochs that run at the same time, so the machine is never oversubscribed.
    cpu_cap = max(1, min(8, (cores - 1) // max(1, epoch_workers)))
    total = total_memory_bytes if total_memory_bytes is not None else physical_memory_bytes()

    from lenspipe.memory_calibration import effective_multiple

    multiple, source = effective_multiple(memory_multiple)
    memory_cap: int | None = None
    estimate: int | None = None
    budget: int | None = None
    if total and input_bytes:
        estimate = max(1, int(input_bytes * multiple))
        budget = int(total * memory_fraction / max(1, epoch_workers))
        memory_cap = max(1, budget // estimate)

    disk_cap: int | None = None
    scratch: int | None = None
    if free_disk_bytes is not None and input_bytes:
        scratch = max(1, int(input_bytes * SCRATCH_PER_PROCESS))
        usable = max(0, free_disk_bytes - disk_reserve_bytes) // max(1, epoch_workers)
        disk_cap = usable // scratch

    if requested == "auto":
        shards = cpu_cap
        if memory_cap is not None:
            shards = min(shards, memory_cap)
        if disk_cap is not None:
            shards = min(shards, max(1, disk_cap))
    else:
        shards = int(requested)
    shards = max(1, min(shards, max(1, fit_count)))
    return ShardDecision(
        shards, cpu_cap, memory_cap, requested, estimate, budget, multiple, source,
        disk_cap, free_disk_bytes, scratch,
    )


def auto_shards(requested: int | str, fit_count: int) -> int:
    """Backwards-compatible CPU-only resolution of the ``shards`` setting."""
    return decide_shards(requested, fit_count).shards


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def inventory(root: Path) -> dict[str, Any]:
    """Summarise what exists for each epoch across the stages, for the console and CLI.

    One row per (epoch, calibration mode) that has any stage output, plus a row
    with ``calibration_mode = None`` for an epoch that only has an input. Flat
    pre-2.1 layouts are moved into ``full-band/`` first (rename-only).
    """
    layout = Layout.at(root)
    migrate_flat_layout(layout.root)
    inputs: dict[str, dict[str, Any]] = {}
    epochs: dict[tuple[str, str | None], dict[str, Any]] = {}

    def entry(source: str, epoch: str, mode: str | None) -> dict[str, Any]:
        key = f"{source}.{epoch}"
        known = inputs.get(key, {})
        return epochs.setdefault(
            (key, mode),
            {
                "source": source,
                "epoch": epoch,
                "calibration_mode": mode,
                "input": known.get("input"),
                "master_model": known.get("master_model"),
                "stage1": None,
                "stage2": [],
                "stage3": [],
            },
        )

    if layout.inputs.is_dir():
        for path in sorted(layout.inputs.glob("*.uvfits")):
            try:
                source, epoch = parse_epoch_filename(path, ".uvfits")
            except ValueError:
                continue
            master = layout.inputs / f"{source}.gmod"
            inputs[f"{source}.{epoch}"] = {
                "source": source,
                "epoch": epoch,
                "input": path.name,
                "master_model": master.name if master.is_file() else None,
            }

    for mode in layout.modes_present(1):
        for meta in sorted(layout.stage_dir(1, mode).glob("*/*.stage1.metadata.json")):
            payload = _read_json(meta)
            if payload is None:
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")), mode)
            record["stage1"] = {
                "version": payload.get("version") or payload.get("lenspipe_version"),
                "rms_jy_per_beam": (
                    payload.get("final_residual_rms_jy_per_beam")
                    if payload.get("final_residual_rms_jy_per_beam") is not None
                    else payload.get("native_residual_rms_jy_per_beam")
                ),
                "common_grid": payload.get("common_image_geometry") is not None,
                "created_utc": payload.get("created_utc"),
            }

    for mode in layout.modes_present(2):
        for meta in sorted(layout.stage_dir(2, mode).glob("*/*.stage2.metadata.json")):
            payload = _read_json(meta)
            if payload is None:
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")), mode)
            prefix = f"{record['source']}.{record['epoch']}."
            tag = meta.name[len(prefix) : -len(".stage2.metadata.json")]
            record["stage2"].append(
                {
                    "product": tag,
                    "mode": payload.get("mode"),
                    "n_fits": payload.get("n_fits"),
                    "version": payload.get("stage2_version"),
                    "created_utc": payload.get("created_utc"),
                }
            )

    combined: list[dict[str, Any]] = []
    for mode in layout.modes_present(3):
        tree = layout.stage_dir(3, mode)
        for meta in sorted(tree.glob("*/*/**/*.stage3.metadata.json")):
            if meta.relative_to(tree).parts[0] == "combined":
                continue
            payload = _read_json(meta)
            if payload is None:
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")), mode)
            record["stage3"].append(
                {
                    "product": payload.get("product_tag"),
                    "analysis_tag": payload.get("analysis_tag"),
                    "version": payload.get("stage3_version"),
                    "created_utc": payload.get("created_utc"),
                }
            )
        combined_root = tree / "combined"
        if combined_root.is_dir():
            for meta in sorted(combined_root.glob("**/*.combined.stage3.metadata.json")):
                payload = _read_json(meta)
                if payload is None:
                    continue
                combined.append(
                    {
                        "source": payload.get("source"),
                        "calibration_mode": mode,
                        "product": payload.get("product_tag"),
                        "analysis_tag": payload.get("analysis_tag"),
                        "n_visits": payload.get("n_visits"),
                        "version": payload.get("stage3_version"),
                        "path": str(meta.parent),
                    }
                )

    for key, known in inputs.items():
        if not any(k == key for k, _ in epochs):
            entry(known["source"], known["epoch"], None)

    order = {mode: index for index, mode in enumerate(CALIBRATION_MODES)}
    rows = [epochs[key] for key in sorted(epochs, key=lambda k: (k[0], order.get(k[1] or "", -1)))]
    summary = {
        "root": str(layout.root),
        "config_present": layout.config_path.is_file(),
        "epochs": rows,
        "combined": combined,
        "freshness": {},
    }
    _attach_freshness(summary, layout.root)
    return summary


def _attach_freshness(summary: dict[str, Any], root: Path) -> None:
    """Annotate inventory entries with provenance status (stat-only, so it is cheap)."""
    from lenspipe.provenance import verify_project

    checks = verify_project(root, hash_on_change=False)  # never read GB files for a listing
    counts: dict[str, int] = {}
    by_epoch = {
        (f"{row['source']}.{row['epoch']}", row.get("calibration_mode")): row for row in summary["epochs"]
    }
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        row = by_epoch.get((f"{check.source}.{check.epoch}", check.calibration_mode))
        if check.stage == 1 and row and row.get("stage1"):
            row["stage1"]["status"] = check.status
            row["stage1"]["reasons"] = check.reasons
        elif check.stage == 2 and row:
            for product in row["stage2"]:
                if product["product"] == check.product:
                    product["status"] = check.status
                    product["reasons"] = check.reasons
        elif check.stage == 3 and check.epoch == "combined":
            for item in summary["combined"]:
                label = str(item["product"]) + (f"/{item['analysis_tag']}" if item.get("analysis_tag") else "")
                if (
                    item["source"] == check.source
                    and item.get("calibration_mode") == check.calibration_mode
                    and label == check.product
                ):
                    item["status"] = check.status
                    item["reasons"] = check.reasons
        elif check.stage == 3 and row:
            for product in row["stage3"]:
                label = str(product["product"]) + (
                    f"/{product['analysis_tag']}" if product.get("analysis_tag") else ""
                )
                if label == check.product:
                    product["status"] = check.status
                    product["reasons"] = check.reasons
    summary["freshness"] = counts
