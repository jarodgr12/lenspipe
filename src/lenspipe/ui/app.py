"""Console entry point: ``lenspipe ui`` calls :func:`serve`."""

from __future__ import annotations

from pathlib import Path

from nicegui import app, ui

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


def serve(project_root: Path, host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    """Open ``project_root`` in the console and block serving it until interrupted."""
    console.open_root(project_root)
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
