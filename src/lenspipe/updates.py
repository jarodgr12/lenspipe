"""Version checks, the installation source, and restarting a running console.

The console shows these on the Project page; ``lenspipe update`` uses the same
helpers. The newest release is the highest ``vX.Y.Z`` tag on the repository,
read with ``git ls-remote`` (git is present wherever lenspipe was installed
from the repository), so no GitHub API or token is needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lenspipe import __version__

__all__ = [
    "DEFAULT_REPO",
    "UpdateStatus",
    "check_for_update",
    "install_source",
    "is_newer",
    "latest_release",
    "parse_release_tags",
    "restart_argv",
    "restart_console",
    "uv_executable",
]

DEFAULT_REPO = "https://github.com/jarodgr12/lenspipe.git"
_TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def is_newer(candidate: str, installed: str = __version__) -> bool:
    """True when ``candidate`` is a higher X.Y.Z than ``installed``."""
    return _version_tuple(candidate) > _version_tuple(installed)


def parse_release_tags(ls_remote_output: str) -> list[str]:
    """Release versions ('2.4.1', ...) from ``git ls-remote --tags`` output, highest last."""
    found = {m.group(1) + "." + m.group(2) + "." + m.group(3) for line in ls_remote_output.splitlines() if (m := _TAG.search(line.strip()))}
    return sorted(found, key=_version_tuple)


_RELEASE_CACHE: dict[str, tuple[float, str | None]] = {}
RELEASE_CACHE_SECONDS = 3600.0


def latest_release(repo: str = DEFAULT_REPO, timeout_s: float = 15.0, *, refresh: bool = False) -> str | None:
    """The newest release tag on ``repo``, or None when it cannot be read (offline, no git).

    The answer is kept for an hour per process, so opening the Project page does
    not reach the network every time; ``refresh`` forces a new look.
    """
    import time

    cached = _RELEASE_CACHE.get(repo)
    if cached is not None and not refresh and time.monotonic() - cached[0] < RELEASE_CACHE_SECONDS:
        return cached[1]
    result_version = _latest_release_uncached(repo, timeout_s)
    _RELEASE_CACHE[repo] = (time.monotonic(), result_version)
    return result_version


def _latest_release_uncached(repo: str, timeout_s: float) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "ls-remote", "--tags", "--refs", repo], text=True, capture_output=True, timeout=timeout_s,
            check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    tags = parse_release_tags(result.stdout)
    return tags[-1] if tags else None


def uv_executable() -> Path | None:
    found = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    return Path(found) if Path(found).is_file() else None


def install_source() -> str | None:
    """The ``uv tool list`` line for lenspipe (name, version, path), or None when not a uv tool install."""
    uv = uv_executable()
    if uv is None:
        return None
    listing = subprocess.run([str(uv), "tool", "list", "--show-paths"], text=True, capture_output=True, check=False)
    for line in listing.stdout.splitlines():
        if line.startswith("lenspipe "):
            return line.strip()
    return None


@dataclass(frozen=True)
class UpdateStatus:
    installed: str
    latest: str | None  # None: could not be checked
    source: str | None  # None: not installed as a uv tool (development checkout)

    @property
    def update_available(self) -> bool:
        return self.latest is not None and is_newer(self.latest, self.installed)

    @property
    def can_update(self) -> bool:
        return self.source is not None

    def summary(self) -> str:
        if self.latest is None:
            head = f"lenspipe {self.installed}; the newest release could not be checked (offline, or git missing)"
        elif self.update_available:
            head = f"lenspipe {self.installed}; version {self.latest} is available"
        else:
            head = f"lenspipe {self.installed} is the newest release"
        if not self.can_update:
            head += ". Not a uv tool install (a development checkout): pull and 'uv sync' to update"
        return head


def check_for_update(repo: str = DEFAULT_REPO, *, refresh: bool = False) -> UpdateStatus:
    return UpdateStatus(installed=__version__, latest=latest_release(repo, refresh=refresh), source=install_source())


def restart_argv(argv: list[str] | None = None) -> list[str]:
    """The command that re-executes this console: the same ``lenspipe ui`` arguments, no new browser tab."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--no-browser" not in args:
        args.append("--no-browser")
    return [sys.executable, "-m", "lenspipe", *args]


def restart_console() -> None:
    """Replace this process with a fresh ``lenspipe ui`` so the newly installed version loads.

    The listening socket is not inherited (Python sockets are non-inheritable), so
    the new process can bind the same port. The registry record is removed first;
    the new process registers itself again.
    """
    from lenspipe.console_registry import unregister

    unregister()
    os.execv(sys.executable, restart_argv())
