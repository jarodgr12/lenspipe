"""Small cached previews of product figures for the Results page.

Stage 3 writes 300 dpi PNGs of a megabyte or more each. A gallery of twenty of
them is far too heavy to push through the console, so each figure is shown as
a cached thumbnail (a few tens of kilobytes) that links to the full image.
Thumbnails live under ``<project>/.cache/thumbnails/`` keyed by the source
file's path, size and mtime, so a regenerated figure gets a new thumbnail.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["THUMBNAIL_WIDTH", "thumbnail_for"]

THUMBNAIL_WIDTH = 480
CACHE_DIRNAME = ".cache/thumbnails"


def thumbnail_for(root: Path, image: Path, width: int = THUMBNAIL_WIDTH) -> Path:
    """Return the cached thumbnail path for ``image``, creating it if needed.

    Falls back to the original image when Pillow cannot read it.
    """
    try:
        stat = os.stat(image)
    except OSError:
        return image
    key = hashlib.sha1(f"{image}|{stat.st_size}|{stat.st_mtime_ns}|{width}".encode()).hexdigest()
    cache_dir = root / CACHE_DIRNAME
    target = cache_dir / f"{key}.png"
    if target.is_file():
        return target
    try:
        from PIL import Image

        cache_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(image) as source:
            source.thumbnail((width, width * 4))  # keep aspect; height rarely matters
            source.convert("RGB").save(target, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001 - a failed preview must not break the page
        return image
    return target
