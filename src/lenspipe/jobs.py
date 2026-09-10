"""Job records and a small local job runner.

A job is a directory ``<project>/.jobs/<id>/`` holding ``job.json`` (what was
asked and its status), ``log.txt`` (everything the stage printed),
``progress.json`` (written by the stage itself via :class:`Reporter`) and
``result.json`` (written by the CLI on exit). Jobs are ordinary subprocesses
of ``python -m lenspipe ...`` so anything the console does is reproducible
from a shell, and a console restart never loses running work.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lenspipe.progress import JOB_DIR_ENV
from lenspipe.project import Layout

__all__ = ["JobManager", "JobRecord", "job_result_path", "write_job_result"]

TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def job_result_path(job_dir: Path) -> Path:
    return job_dir / "result.json"


def write_job_result(job_dir: Path, returncode: int, error: str | None = None) -> None:
    """Called by the CLI when it runs inside a job directory."""
    try:
        job_result_path(job_dir).write_text(
            json.dumps({"returncode": returncode, "error": error, "finished_utc": _now()}),
            encoding="utf-8",
        )
    except OSError:
        pass


@dataclass
class JobRecord:
    id: str
    title: str
    stage: str
    argv: list[str]
    project_root: str
    created_utc: str
    status: str = "queued"
    started_utc: str | None = None
    finished_utc: str | None = None
    returncode: int | None = None
    pid: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return Layout.at(self.project_root).jobs / self.id

    @property
    def log_path(self) -> Path:
        return self.directory / "log.txt"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def command_line(self) -> str:
        return "lenspipe " + " ".join(_quote(arg) for arg in self.argv)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JobRecord:
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})

    @classmethod
    def load(cls, directory: Path) -> JobRecord | None:
        path = directory / "job.json"
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.directory / "job.json.tmp"
        tmp.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        os.replace(tmp, self.directory / "job.json")

    def progress(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads((self.directory / "progress.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def tail(self, lines: int = 200) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                chunk = min(size, max(4096, lines * 200))
                handle.seek(size - chunk)
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])


def _quote(argument: str) -> str:
    if not argument or any(ch.isspace() for ch in argument) or '"' in argument:
        return json.dumps(argument)
    return argument


def _signal_group(pid: int, signum: int) -> None:
    """Signal a job's process group; a group that has already gone is not an error."""
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobManager:
    """Submit, watch and cancel jobs for one project. Safe to use from several threads."""

    def __init__(self, project_root: Path | str, max_concurrent: int = 1) -> None:
        self.layout = Layout.at(project_root)
        self.max_concurrent = max(1, max_concurrent)
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()
        self.layout.jobs.mkdir(parents=True, exist_ok=True)

    # -- creation -----------------------------------------------------------

    def submit(self, argv: list[str], *, title: str, stage: str, **extra: Any) -> JobRecord:
        with self._lock:
            job_id = self._new_id(stage)
            record = JobRecord(
                id=job_id,
                title=title,
                stage=stage,
                argv=list(argv),
                project_root=str(self.layout.root),
                created_utc=_now(),
                extra=dict(extra),
            )
            record.save()
            self.pump()
            return self.get(job_id) or record

    def _new_id(self, stage: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        base = f"{stamp}-{stage}"
        candidate = base
        counter = 1
        while (self.layout.jobs / candidate).exists():
            counter += 1
            candidate = f"{base}-{counter}"
        return candidate

    # -- lifecycle ----------------------------------------------------------

    def pump(self) -> None:
        """Reconcile running jobs and start queued ones up to the concurrency cap."""
        with self._lock:
            records = self.list()
            running = [r for r in records if r.status == "running"]
            for record in running:
                self._reconcile(record)
            running_count = sum(1 for r in self.list() if r.status == "running")
            for record in sorted((r for r in records if r.status == "queued"), key=lambda r: r.created_utc):
                if running_count >= self.max_concurrent:
                    break
                self._start(record)
                running_count += 1

    def _start(self, record: JobRecord) -> None:
        record.directory.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env[JOB_DIR_ENV] = str(record.directory)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("MPLBACKEND", "Agg")
        log_handle = record.log_path.open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "lenspipe", *record.argv],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(self.layout.root),
                env=env,
                start_new_session=True,
                preexec_fn=self._priority_setter(),
            )
        except OSError as exc:
            log_handle.close()
            record.status = "failed"
            record.error = f"could not start: {exc}"
            record.finished_utc = _now()
            record.save()
            return
        finally:
            pass
        log_handle.close()
        self._processes[record.id] = process
        record.status = "running"
        record.pid = process.pid
        record.started_utc = _now()
        record.save()

    def _priority_setter(self):
        """Run jobs at the project's configured niceness so the console stays responsive."""
        try:
            from lenspipe.config import load_config

            level = load_config(self.layout.root).run.nice
        except Exception:  # noqa: BLE001 - a broken config must not stop jobs from starting
            level = 10
        if not hasattr(os, "setpriority"):
            return None

        def apply() -> None:
            try:
                os.setpriority(os.PRIO_PROCESS, 0, max(0, min(19, int(level))))
            except OSError:
                pass

        return apply

    def _reconcile(self, record: JobRecord) -> None:
        process = self._processes.get(record.id)
        returncode: int | None = None
        if process is not None:
            returncode = process.poll()
            if returncode is None:
                return
        else:
            result = self._read_result(record)
            if result is not None:
                returncode = int(result.get("returncode", 1))
            elif _pid_alive(record.pid):
                return
            else:
                returncode = -1
                record.error = "process ended without writing a result (console restarted?)"
        record.returncode = returncode
        record.finished_utc = _now()
        if record.status != "cancelled":
            record.status = "completed" if returncode == 0 else "failed"
            if returncode not in (0, None) and record.error is None:
                result = self._read_result(record)
                record.error = (result or {}).get("error") or f"exit status {returncode}"
        record.save()
        self._processes.pop(record.id, None)

    @staticmethod
    def _read_result(record: JobRecord) -> dict[str, Any] | None:
        try:
            return json.loads(job_result_path(record.directory).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def cancel(self, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self.get(job_id)
            if record is None or record.is_terminal:
                return record
            if record.status == "queued":
                record.status = "cancelled"
                record.finished_utc = _now()
                record.save()
                return record
            record.status = "cancelled"
            record.save()
            pid = record.pid
            process = self._processes.get(job_id)
            if pid:
                _signal_group(pid, signal.SIGTERM)
                if process is not None:
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        _signal_group(pid, signal.SIGKILL)
                        try:
                            process.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            pass
                else:
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and _pid_alive(pid):
                        time.sleep(0.1)
                    if _pid_alive(pid):
                        _signal_group(pid, signal.SIGKILL)
            record.finished_utc = _now()
            record.returncode = -15
            record.save()
            self._processes.pop(job_id, None)
            self.pump()
            return record

    # -- queries ------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord | None:
        return JobRecord.load(self.layout.jobs / job_id)

    def list(self, limit: int | None = None) -> list[JobRecord]:
        records: list[JobRecord] = []
        if not self.layout.jobs.is_dir():
            return records
        for directory in sorted(self.layout.jobs.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            record = JobRecord.load(directory)
            if record is not None:
                records.append(record)
        return records[:limit] if limit else records

    def wait(self, job_id: str, timeout_s: float | None = None, poll_s: float = 0.25) -> JobRecord:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            self.pump()
            record = self.get(job_id)
            if record is None or record.is_terminal:
                if record is None:
                    raise KeyError(job_id)
                return record
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(f"job {job_id} still {record.status}")
            time.sleep(poll_s)
