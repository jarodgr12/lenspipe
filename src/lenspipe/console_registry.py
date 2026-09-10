"""Know which consoles are running so ``lenspipe stop`` can end them cleanly.

A console registers itself in ``~/.lenspipe/consoles/<pid>.json`` when it
starts and removes the record on a clean exit. Stale records (the process is
gone, or the pid now belongs to something else) are ignored and cleaned up.
Consoles started by versions without a registry are found by scanning the
process table for a ``lenspipe ui`` command line.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "ConsoleRecord",
    "find_consoles",
    "looks_like_console_command",
    "port_in_use",
    "register",
    "stop_console",
    "unregister",
]

REGISTRY_DIR = Path(os.environ.get("LENSPIPE_HOME", Path.home() / ".lenspipe")) / "consoles"


@dataclass
class ConsoleRecord:
    pid: int
    host: str
    port: int
    root: str
    started_utc: str
    source: str = "registry"  # registry | scan

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _record_path(pid: int) -> Path:
    return REGISTRY_DIR / f"{pid}.json"


def register(host: str, port: int, root: Path, pid: int | None = None) -> ConsoleRecord:
    record = ConsoleRecord(
        pid=pid or os.getpid(), host=host, port=int(port), root=str(root),
        started_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    _record_path(record.pid).write_text(json.dumps(record.to_dict(), indent=1), encoding="utf-8")
    return record


def unregister(pid: int | None = None) -> None:
    try:
        _record_path(pid or os.getpid()).unlink()
    except OSError:
        pass


def _command_line(pid: int) -> str | None:
    """Full command line of a process. ``ps`` truncates to the terminal width
    unless told otherwise, so /proc is preferred and ``-ww`` is used otherwise."""
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
    except OSError:
        raw = b""
    if raw:
        return " ".join(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part) or None
    try:
        output = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)], text=True, capture_output=True, check=False
        ).stdout.strip()
    except OSError:
        return None
    return output or None


_WRAPPERS = {
    "sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "nohup", "env", "timeout", "uv",
    "sudo", "script", "tmux", "screen", "ssh", "grep", "egrep", "pgrep", "tail", "less", "cat",
    "watch", "xargs", "time",
}


def looks_like_console_command(command: str) -> bool:
    """True only for the console process itself, never for a shell or wrapper that launched it.

    Accepted shapes: ``<python> <...>/lenspipe ui ...``, ``lenspipe ui ...`` and
    ``<python> -m lenspipe ui ...``. Nothing before the ``lenspipe ui`` pair may
    be a shell or wrapper name, so ``bash -c 'lenspipe ui ...'`` and ``uv run
    lenspipe ui`` are not matched (the python they start is). Interpreter names
    are deliberately not required: they vary between platforms and installs.
    """
    tokens = command.split()
    if len(tokens) < 2:
        return False
    pair_at: int | None = None
    for index, token in enumerate(tokens[:-1]):
        if Path(token).name == "lenspipe" and tokens[index + 1] == "ui":
            pair_at = index
            break
        if token == "-m" and tokens[index + 1] == "lenspipe" and index + 2 < len(tokens) and tokens[index + 2] == "ui":
            pair_at = index
            break
    if pair_at is None:
        return False
    # Paths may contain spaces (they split into several tokens), so inspect every
    # token before the pair rather than trusting the first one.
    prefix = [Path(token).name for token in tokens[:pair_at]]
    return not any(name in _WRAPPERS for name in prefix)


def _is_console_process(pid: int) -> bool:
    command = _command_line(pid)
    return bool(command) and looks_like_console_command(command)


def _scan_processes() -> list[tuple[int, str]]:
    skip = {os.getpid(), os.getppid()}
    found: list[tuple[int, str]] = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) in skip:
                continue
            command = _command_line(int(entry.name))
            if command and looks_like_console_command(command):
                found.append((int(entry.name), command))
        return sorted(found)
    try:
        output = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="], text=True, capture_output=True, check=False
        ).stdout
    except OSError:
        return []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        if not pid_text.isdigit() or int(pid_text) in skip:
            continue
        if looks_like_console_command(command):
            found.append((int(pid_text), command))
    return found


def port_in_use(host: str, port: int) -> bool:
    """True when something already listens on host:port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host if host not in {"0.0.0.0", ""} else "127.0.0.1", port))
        except OSError:
            return True
    return False


def _port_from_command(command: str) -> int:
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token == "--port" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
            return int(tokens[index + 1])
        if token.startswith("--port="):
            value = token.split("=", 1)[1]
            return int(value) if value.isdigit() else 8080
    return 8080


def _root_from_command(command: str) -> str:
    tokens = command.split()
    try:
        position = tokens.index("ui")
    except ValueError:
        return ""
    for token in tokens[position + 1 :]:
        if not token.startswith("-"):
            return str(Path(token).expanduser().resolve()) if token else ""
        if token in {"--port", "--host"}:
            break
    return str(Path.cwd())


def find_consoles(root: Path | None = None, port: int | None = None) -> list[ConsoleRecord]:
    """Running consoles, optionally filtered by project root and port."""
    records: dict[int, ConsoleRecord] = {}
    if REGISTRY_DIR.is_dir():
        for path in sorted(REGISTRY_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = ConsoleRecord(**{k: payload[k] for k in ("pid", "host", "port", "root", "started_utc")})
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if _is_console_process(record.pid):
                records[record.pid] = record
            else:
                path.unlink(missing_ok=True)
    own = os.getpid()
    for pid, command in _scan_processes():
        if pid in records or pid == own:
            continue
        records[pid] = ConsoleRecord(
            pid=pid, host="127.0.0.1", port=_port_from_command(command),
            root=_root_from_command(command), started_utc="", source="scan",
        )
    wanted_root = str(Path(root).expanduser().resolve()) if root is not None else None
    result = []
    for record in sorted(records.values(), key=lambda r: r.pid):
        if wanted_root is not None and record.root and record.root != wanted_root:
            continue
        if port is not None and record.port != port:
            continue
        result.append(record)
    return result


def stop_console(record: ConsoleRecord, grace_s: float = 5.0) -> bool:
    """Terminate one console process; returns True when it is gone.

    Only the console's own pid is signalled, never its process group: a console
    started from a script or a login shell may share a group with the thing that
    launched it. Jobs are separate sessions and are unaffected either way.
    """
    def alive() -> bool:
        try:
            os.kill(record.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return _is_console_process(record.pid)

    if not alive():
        unregister(record.pid)
        return True
    for signum, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 2.0)):
        try:
            os.kill(record.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not alive():
                unregister(record.pid)
                return True
            time.sleep(0.1)
    return not alive()


if __name__ == "__main__":  # pragma: no cover - manual check
    for item in find_consoles():
        print(item.pid, item.url, item.root, item.source, file=sys.stderr)
