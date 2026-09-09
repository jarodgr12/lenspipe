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
