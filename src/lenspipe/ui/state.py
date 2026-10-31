"""Process-wide console state: the open project, its job manager, and stage chains."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nicegui import app
from starlette.routing import Mount, Route

from lenspipe.config import LenspipeConfig, load_config
from lenspipe.jobs import JobManager, JobRecord
from lenspipe.project import Layout, inventory

__all__ = ["ChainStep", "Console", "console"]

FILES_PREFIX = "/files"
RECENT_PATH = Path("~/.lenspipe/recent.json").expanduser()
RECENT_LIMIT = 8


@dataclass
class ChainStep:
    argv: list[str]
    title: str
    stage: str


@dataclass
class Console:
    root: Path = field(default_factory=lambda: Path.cwd().resolve())
    manager: JobManager | None = None
    dark: bool = False
    notices: list[str] = field(default_factory=list)
    _noticed: set[str] = field(default_factory=set)
    host: str | None = None  # set by serve(); None when not serving (tests)
    port: int | None = None

    # -- project ------------------------------------------------------------

    @property
    def layout(self) -> Layout:
        return Layout.at(self.root)

    def open_root(self, root: Path | str) -> None:
        """Switch the whole console to another project root.

        A path inside a project (``.../stage2/full-band/X``) opens that project; a
        directory that is no project at all is still opened as given, so a new
        project can be started from the console, but its page says so.
        """
        from lenspipe.project import NotAProject, find_project_root

        try:
            resolved, _ = find_project_root(root)
        except NotAProject:
            resolved = Layout.at(root).root
        layout = Layout.at(resolved)
        self.root = layout.root
        self.manager = JobManager(self.root, max_concurrent=1)
        self._noticed.clear()
        self._mount_files()
        self.remember_root(self.root)
        if self.host is not None and self.port is not None:
            # Keep `lenspipe stop <project>` accurate after switching projects in the UI.
            from lenspipe.console_registry import register

            register(self.host, self.port, self.root)

    @classmethod
    def default_root(cls) -> Path:
        """The project to open when none is given: the most recent one, else the cwd."""
        for candidate in cls.recent_roots():
            path = Path(candidate)
            if path.is_dir():
                return path
        return Path.cwd().resolve()

    def _mount_files(self) -> None:
        # Starlette keeps routes in a plain list, so the previous project's handler can be
        # dropped before the new root is served read-only under the same prefix. NiceGUI
        # registers static files as a Mount in older releases and as a plain GET route in
        # newer ones; whichever is left in place would keep winning the route match and
        # every image on the Results page would come back 404 after switching projects.
        stale = {FILES_PREFIX, f"{FILES_PREFIX}/{{path:path}}"}
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if not (isinstance(route, Mount | Route) and route.path in stale)
        ]
        app.add_static_files(FILES_PREFIX, str(self.root), max_cache_age=0)

    def file_url(self, path: Path) -> str:
        return f"{FILES_PREFIX}/{path.resolve().relative_to(self.root).as_posix()}"

    def config(self) -> LenspipeConfig:
        return load_config(self.root)

    def config_error(self) -> str | None:
        try:
            load_config(self.root)
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            return f"{type(exc).__name__}: {exc}"
        return None

    def inventory(self) -> dict[str, Any]:
        return inventory(self.root)

    def runs(self) -> list:
        """The project's runs (named parameter sets under ``runs/``), by label."""
        from lenspipe.runs import list_runs

        return list_runs(self.root)

    def run_root(self, label: str | None) -> Path | None:
        """The root of an existing run, or None when ``label`` is empty or names no run yet."""
        from lenspipe.runs import get_run

        if not label:
            return None
        try:
            run = get_run(self.root, label)
        except ValueError:
            return None
        return run.root if run else None

    # -- recent roots -------------------------------------------------------

    @staticmethod
    def recent_roots() -> list[str]:
        try:
            data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [str(item) for item in data if isinstance(item, str)]

    @classmethod
    def remember_root(cls, root: Path) -> None:
        entries = [str(root)] + [item for item in cls.recent_roots() if item != str(root)]
        try:
            RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            RECENT_PATH.write_text(json.dumps(entries[:RECENT_LIMIT], indent=1), encoding="utf-8")
        except OSError:
            pass

    # -- jobs ---------------------------------------------------------------

    def jobs(self, limit: int | None = None) -> list[JobRecord]:
        return self.manager.list(limit) if self.manager else []

    def active_jobs(self) -> list[JobRecord]:
        return [job for job in self.jobs() if job.status in {"queued", "running"}]

    def submit_chain(self, steps: list[ChainStep]) -> JobRecord | None:
        """Queue every step now, each waiting for the previous one to complete.

        Queuing the whole chain at once keeps the queue in submission order: a
        chain submitted later cannot start before an earlier chain has finished.
        """
        if not steps or self.manager is None:
            return None
        first: JobRecord | None = None
        previous: JobRecord | None = None
        for step in steps:
            record = self.manager.submit(
                step.argv, title=step.title, stage=step.stage, depends_on=previous.id if previous else None
            )
            first = first or record
            previous = record
        return first

    def tick(self) -> None:
        """Reconcile job state and start the next queued job. Called from an app timer."""
        if self.manager is None:
            return
        self.manager.pump()
        for record in self.manager.list(50):
            if record.status == "skipped" and record.id not in self._noticed:
                self._noticed.add(record.id)
                self.notices.append(f"Skipped {record.title}: {record.error or 'the previous stage did not complete'}.")

    def pending_chain_titles(self) -> list[str]:
        """Queued jobs that wait for an earlier stage, oldest first."""
        waiting = [job for job in self.jobs() if job.status == "queued" and job.depends_on]
        return [job.title for job in sorted(waiting, key=lambda job: job.created_utc)]

    @staticmethod
    def cpu_count() -> int:
        return os.cpu_count() or 1


console = Console()
