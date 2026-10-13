"""Compare the products of two project directories, for validating v2 against legacy runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from lenspipe.project import Layout

__all__ = ["Difference", "compare_projects"]

STAGE2_META_KEYS = (
    "mode", "channels_per_if", "excluded_edge_channels_per_side", "fitted_channels_per_if",
    "total_channels", "n_fits", "groups", "components", "mjd",
)


@dataclass
class Difference:
    stage: int
    item: str
    status: str  # identical | equivalent | different | missing
    detail: str = ""
    columns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"identical", "equivalent"}

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage, "item": self.item, "status": self.status,
            "detail": self.detail, "columns": list(self.columns),
        }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _compare_files(stage: int, item: str, left: Path, right: Path, text: bool) -> Difference:
    if not left.exists() or not right.exists():
        side = "left" if not left.exists() else "right"
        return Difference(stage, item, "missing", f"absent on the {side} side")
    if text:
        if left.read_text(errors="replace") == right.read_text(errors="replace"):
            return Difference(stage, item, "identical")
        return Difference(stage, item, "different", "text differs")
    if left.stat().st_size == right.stat().st_size and _sha(left) == _sha(right):
        return Difference(stage, item, "identical")
    return Difference(stage, item, "different", "bytes differ")


def _compare_csv(stage: int, item: str, left: Path, right: Path, rtol: float, atol: float) -> Difference:
    if not left.exists() or not right.exists():
        side = "left" if not left.exists() else "right"
        return Difference(stage, item, "missing", f"absent on the {side} side")
    if left.read_bytes() == right.read_bytes():
        return Difference(stage, item, "identical")
    try:
        a = pd.read_csv(left)
        b = pd.read_csv(right)
    except Exception as exc:  # noqa: BLE001
        return Difference(stage, item, "different", f"could not parse: {exc}")
    if list(a.columns) != list(b.columns):
        return Difference(
            stage, item, "different", "column sets differ",
            sorted(set(a.columns) ^ set(b.columns)),
        )
    if len(a) != len(b):
        return Difference(stage, item, "different", f"row counts differ ({len(a)} vs {len(b)})")
    bad: list[str] = []
    worst = 0.0
    for column in a.columns:
        x, y = a[column], b[column]
        if pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y):
            xv = x.to_numpy(dtype=float)
            yv = y.to_numpy(dtype=float)
            both_nan = np.isnan(xv) & np.isnan(yv)
            close = np.isclose(xv, yv, rtol=rtol, atol=atol, equal_nan=True) | both_nan
            if not close.all():
                bad.append(column)
                with np.errstate(invalid="ignore", divide="ignore"):
                    rel = np.nanmax(np.abs(xv - yv) / np.maximum(np.abs(yv), atol))
                worst = max(worst, float(rel))
        elif not x.astype(str).equals(y.astype(str)):
            bad.append(column)
    if bad:
        return Difference(
            stage, item, "different", f"{len(bad)} column(s) differ; worst relative difference {worst:.3g}", bad
        )
    return Difference(stage, item, "equivalent", f"numerically equal within rtol={rtol:g}, atol={atol:g}")


def _compare_json_subset(stage: int, item: str, left: Path, right: Path, keys: tuple[str, ...]) -> Difference:
    if not left.exists() or not right.exists():
        side = "left" if not left.exists() else "right"
        return Difference(stage, item, "missing", f"absent on the {side} side")
    a = json.loads(left.read_text())
    b = json.loads(right.read_text())
    bad = [key for key in keys if a.get(key) != b.get(key)]
    if bad:
        return Difference(stage, item, "different", "metadata keys differ", bad)
    return Difference(stage, item, "equivalent", f"{len(keys)} metadata keys agree")


def compare_projects(
    left_root: Path,
    right_root: Path,
    *,
    epochs: set[str] | None = None,
    products: set[str] | None = None,
    stages: set[int] | None = None,
    rtol: float = 1e-10,
    atol: float = 1e-14,
) -> list[Difference]:
    """Compare stage 1, 2 and 3 products that exist on the left with their right counterparts.

    Either project may use the per-mode trees (``stage<N>/<mode>/...``, 2.1 on) or the
    flat pre-2.1 layout (the legacy scripts). A flat tree is compared with the other
    side's ``full-band`` tree; per-mode trees are compared mode by mode.
    """
    left, right = Layout.at(left_root), Layout.at(right_root)
    diffs: list[Difference] = []
    stages = stages or {1, 2, 3}

    def wanted(epoch: str) -> bool:
        return not epochs or epoch in epochs

    for stage1_left, stage1_right in _paired_trees(left, right, 1):
        if 1 not in stages or not stage1_left.is_dir():
            continue
        for epoch_dir in sorted(p for p in stage1_left.iterdir() if p.is_dir()):
            prefix = epoch_dir.name
            epoch = prefix.rsplit(".", 1)[-1]
            if not wanted(epoch):
                continue
            other = stage1_right / prefix
            diffs.append(_compare_files(1, f"{prefix}.gmod", epoch_dir / f"{prefix}.gmod", other / f"{prefix}.gmod", True))
            diffs.append(_compare_files(1, f"{prefix}.cal.uvf", epoch_dir / f"{prefix}.cal.uvf", other / f"{prefix}.cal.uvf", False))
            diffs.append(
                _compare_json_subset(
                    1, f"{prefix}.model.json", epoch_dir / f"{prefix}.model.json",
                    other / f"{prefix}.model.json", ("groups", "components", "n_groups", "n_components"),
                )
            )
            left_meta = epoch_dir / f"{prefix}.stage1.metadata.json"
            right_meta = other / f"{prefix}.stage1.metadata.json"
            if left_meta.exists() and right_meta.exists():
                a = json.loads(left_meta.read_text()).get("final_residual_rms_jy_per_beam")
                b = json.loads(right_meta.read_text()).get("final_residual_rms_jy_per_beam")
                same = a == b or (a is not None and b is not None and np.isclose(a, b, rtol=1e-9))
                diffs.append(
                    Difference(1, f"{prefix} residual RMS", "equivalent" if same else "different", f"{a} vs {b}")
                )

    for stage2_left, stage2_right in _paired_trees(left, right, 2):
        if 2 not in stages or not stage2_left.is_dir():
            continue
        for csv in sorted(stage2_left.glob("*/*.spectrum.csv")):
            prefix = csv.parent.name
            epoch = prefix.rsplit(".", 1)[-1]
            product = csv.name[len(prefix) + 1 : -len(".spectrum.csv")]
            if not wanted(epoch) or (products and product not in products):
                continue
            other_dir = stage2_right / prefix
            diffs.append(_compare_csv(2, csv.name, csv, other_dir / csv.name, rtol, atol))
            meta = f"{prefix}.{product}.stage2.metadata.json"
            diffs.append(_compare_json_subset(2, meta, csv.parent / meta, other_dir / meta, STAGE2_META_KEYS))

    for stage3_left, stage3_right in _paired_trees(left, right, 3):
        if 3 not in stages or not stage3_left.is_dir():
            continue
        for csv in sorted(stage3_left.rglob("*.csv")):
            relative = csv.relative_to(stage3_left)
            parts = relative.parts
            if parts[0] != "combined":
                epoch = parts[0].rsplit(".", 1)[-1]
                if not wanted(epoch):
                    continue
                if products and len(parts) > 1 and parts[1] not in products:
                    continue
            elif products and len(parts) > 2 and parts[2] not in products:
                continue
            diffs.append(_compare_csv(3, str(relative), csv, stage3_right / relative, rtol, atol))
    return diffs


def _paired_trees(left: Layout, right: Layout, stage: int) -> list[tuple[Path, Path]]:
    """(left tree, right tree) pairs for one stage, one per calibration mode on the left.

    A side without any ``<mode>`` directory is the flat legacy layout and stands
    for ``full-band``.
    """
    def tree(layout: Layout, mode: str) -> Path:
        return layout.stage_dir(stage, mode) if layout.modes_present(stage) else layout.root / f"stage{stage}"

    left_modes = left.modes_present(stage) or ["full-band"]
    return [(tree(left, mode), tree(right, mode)) for mode in left_modes]
