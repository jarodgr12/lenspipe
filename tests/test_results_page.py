"""Results-page helpers: cheap freshness checks, thumbnails, and lazy table reads."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from lenspipe import provenance
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.project import inventory
from lenspipe.stage1 import run_stage1
from lenspipe.ui.thumbnails import THUMBNAIL_WIDTH, thumbnail_for
from tests.conftest import write_synthetic_uvfits


def test_inventory_never_hashes_inputs(project: Path, fake_difmap: Path, monkeypatch) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    run_stage1(project, cfg, reporter=quiet, workers=1)
    # Replace an input: size/mtime change. The listing must notice without reading the file.
    write_synthetic_uvfits(project / "inputs" / "MG0414.A.uvfits", seed=5)

    def forbidden(path: Path, chunk_size: int = 0) -> str:
        raise AssertionError(f"inventory hashed {path}")

    monkeypatch.setattr(provenance, "file_sha256", forbidden)
    summary = inventory(project)
    row_a = next(r for r in summary["epochs"] if r["epoch"] == "A")
    assert row_a["stage1"]["status"] == "stale"
    assert any("size or mtime changed" in reason for reason in row_a["stage1"]["reasons"])
    # The explicit verify still confirms by hash (and would raise here because we forbade it).
    with pytest.raises(AssertionError, match="hashed"):
        provenance.verify_project(project)


def test_thumbnail_is_small_cached_and_tracks_source_changes(tmp_path: Path) -> None:
    from PIL import Image

    root = tmp_path / "proj"
    root.mkdir()
    big = root / "figure.png"
    Image.new("RGB", (2400, 2400), (200, 30, 30)).save(big)
    thumb = thumbnail_for(root, big)
    assert thumb != big and thumb.is_file()
    with Image.open(thumb) as image:
        assert image.width == THUMBNAIL_WIDTH and image.height == THUMBNAIL_WIDTH
    assert thumb.stat().st_size < big.stat().st_size / 10
    assert thumbnail_for(root, big) == thumb  # cached
    Image.new("RGB", (2400, 1200), (30, 200, 30)).save(big)
    renewed = thumbnail_for(root, big)
    assert renewed != thumb  # source changed: new cache entry
    with Image.open(renewed) as image:
        assert image.height == THUMBNAIL_WIDTH // 2


def test_thumbnail_falls_back_to_original_for_unreadable_files(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    bogus = root / "not-an-image.png"
    bogus.write_text("hello")
    assert thumbnail_for(root, bogus) == bogus
    assert thumbnail_for(root, root / "missing.png") == root / "missing.png"


def test_results_helpers_lazy_table_head(tmp_path: Path) -> None:
    import pandas as pd

    from lenspipe.ui.pages_results import MAX_ROWS, _read_table

    csv = tmp_path / "big.csv"
    pd.DataFrame({"a": range(1000), "b": range(1000)}).to_csv(csv, index=False)
    head, total = _read_table(csv)
    assert total == 1000 and len(head) == MAX_ROWS
