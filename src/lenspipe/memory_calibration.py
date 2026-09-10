"""Per-machine record of how much memory DifMAP really uses per byte of input.

After every Stage 2 run the largest DifMAP footprint is divided by the input
size and, when the input was large enough to be meaningful, stored in
``~/.lenspipe/difmap_memory.json``. ``stage2.memory_multiple = "auto"`` then
sizes shards from that measurement (with a safety margin) instead of the
conservative built-in guess, so the first run on a machine is cautious and
every later run uses real numbers.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "DEFAULT_MULTIPLE",
    "MIN_INPUT_BYTES",
    "SAFETY_FACTOR",
    "MemoryCalibration",
    "effective_multiple",
    "load_calibration",
    "record_measurement",
]

DEFAULT_MULTIPLE = 3.0  # used until a measurement exists
SAFETY_FACTOR = 1.25  # headroom on top of the measured ratio
MIN_INPUT_BYTES = 200 * 1024 * 1024  # smaller inputs say nothing about DifMAP's scaling


def _store_path() -> Path:
    home = Path(os.environ.get("LENSPIPE_HOME", Path.home() / ".lenspipe"))
    return home / "difmap_memory.json"


@dataclass
class MemoryCalibration:
    multiple: float  # peak RSS / input bytes, largest ratio observed
    peak_rss_bytes: int
    input_bytes: int
    host: str
    measured_utc: str
    samples: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def load_calibration() -> MemoryCalibration | None:
    try:
        payload = json.loads(_store_path().read_text(encoding="utf-8"))
        return MemoryCalibration(**{k: payload[k] for k in ("multiple", "peak_rss_bytes", "input_bytes", "host", "measured_utc")},
                                 samples=int(payload.get("samples", 1)))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def record_measurement(peak_rss_bytes: int | None, input_bytes: int) -> MemoryCalibration | None:
    """Store the observed ratio if it is meaningful; keeps the largest ratio seen."""
    if not peak_rss_bytes or peak_rss_bytes <= 0 or input_bytes < MIN_INPUT_BYTES:
        return None
    ratio = peak_rss_bytes / input_bytes
    previous = load_calibration()
    if previous is not None and previous.multiple >= ratio:
        previous.samples += 1
        previous.measured_utc = datetime.now(UTC).isoformat(timespec="seconds")
        updated = previous
    else:
        updated = MemoryCalibration(
            multiple=round(ratio, 3),
            peak_rss_bytes=int(peak_rss_bytes),
            input_bytes=int(input_bytes),
            host=socket.gethostname(),
            measured_utc=datetime.now(UTC).isoformat(timespec="seconds"),
            samples=(previous.samples + 1) if previous else 1,
        )
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated.to_dict(), indent=1), encoding="utf-8")
    except OSError:
        pass
    return updated


def effective_multiple(configured: float | str) -> tuple[float, str]:
    """Resolve the configured multiple to a number and say where it came from."""
    if configured != "auto":
        return float(configured), "configured"
    calibration = load_calibration()
    if calibration is None:
        return DEFAULT_MULTIPLE, "default (no measurement yet)"
    return round(calibration.multiple * SAFETY_FACTOR, 3), (
        f"measured {calibration.multiple:g} x {SAFETY_FACTOR:g} safety "
        f"({calibration.samples} run(s), {calibration.measured_utc[:10]})"
    )
