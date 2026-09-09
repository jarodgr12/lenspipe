"""Product provenance: fingerprints of inputs and a freshness check across stages.

Every stage records a fingerprint (size, mtime, sha256) of the files it read.
``verify_project`` compares those with the files as they are now. Size and
mtime are checked first; the hash is only recomputed when they differ, so a
verify pass over multi-gigabyte visits stays fast unless something changed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lenspipe.project import Layout
from lenspipe.uvfits import file_sha256

__all__ = ["Check", "fingerprint", "verify_project"]

FRESH = "fresh"
STALE = "stale"
UPSTREAM = "upstream-stale"
UNKNOWN = "unknown"
MISSING = "missing-input"


def fingerprint(path: Path, *, with_hash: bool = True) -> dict[str, Any]:
    """Size, mtime and (optionally) sha256 of one file, for later comparison."""
    stat = os.stat(path)
    record: dict[str, Any] = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if with_hash:
        record["sha256"] = file_sha256(path)
    return record


def _changed(recorded: dict[str, Any] | None, *, force_hash: bool = False) -> str | None:
    """Return None when the file matches its recorded fingerprint, else a reason."""
    if not recorded or "path" not in recorded:
        return "no fingerprint recorded"
    path = Path(recorded["path"])
    if not path.exists():
        return f"missing: {path.name}"
    stat = os.stat(path)
    same_stat = stat.st_size == recorded.get("size") and stat.st_mtime_ns == recorded.get("mtime_ns")
    if same_stat and not force_hash:
        return None
    recorded_hash = recorded.get("sha256")
    if recorded_hash is None:
        return f"size or mtime changed: {path.name}"
    if file_sha256(path) == recorded_hash:
        return None
    return f"content changed: {path.name}"


@dataclass
class Check:
    stage: int
    source: str
    epoch: str
    product: str | None
    status: str
    reasons: list[str] = field(default_factory=list)
    metadata_path: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}.{self.epoch}" + (f".{self.product}" if self.product else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "source": self.source,
            "epoch": self.epoch,
            "product": self.product,
            "status": self.status,
            "reasons": list(self.reasons),
            "metadata": self.metadata_path,
        }


def _load(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _check_provenance(payload: dict[str, Any], force_hash: bool) -> tuple[str, list[str]]:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict) or not provenance:
        return UNKNOWN, ["produced before provenance was recorded"]
    reasons: list[str] = []
    for name, recorded in provenance.items():
        if isinstance(recorded, dict) and "path" in recorded:
            reason = _changed(recorded, force_hash=force_hash)
            if reason:
                reasons.append(f"{name}: {reason}")
        elif isinstance(recorded, dict):
            for sub_name, sub_recorded in recorded.items():
                reason = _changed(sub_recorded, force_hash=force_hash)
                if reason:
                    reasons.append(f"{name}[{sub_name}]: {reason}")
    if any("missing:" in reason for reason in reasons):
        return MISSING, reasons
    return (STALE if reasons else FRESH), reasons


def verify_project(project_root: Path, *, force_hash: bool = False) -> list[Check]:
    """Check every recorded product against its inputs; downstream of stale is upstream-stale."""
    layout = Layout.at(project_root)
    checks: list[Check] = []
    epoch_status: dict[tuple[str, str], str] = {}

    for meta in sorted(layout.stage1.glob("*/*.stage1.metadata.json")) if layout.stage1.is_dir() else []:
        payload = _load(meta)
        if payload is None:
            continue
        source, epoch = str(payload.get("source", "")), str(payload.get("epoch", ""))
        status, reasons = _check_provenance(payload, force_hash)
        checks.append(Check(1, source, epoch, None, status, reasons, str(meta)))
        epoch_status[(source, epoch)] = status

    product_status: dict[tuple[str, str, str], str] = {}
    for meta in sorted(layout.stage2.glob("*/*.stage2.metadata.json")) if layout.stage2.is_dir() else []:
        payload = _load(meta)
        if payload is None:
            continue
        source, epoch = str(payload.get("source", "")), str(payload.get("epoch", ""))
        prefix = f"{source}.{epoch}."
        product = meta.name[len(prefix) : -len(".stage2.metadata.json")]
        status, reasons = _check_provenance(payload, force_hash)
        upstream = epoch_status.get((source, epoch))
        if status == FRESH and upstream in {STALE, UPSTREAM, MISSING}:
            status, reasons = UPSTREAM, [f"stage 1 for {source}.{epoch} is {upstream}"]
        checks.append(Check(2, source, epoch, product, status, reasons, str(meta)))
        product_status[(source, epoch, product)] = status

    if layout.stage3.is_dir():
        for meta in sorted(layout.stage3.rglob("*.stage3.metadata.json")):
            payload = _load(meta)
            if payload is None:
                continue
            source = str(payload.get("source", ""))
            product = payload.get("product_tag")
            tag = payload.get("analysis_tag")
            label = f"{product}/{tag}" if tag else str(product)
            status, reasons = _check_provenance(payload, force_hash)
            if meta.name.endswith(".combined.stage3.metadata.json"):
                epochs = [str(e) for e in payload.get("epochs", [])]
                upstream = [
                    e for e in epochs
                    if product_status.get((source, e, str(product))) in {STALE, UPSTREAM, MISSING}
                ]
                if status == FRESH and upstream:
                    status, reasons = UPSTREAM, [f"stage 2 stale for epoch(s) {', '.join(upstream)}"]
                checks.append(Check(3, source, "combined", label, status, reasons, str(meta)))
            else:
                epoch = str(payload.get("epoch", ""))
                upstream = product_status.get((source, epoch, str(product)))
                if status == FRESH and upstream in {STALE, UPSTREAM, MISSING}:
                    status, reasons = UPSTREAM, [f"stage 2 {product} for {source}.{epoch} is {upstream}"]
                checks.append(Check(3, source, epoch, label, status, reasons, str(meta)))
    return checks
