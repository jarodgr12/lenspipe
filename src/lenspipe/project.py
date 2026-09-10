"""Project directory layout, dataset naming, JSON products, and the inventory."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lenspipe.config import CONFIG_FILENAME

__all__ = [
    "Layout",
    "ShardDecision",
    "auto_shards",
    "decide_shards",
    "inventory",
    "json_safe",
    "parse_epoch_filename",
    "physical_memory_bytes",
    "utc_now",
    "write_json",
]


@dataclass(frozen=True)
class Layout:
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

    @property
    def jobs(self) -> Path:
        return self.root / ".jobs"

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    @classmethod
    def at(cls, root: Path | str) -> Layout:
        return cls(Path(root).expanduser().resolve())


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


@dataclass(frozen=True)
class ShardDecision:
    shards: int
    cpu_cap: int
    memory_cap: int | None
    requested: int | str
    estimated_bytes_per_process: int | None
    memory_budget_bytes: int | None

    def describe(self) -> str:
        parts = [f"shards={self.shards}", f"requested={self.requested}", f"cpu_cap={self.cpu_cap}"]
        if self.memory_cap is not None:
            parts.append(f"memory_cap={self.memory_cap}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shards": self.shards,
            "requested": self.requested,
            "cpu_cap": self.cpu_cap,
            "memory_cap": self.memory_cap,
            "estimated_bytes_per_process": self.estimated_bytes_per_process,
            "memory_budget_bytes": self.memory_budget_bytes,
        }


def decide_shards(
    requested: int | str,
    fit_count: int,
    *,
    input_bytes: int | None = None,
    epoch_workers: int = 1,
    memory_fraction: float = 0.5,
    memory_multiple: float = 3.0,
    total_memory_bytes: int | None = None,
    cpu_count: int | None = None,
) -> ShardDecision:
    """Resolve the ``shards`` setting against CPUs, memory and the fit count.

    Every shard loads the whole dataset in its own DifMAP, so the memory budget
    (``memory_fraction`` of physical RAM, divided across concurrent epochs) is
    shared by ``shards`` processes each estimated at ``memory_multiple`` times
    the input size. An explicit shard count is honoured even when it exceeds
    the memory cap; ``auto`` never exceeds it.
    """
    cores = cpu_count or os.cpu_count() or 2
    # Leave one core for the console and the OS, and share the rest between the
    # epochs that run at the same time, so the machine is never oversubscribed.
    cpu_cap = max(1, min(8, (cores - 1) // max(1, epoch_workers)))
    total = total_memory_bytes if total_memory_bytes is not None else physical_memory_bytes()

    memory_cap: int | None = None
    estimate: int | None = None
    budget: int | None = None
    if total and input_bytes:
        estimate = max(1, int(input_bytes * memory_multiple))
        budget = int(total * memory_fraction / max(1, epoch_workers))
        memory_cap = max(1, budget // estimate)

    if requested == "auto":
        shards = cpu_cap if memory_cap is None else min(cpu_cap, memory_cap)
    else:
        shards = int(requested)
    shards = max(1, min(shards, max(1, fit_count)))
    return ShardDecision(shards, cpu_cap, memory_cap, requested, estimate, budget)


def auto_shards(requested: int | str, fit_count: int) -> int:
    """Backwards-compatible CPU-only resolution of the ``shards`` setting."""
    return decide_shards(requested, fit_count).shards


def inventory(root: Path) -> dict[str, Any]:
    """Summarise what exists for each epoch across the stages, for the console and CLI."""
    layout = Layout.at(root)
    epochs: dict[str, dict[str, Any]] = {}

    def entry(source: str, epoch: str) -> dict[str, Any]:
        key = f"{source}.{epoch}"
        return epochs.setdefault(
            key,
            {
                "source": source,
                "epoch": epoch,
                "input": None,
                "master_model": None,
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
            record = entry(source, epoch)
            record["input"] = path.name
            master = layout.inputs / f"{source}.gmod"
            record["master_model"] = master.name if master.is_file() else None

    if layout.stage1.is_dir():
        for meta in sorted(layout.stage1.glob("*/*.stage1.metadata.json")):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")))
            record["stage1"] = {
                "version": payload.get("version") or payload.get("lenspipe_version"),
                "rms_jy_per_beam": payload.get("final_residual_rms_jy_per_beam"),
                "created_utc": payload.get("created_utc"),
            }

    if layout.stage2.is_dir():
        for meta in sorted(layout.stage2.glob("*/*.stage2.metadata.json")):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")))
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

    if layout.stage3.is_dir():
        for meta in sorted(layout.stage3.glob("*/*/**/*.stage3.metadata.json")):
            if meta.parts[-4] == "combined" or "combined" in meta.relative_to(layout.stage3).parts[:1]:
                continue
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = entry(str(payload.get("source", "")), str(payload.get("epoch", "")))
            record["stage3"].append(
                {
                    "product": payload.get("product_tag"),
                    "analysis_tag": payload.get("analysis_tag"),
                    "version": payload.get("stage3_version"),
                    "created_utc": payload.get("created_utc"),
                }
            )

    combined: list[dict[str, Any]] = []
    combined_root = layout.stage3 / "combined"
    if combined_root.is_dir():
        for meta in sorted(combined_root.glob("**/*.combined.stage3.metadata.json")):
            try:
                payload = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            combined.append(
                {
                    "source": payload.get("source"),
                    "product": payload.get("product_tag"),
                    "analysis_tag": payload.get("analysis_tag"),
                    "n_visits": payload.get("n_visits"),
                    "version": payload.get("stage3_version"),
                    "path": str(meta.parent),
                }
            )

    summary = {
        "root": str(layout.root),
        "config_present": layout.config_path.is_file(),
        "epochs": [epochs[key] for key in sorted(epochs)],
        "combined": combined,
        "freshness": {},
    }
    _attach_freshness(summary, layout.root)
    return summary


def _attach_freshness(summary: dict[str, Any], root: Path) -> None:
    """Annotate inventory entries with provenance status (stat-only, so it is cheap)."""
    from lenspipe.provenance import verify_project

    checks = verify_project(root)
    counts: dict[str, int] = {}
    by_epoch = {f"{row['source']}.{row['epoch']}": row for row in summary["epochs"]}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        row = by_epoch.get(f"{check.source}.{check.epoch}")
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
                if item["source"] == check.source and label == check.product:
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
