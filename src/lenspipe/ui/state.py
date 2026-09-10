"""Process-wide console state: the open project, its job manager, and stage chains."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nicegui import app
from starlette.routing import Mount

from lenspipe.config import LenspipeConfig, load_config
from lenspipe.jobs import JobManager, JobRecord
from lenspipe.project import Layout, inventory

__all__ = ["ChainStep", "Console", "Pipeline", "console"]

FILES_PREFIX = "/files"
RECENT_PATH = Path("~/.lenspipe/recent.json").expanduser()
RECENT_LIMIT = 8


@dataclass
class ChainStep:
    argv: list[str]
    title: str
    stage: str


@dataclass
class Pipeline:
    """Stage jobs submitted one after another; stops at the first job that fails."""

    current: str
    remaining: list[ChainStep]


@dataclass
class Console:
    root: Path = field(default_factory=lambda: Path.cwd().resolve())
    manager: JobManager | None = None
    dark: bool = False
    pipelines: list[Pipeline] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    host: str | None = None  # set by serve(); None when not serving (tests)
    port: int | None = None

    # -- project ------------------------------------------------------------

    @property
    def layout(self) -> Layout:
        return Layout.at(self.root)

    def open_root(self, root: Path | str) -> None:
        """Switch the whole console to another project root."""
        layout = Layout.at(root)
        self.root = layout.root
        self.manager = JobManager(self.root, max_concurrent=1)
        self.pipelines.clear()
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
        # Starlette keeps routes in a plain list, so the previous mount can be dropped
        # before the new root is served read-only under the same prefix.
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if not (isinstance(route, Mount) and route.path == FILES_PREFIX)
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
        """Submit the first step now; each later step waits for the previous ``completed``."""
        if not steps or self.manager is None:
            return None
        first = self.manager.submit(steps[0].argv, title=steps[0].title, stage=steps[0].stage)
        if len(steps) > 1:
            self.pipelines.append(Pipeline(current=first.id, remaining=list(steps[1:])))
        return first

    def tick(self) -> None:
        """Reconcile job state and release chained steps. Called from an app timer."""
        if self.manager is None:
            return
        self.manager.pump()
        for pipeline in list(self.pipelines):
            record = self.manager.get(pipeline.current)
            if record is not None and record.status in {"queued", "running"}:
                continue
            if record is None or record.status != "completed":
                reason = record.status if record else "missing"
                titles = ", ".join(step.title for step in pipeline.remaining)
                self.notices.append(f"Skipped {titles}: job {pipeline.current} {reason}.")
                self.pipelines.remove(pipeline)
                continue
            step = pipeline.remaining.pop(0)
            new = self.manager.submit(step.argv, title=step.title, stage=step.stage)
            pipeline.current = new.id
            if not pipeline.remaining:
                self.pipelines.remove(pipeline)

    def pending_chain_titles(self) -> list[str]:
        return [step.title for pipeline in self.pipelines for step in pipeline.remaining]

    @staticmethod
    def cpu_count() -> int:
        return os.cpu_count() or 1


console = Console()
