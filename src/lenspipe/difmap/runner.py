"""Run DifMAP as a subprocess with a command script on stdin and a streamed log."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DifmapNotFound", "DifmapResult", "difmap_version", "resolve_executable", "run_difmap"]

# Real banner: "Caltech difference mapping program - version 2.5q (3 Dec 2022)".
_VERSION_PATTERN = re.compile(
    r"(?:difference mapping program|difmap)[^\n]*?\bversion\s+v?([0-9][A-Za-z0-9._-]*)"
    r"|difmap\s+v?([0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)
_DIFMAP_SIGNS = ("difference mapping program", "Quitting program", "Exiting program", "Started logfile")


class DifmapNotFound(FileNotFoundError):
    pass


@dataclass
class DifmapResult:
    returncode: int
    log_text: str
    log_path: Path
    duration_s: float
    cancelled: bool = False
    peak_rss_bytes: int | None = None  # largest resident set of any child so far (see below)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.cancelled


def children_peak_rss_bytes() -> int | None:
    """Peak resident memory of the largest child process this process has reaped.

    The OS keeps this as the maximum over all terminated children, so after a
    DifMAP run it is the footprint of the biggest DifMAP so far. It is what the
    shard sizing needs; per-shard attribution is not required.
    """
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (ImportError, OSError, ValueError):
        return None
    if peak <= 0:
        return None
    # Linux reports kibibytes, macOS bytes.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def resolve_executable(executable: str) -> str:
    """Return a runnable path for ``executable`` or raise ``DifmapNotFound``."""
    found = shutil.which(executable)
    if found:
        return found
    explicit = Path(executable).expanduser()
    if explicit.is_file() and os.access(explicit, os.X_OK):
        return str(explicit)
    raise DifmapNotFound(f"DifMAP executable not found: {executable}")


def difmap_version(executable: str, timeout_s: float = 30.0) -> dict[str, str | bool | None]:
    """Probe DifMAP for its version; never raises.

    DifMAP may exit on ``quit`` without flushing its buffered stdout, so the
    banner is also read from the ``difmap.log_N`` file it writes in its working
    directory. The probe runs in a temporary directory so that file does not
    litter the caller's cwd. ``is_difmap`` is True when the output carries
    DifMAP's own messages even if no version could be parsed.
    """
    import tempfile

    try:
        path = resolve_executable(executable)
    except DifmapNotFound:
        return {"executable": executable, "version": None, "banner": None, "is_difmap": False}
    text = ""
    with tempfile.TemporaryDirectory(prefix="lenspipe-difmap-probe-") as scratch:
        try:
            completed = subprocess.run(
                [path],
                input="quit\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                check=False,
                cwd=scratch,
            )
            text = completed.stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return {"executable": executable, "version": None, "banner": None, "is_difmap": False}
        for own_log in sorted(Path(scratch).glob("difmap.log*")):
            try:
                text = own_log.read_text(errors="replace")[:4000] + "\n" + text
            except OSError:
                pass
    banner = next(
        (line.strip() for line in text.splitlines() if line.strip() and not line.startswith("!")), None
    )
    match = _VERSION_PATTERN.search(text[:8000])
    version = (match.group(1) or match.group(2)) if match else None
    return {
        "executable": executable,
        "version": version,
        "banner": banner,
        "is_difmap": bool(version) or any(sign in text for sign in _DIFMAP_SIGNS),
    }


def run_difmap(
    executable: str,
    commands: str,
    log_path: Path,
    *,
    on_line: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    stream: str = "pipe",
    cwd: Path | None = None,
    nice: int | None = None,
) -> DifmapResult:
    """Run DifMAP with ``commands`` on stdin, writing the log incrementally.

    The full log is also returned. ``on_line`` receives each output line as it
    arrives (without its newline). Setting ``cancel_event`` terminates the whole
    process group. With ``stream="pty"`` DifMAP sees a terminal on stdout and
    line-buffers, which makes per-fit progress arrive promptly. ``nice`` sets
    DifMAP's scheduling priority (0 normal to 19 lowest) so a saturating fit
    does not starve the console.
    """
    path = resolve_executable(executable)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    cancelled = False
    preexec = _priority_setter(nice)

    # stdout and stderr are captured on separate pipes. DifMAP block-buffers stdout when
    # piped but writes warnings to stderr unbuffered; merging them in one pipe lets a
    # warning land in the middle of a stdout line (seen splitting the model-fit table
    # rule in real logs). Separate pipes keep every line intact; stderr lines are
    # written to the log tagged "! [stderr]".
    if stream == "pty":
        popen_kwargs, reader_factory = _pty_setup()
    else:
        popen_kwargs, reader_factory = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}, None

    process = subprocess.Popen(
        [path],
        stdin=subprocess.PIPE,
        text=False,
        cwd=str(cwd) if cwd else None,
        start_new_session=True,
        preexec_fn=preexec,
        **popen_kwargs,
    )

    def feed_stdin() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write(commands.encode("utf-8"))
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    feeder = threading.Thread(target=feed_stdin, name="difmap-stdin", daemon=True)
    feeder.start()

    def watch_cancel() -> None:
        nonlocal cancelled
        if cancel_event is None:
            return
        while process.poll() is None:
            if cancel_event.wait(0.25):
                cancelled = True
                _terminate_group(process)
                return

    watcher = threading.Thread(target=watch_cancel, name="difmap-cancel", daemon=True)
    watcher.start()

    lines: list[str] = []
    with log_path.open("w", encoding="utf-8") as log_handle:
        reader = reader_factory(process) if reader_factory else _pipe_reader(process)
        for line in reader:
            lines.append(line)
            log_handle.write(line + "\n")
            log_handle.flush()
            if on_line is not None:
                on_line(line)

    returncode = process.wait()
    feeder.join(timeout=1.0)
    return DifmapResult(
        returncode=returncode,
        log_text="\n".join(lines) + ("\n" if lines else ""),
        log_path=log_path,
        duration_s=time.monotonic() - start,
        cancelled=cancelled,
        peak_rss_bytes=children_peak_rss_bytes(),
    )


def _priority_setter(nice: int | None):
    """A preexec hook that gives the child an absolute niceness, or None to inherit."""
    if nice is None or not hasattr(os, "setpriority"):
        return None
    level = max(0, min(19, int(nice)))

    def apply() -> None:
        try:
            os.setpriority(os.PRIO_PROCESS, 0, level)
        except OSError:
            pass

    return apply


def _terminate_group(process: subprocess.Popen, grace_s: float = 5.0) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


STDERR_PREFIX = "! [stderr] "


def _split_lines(handle):
    """Yield complete lines from a binary handle without waiting for EOF."""
    buffer = b""
    read = handle.read1 if hasattr(handle, "read1") else handle.read
    for chunk in iter(lambda: read(65536), b""):
        buffer += chunk
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw, buffer = buffer[:newline], buffer[newline + 1 :]
            yield raw.rstrip(b"\r").decode("utf-8", errors="replace")
    if buffer:
        yield buffer.rstrip(b"\r").decode("utf-8", errors="replace")


def _pipe_reader(process: subprocess.Popen):
    """Merge stdout and stderr line by line; each stream's lines stay intact."""
    import queue

    assert process.stdout is not None
    channel: queue.Queue = queue.Queue()
    sentinel = object()

    def pump(handle, prefix: str) -> None:
        try:
            for line in _split_lines(handle):
                channel.put(prefix + line)
        finally:
            channel.put(sentinel)

    readers = [threading.Thread(target=pump, args=(process.stdout, ""), daemon=True)]
    if process.stderr is not None:
        readers.append(threading.Thread(target=pump, args=(process.stderr, STDERR_PREFIX), daemon=True))
    for reader in readers:
        reader.start()
    open_streams = len(readers)
    while open_streams:
        item = channel.get()
        if item is sentinel:
            open_streams -= 1
            continue
        yield item


def _pty_setup():
    import pty

    master_fd, slave_fd = pty.openpty()

    def reader_factory(process: subprocess.Popen):
        os.close(slave_fd)
        buffer = b""
        try:
            while True:
                try:
                    chunk = os.read(master_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw, buffer = buffer[:newline], buffer[newline + 1 :]
                    yield raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if buffer:
                yield buffer.rstrip(b"\r").decode("utf-8", errors="replace")
        finally:
            os.close(master_fd)

    return {"stdout": slave_fd, "stderr": slave_fd}, reader_factory
