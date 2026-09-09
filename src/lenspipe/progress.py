"""Progress and log reporting shared by the CLI and the job runner."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

__all__ = ["JOB_DIR_ENV", "Reporter", "default_reporter", "get_logger"]

JOB_DIR_ENV = "LENSPIPE_JOB_DIR"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class Reporter:
    """Receives log lines and progress; writes ``progress.json`` when run as a job.

    Thread-safe. ``progress`` is keyed by a label so concurrent epochs and shards
    can report independently; the file holds the latest state of every label.
    """

    def __init__(self, job_dir: Path | None = None, stream=None) -> None:
        self.job_dir = job_dir
        self.stream = stream if stream is not None else sys.stdout
        self._lock = threading.Lock()
        self._progress: dict[str, dict[str, Any]] = {}

    def log(self, message: str, level: int = logging.INFO) -> None:
        with self._lock:
            print(message, file=self.stream, flush=True)

    def warn(self, message: str) -> None:
        self.log(f"WARNING: {message}", logging.WARNING)

    def error(self, message: str) -> None:
        with self._lock:
            print(f"ERROR: {message}", file=sys.stderr, flush=True)

    def progress(self, label: str, done: int, total: int, detail: str | None = None) -> None:
        with self._lock:
            self._progress[label] = {"done": int(done), "total": int(total), "detail": detail}
            self._flush_locked()

    def clear(self, label: str) -> None:
        with self._lock:
            self._progress.pop(label, None)
            self._flush_locked()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._progress.items()}

    def _flush_locked(self) -> None:
        if self.job_dir is None:
            return
        try:
            target = self.job_dir / "progress.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._progress, indent=1), encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            pass


def default_reporter() -> Reporter:
    """A reporter bound to the job directory named by the environment, if any."""
    job_dir = os.environ.get(JOB_DIR_ENV)
    return Reporter(Path(job_dir) if job_dir else None)
