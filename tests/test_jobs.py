"""Job records and the local job runner."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from lenspipe.jobs import JobManager


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_submit_runs_cli_and_records_result(project: Path) -> None:
    manager = JobManager(project)
    record = manager.submit(["inventory", str(project), "--json"], title="inventory", stage="inventory")
    assert record.status in {"queued", "running"}
    final = manager.wait(record.id, timeout_s=60)
    assert final.status == "completed", final.tail()
    assert final.returncode == 0
    assert '"epochs"' in final.tail()
    assert final.command_line.startswith("lenspipe inventory")
    assert (final.directory / "result.json").is_file()
    assert manager.list()[0].id == final.id


def test_failed_job_reports_error(project: Path) -> None:
    manager = JobManager(project)
    record = manager.submit(["stage3", str(project / "does-not-exist")], title="bad", stage="stage3")
    final = manager.wait(record.id, timeout_s=60)
    assert final.status == "failed"
    assert final.returncode not in (0, None)
    assert final.error


def test_queue_respects_concurrency_and_cancel(project: Path) -> None:
    manager = JobManager(project, max_concurrent=1)
    port = _free_port()
    blocker = manager.submit(
        ["ui", str(project), "--no-browser", "--port", str(port)], title="ui", stage="ui"
    )
    queued = manager.submit(["inventory", str(project)], title="inventory", stage="inventory")
    manager.pump()
    assert manager.get(blocker.id).status == "running"
    assert manager.get(queued.id).status == "queued"

    cancelled = manager.cancel(blocker.id)
    assert cancelled.status == "cancelled"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        manager.pump()
        if manager.get(queued.id).is_terminal:
            break
        time.sleep(0.2)
    assert manager.get(queued.id).status == "completed"
    assert manager.get(blocker.id).status == "cancelled"


def test_cancel_queued_job(project: Path) -> None:
    manager = JobManager(project, max_concurrent=1)
    port = _free_port()
    blocker = manager.submit(["ui", str(project), "--no-browser", "--port", str(port)], title="ui", stage="ui")
    queued = manager.submit(["inventory", str(project)], title="inventory", stage="inventory")
    assert manager.cancel(queued.id).status == "cancelled"
    manager.cancel(blocker.id)


@pytest.mark.parametrize("lines", [1, 5])
def test_tail_limits_lines(project: Path, lines: int) -> None:
    manager = JobManager(project)
    record = manager.submit(["describe", str(project)], title="describe", stage="describe")
    final = manager.wait(record.id, timeout_s=60)
    assert len(final.tail(lines).splitlines()) <= lines


def test_finished_jobs_are_read_from_disk_only_once(project: Path, monkeypatch) -> None:
    """Every console tick lists the jobs; finished ones must not cost a file read each time."""
    from lenspipe.jobs import JobManager, JobRecord

    manager = JobManager(project)
    done = manager.submit(["--version"], title="done", stage="other")
    manager.wait(done.id, timeout_s=60)
    assert manager.list()[0].status == "completed"
    loads: list[str] = []
    original = JobRecord.load

    def counting_load(directory):
        loads.append(directory.name)
        return original(directory)

    monkeypatch.setattr(JobRecord, "load", staticmethod(counting_load))
    manager.list()
    manager.list()
    assert done.id not in loads  # served from the cache
    queued = manager.submit(["--version"], title="again", stage="other")
    manager.wait(queued.id, timeout_s=60)
    assert queued.id in loads  # active jobs are re-read until they finish


def test_running_record_whose_pid_now_belongs_to_another_process_is_failed_not_stuck(project: Path, monkeypatch) -> None:
    """A job killed outside the console leaves status=running; if its pid is reused by an
    unrelated process the queue must not wait on it for ever."""
    from lenspipe.jobs import JobManager, JobRecord

    manager = JobManager(project)
    stale = JobRecord(
        id="20260101T000000-stage2", title="stale", stage="stage2", argv=["stage2"], project_root=str(project),
        created_utc="2026-01-01T00:00:00+00:00", status="running", pid=424242, started_utc="2026-01-01T00:00:01+00:00",
    )
    stale.save()
    monkeypatch.setattr("lenspipe.jobs._pid_alive", lambda pid: True)  # some process has that pid
    monkeypatch.setattr("lenspipe.console_registry._command_line", lambda pid: "/usr/bin/firefox --profile x")
    queued = manager.submit(["--version"], title="next", stage="other")
    assert manager.get(stale.id).status == "failed" and "killed" in manager.get(stale.id).error
    manager.wait(queued.id, timeout_s=60)
    assert manager.get(queued.id).status == "completed"
    assert manager.queue_status() is None

    # And a pid that really is a lenspipe job keeps the record running and the queue waiting.
    stale2 = JobRecord(
        id="20260101T000002-stage2", title="busy", stage="stage2", argv=["stage2"], project_root=str(project),
        created_utc="2026-01-01T00:00:02+00:00", status="running", pid=424243, started_utc="2026-01-01T00:00:03+00:00",
    )
    stale2.save()
    monkeypatch.setattr("lenspipe.console_registry._command_line", lambda pid: "python -m lenspipe stage2 /p")
    waiting = manager.submit(["--version"], title="held", stage="other")
    assert manager.get(stale2.id).status == "running" and manager.get(waiting.id).status == "queued"
    assert "running now: busy" in manager.queue_status() and "next: held" in manager.queue_status()
