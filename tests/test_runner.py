"""The DifMAP runner streams output, records the log, and honours cancellation."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lenspipe.difmap.runner import DifmapNotFound, difmap_version, run_difmap


def test_streams_lines_and_writes_log(fake_difmap: Path, tmp_path: Path) -> None:
    seen: list[str] = []
    result = run_difmap(
        str(fake_difmap),
        'mapsize 256\nprint "HELLO", 1, imstat(rms)\nquit\n',
        tmp_path / "run.log",
        on_line=seen.append,
    )
    assert result.ok and result.returncode == 0
    assert "HELLO 1 0" in result.log_text
    assert seen == result.log_text.splitlines()
    assert (tmp_path / "run.log").read_text() == result.log_text
    assert result.duration_s >= 0


def test_large_command_stream_does_not_deadlock(fake_difmap: Path, tmp_path: Path) -> None:
    commands = "".join(f"mapsize {i}\n" for i in range(20000)) + "quit\n"  # > pipe buffer
    result = run_difmap(str(fake_difmap), commands, tmp_path / "big.log")
    assert result.ok
    assert result.log_text.count("Map grid") == 20000


def test_nonzero_exit_is_reported(fake_difmap: Path, tmp_path: Path) -> None:
    result = run_difmap(str(fake_difmap), "fail\n", tmp_path / "fail.log")
    assert not result.ok and result.returncode == 3


def test_cancel_terminates_process(fake_difmap: Path, tmp_path: Path) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    start = time.monotonic()
    result = run_difmap(str(fake_difmap), "sleep 30\nquit\n", tmp_path / "c.log", cancel_event=cancel)
    assert result.cancelled and not result.ok
    assert time.monotonic() - start < 15


def test_stderr_never_splits_a_stdout_line(tmp_path: Path) -> None:
    """A block-buffered stdout line interleaved with an unbuffered stderr warning."""
    script = tmp_path / "chatty"
    script.write_text(
        "#!/bin/sh\n"
        "cat >/dev/null\n"                         # swallow the command script
        "printf '%s' '#---'\n"                     # start of a line, no newline yet
        "printf '%s\\n' 'Warning: pixels' 1>&2\n"  # stderr arrives mid-line
        "sleep 0.2\n"
        "printf '%s\\n' '----- rest of rule'\n"
        "printf '%s\\n' 'STAGE2_RMS 1 0.5'\n"
    )
    script.chmod(0o755)
    result = run_difmap(str(script), "quit\n", tmp_path / "chatty.log")
    lines = result.log_text.splitlines()
    assert "#-------- rest of rule" in lines
    assert "! [stderr] Warning: pixels" in lines
    assert "STAGE2_RMS 1 0.5" in lines
    assert not any("#---Warning" in line for line in lines)


def test_nice_lowers_difmap_priority(fake_difmap: Path, tmp_path: Path) -> None:
    import os

    baseline = os.getpriority(os.PRIO_PROCESS, 0)
    inherited = run_difmap(str(fake_difmap), "nice\nquit\n", tmp_path / "n0.log")
    assert f"NICE {baseline}" in inherited.log_text
    lowered = run_difmap(str(fake_difmap), "nice\nquit\n", tmp_path / "n1.log", nice=15)
    assert "NICE 15" in lowered.log_text
    clamped = run_difmap(str(fake_difmap), "nice\nquit\n", tmp_path / "n2.log", nice=99)
    assert "NICE 19" in clamped.log_text


def test_missing_executable() -> None:
    with pytest.raises(DifmapNotFound):
        run_difmap("definitely-not-difmap-xyz", "quit\n", Path("/tmp/never.log"))
    assert difmap_version("definitely-not-difmap-xyz")["version"] is None


def test_version_probe(fake_difmap: Path) -> None:
    info = difmap_version(str(fake_difmap))
    assert info["version"] == "2.5k"
    assert "fake-difmap" in (info["banner"] or "")


@pytest.mark.skipif(not hasattr(__import__("os"), "openpty"), reason="no pty")
def test_pty_stream_mode(fake_difmap: Path, tmp_path: Path) -> None:
    result = run_difmap(str(fake_difmap), 'print "PTY", 2, imstat(rms)\nquit\n', tmp_path / "p.log", stream="pty")
    assert result.ok and "PTY 2 0" in result.log_text
