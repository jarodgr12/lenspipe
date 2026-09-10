"""`lenspipe stop` finds and ends consoles, registered or merely running."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe import console_registry
from lenspipe.cli import app

runner = CliRunner()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "consoles"
    monkeypatch.setattr(console_registry, "REGISTRY_DIR", directory)
    return directory


def _fake_console(root: Path, port: int) -> subprocess.Popen:
    """A long-lived process whose command line looks like `lenspipe ui <root> --port N`."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "lenspipe", "ui", str(root), "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    time.sleep(0.3)
    return process


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _ps(pid: int) -> str:
    """What the registry sees for the fake console; shown when a lookup assertion fails."""
    return console_registry._command_line(pid) or "<no command line>"


def test_registered_console_is_found_and_stopped(registry: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    process = _fake_console(root, 8912)
    try:
        console_registry.register("127.0.0.1", 8912, root, pid=process.pid)
        found = console_registry.find_consoles(root=root)
        assert [c.pid for c in found] == [process.pid], _ps(process.pid)
        assert found[0].source == "registry"
        assert console_registry.find_consoles(port=1) == []
        assert console_registry.stop_console(found[0])
        process.wait(timeout=10)
        assert not _alive(process.pid)
        assert not list(registry.glob("*.json"))
    finally:
        if process.poll() is None:
            process.kill()


def test_unregistered_console_is_found_by_process_scan(registry: Path, tmp_path: Path) -> None:
    root = tmp_path / "old-version-project"
    root.mkdir()
    process = _fake_console(root, 8913)
    try:
        found = [c for c in console_registry.find_consoles(port=8913)]
        assert [c.pid for c in found] == [process.pid], _ps(process.pid)
        assert found[0].source == "scan" and found[0].root == str(root.resolve())
        assert console_registry.stop_console(found[0])
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()


def test_stale_registry_entries_are_ignored_and_removed(registry: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    process = _fake_console(root, 8914)
    console_registry.register("127.0.0.1", 8914, root, pid=process.pid)
    process.kill()
    process.wait(timeout=10)
    assert console_registry.find_consoles(port=8914) == []
    assert not list(registry.glob("*.json"))


def test_stop_command_end_to_end(registry: Path, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / ".jobs").mkdir(parents=True)
    process = _fake_console(root, 8915)
    try:
        console_registry.register("127.0.0.1", 8915, root, pid=process.pid)
        listing = runner.invoke(app, ["stop", str(root), "--list"])
        assert listing.exit_code == 0 and str(process.pid) in listing.output, (listing.output, _ps(process.pid))
        assert _alive(process.pid)

        result = runner.invoke(app, ["stop", str(root)])
        assert result.exit_code == 0, result.output
        assert f"stopped pid {process.pid}" in result.output
        process.wait(timeout=10)

        nothing = runner.invoke(app, ["stop", str(root)])
        assert nothing.exit_code == 3 and "No running console" in nothing.output
        nothing_at_all = runner.invoke(app, ["stop"])
        assert nothing_at_all.exit_code in (0, 3)
    finally:
        if process.poll() is None:
            process.kill()


def test_ui_refuses_second_console_on_same_port(registry: Path, tmp_path: Path) -> None:
    import socket

    root = tmp_path / "proj"
    root.mkdir()
    process = _fake_console(root, 8916)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 8916))
    listener.listen(1)
    try:
        console_registry.register("127.0.0.1", 8916, root, pid=process.pid)
        result = runner.invoke(app, ["ui", str(root), "--port", "8916", "--no-browser"])
        assert result.exit_code == 1
        assert "already listening on port 8916" in result.output and "lenspipe stop" in result.output, (
            result.output, _ps(process.pid)
        )
    finally:
        listener.close()
        process.kill()


def test_ui_reports_port_taken_by_something_else(tmp_path: Path, registry: Path) -> None:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 8917))
    listener.listen(1)
    try:
        result = runner.invoke(app, ["ui", str(tmp_path), "--port", "8917", "--no-browser"])
        assert result.exit_code == 1 and "another program" in result.output
    finally:
        listener.close()


@pytest.mark.parametrize(
    "command, expected",
    [
        ("/venv/bin/python3.12 /venv/bin/lenspipe ui /data/MG0414 --port 8080", True),
        ("lenspipe ui --port 8080", True),
        ("/usr/bin/python3 -m lenspipe ui /data/x", True),
        ("bash -c 'lenspipe ui /data/x --port 8931'", False),
        ("zsh -c uv run lenspipe ui /data/x", False),
        ("uv run lenspipe ui /data/x", False),
        ("/venv/bin/python3 /venv/bin/lenspipe stage2 /data/x", False),
        ("grep lenspipe ui", False),
    ],
)
def test_process_scan_only_matches_the_console_itself(command: str, expected: bool) -> None:
    assert console_registry.looks_like_console_command(command) is expected
