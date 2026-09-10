"""Console entry point: ``lenspipe ui`` calls :func:`serve`."""

from __future__ import annotations

import atexit
from pathlib import Path

from nicegui import app, ui

from lenspipe.console_registry import register, unregister
from lenspipe.ui import (  # noqa: F401 - importing registers the @ui.page routes
    pages_jobs,
    pages_parameters,
    pages_project,
    pages_results,
    pages_run,
)
from lenspipe.ui.state import console

__all__ = ["serve"]

TICK_SECONDS = 2.0


def serve(
    project_root: Path | None, host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True
) -> None:
    """Serve the console until interrupted, opening ``project_root`` (or the last used project).

    The console registers itself so ``lenspipe stop`` can find it, re-registers
    when the project is switched in the UI, and removes the record on a clean
    shutdown.
    """
    console.host, console.port = host, port
    console.open_root(project_root if project_root is not None else console.default_root())
    register(host, port, console.root)
    atexit.register(unregister)
    app.on_shutdown(unregister)
    # Queued jobs start and chained stages are released even when no browser tab is open.
    app.timer(TICK_SECONDS, console.tick)
    ui.run(
        host=host,
        port=port,
        title="lenspipe",
        reload=False,
        show=open_browser,
        dark=None,
        show_welcome_message=True,
    )
