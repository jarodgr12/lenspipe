"""Shared page frame (header, navigation drawer) and small display helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from nicegui import ui

from lenspipe.ui.state import console

__all__ = ["frame", "status_color", "status_chip", "fmt_utc", "duration", "MONO"]

ACCENT = "#0B6E74"
MONO = "font-mono text-sm break-all"

NAV = [
    ("Project", "/", "folder"),
    ("Parameters", "/parameters", "tune"),
    ("Run", "/run", "play_arrow"),
    ("Jobs", "/jobs", "list_alt"),
    ("Results", "/results", "image"),
]

STATUS_COLORS = {
    "queued": "grey-7",
    "running": "amber-8",
    "completed": "green-7",
    "failed": "red-7",
    "cancelled": "grey-8",
    "skipped": "grey-8",
}


def status_color(status: str) -> str:
    return STATUS_COLORS.get(status, "grey-7")


def status_chip(status: str) -> ui.badge:
    return ui.badge(status, color=status_color(status)).props("outline" if status == "queued" else "")


def fmt_utc(stamp: str | None) -> str:
    if not stamp:
        return "-"
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def duration(start: str | None, end: str | None) -> str:
    if not start:
        return "-"
    try:
        begin = datetime.fromisoformat(start)
        finish = datetime.fromisoformat(end) if end else datetime.now(UTC)
    except ValueError:
        return "-"
    seconds = int((finish - begin).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@contextmanager
def frame(title: str, path: str) -> Iterator[None]:
    """Header + left navigation shared by every page; yields inside the content column."""
    ui.colors(primary=ACCENT)
    dark = ui.dark_mode(console.dark)

    def toggle_dark() -> None:
        console.dark = not console.dark
        dark.set_value(console.dark)
        toggle.props(f"icon={'light_mode' if console.dark else 'dark_mode'}")

    with ui.header(elevated=False).classes("items-center justify-between px-4 py-2"):
        with ui.row().classes("items-center gap-3"):
            ui.label("lenspipe").classes("text-lg font-medium")
            ui.label(title).classes("text-base opacity-80")
        with ui.row().classes("items-center gap-3"):
            ui.label(str(console.root)).classes("font-mono text-xs opacity-80")
            toggle = ui.button(
                icon="light_mode" if console.dark else "dark_mode", on_click=toggle_dark
            ).props("flat dense round color=white")

    with ui.left_drawer(value=True, bordered=True).props("width=200").classes("p-0"):
        with ui.list().props("dense padding").classes("w-full"):
            for label, target, icon in NAV:
                active = target == path
                with ui.item(on_click=lambda t=target: ui.navigate.to(t)).props(
                    f"clickable {'active' if active else ''}"
                ).classes("text-primary" if active else ""):
                    with ui.item_section().props("avatar"):
                        ui.icon(icon)
                    with ui.item_section():
                        ui.item_label(label)

    with ui.column().classes("w-full max-w-screen-xl gap-4 p-2"):
        _outdated_banner()
        yield


def _outdated_banner() -> None:
    """After an update the console keeps running the old code (job queueing included) until it restarts."""
    from lenspipe import __version__
    from lenspipe.updates import console_outdated

    newer = console_outdated()
    if newer is None:
        return

    async def restart() -> None:
        import asyncio

        from nicegui import run

        from lenspipe.updates import restart_console

        ui.notify("Restarting the console; this page reloads in a few seconds", type="info")
        ui.run_javascript("setTimeout(() => location.reload(), 6000)")
        await asyncio.sleep(0.5)
        await run.io_bound(restart_console)

    with ui.row().classes("w-full items-center gap-3 no-wrap q-pa-sm rounded-borders bg-red-1 text-red-10"):
        ui.icon("warning", size="sm")
        ui.label(
            f"lenspipe {newer} is installed, but this console is still running {__version__}: it queues and "
            "starts jobs the old way until it is restarted. Restart it when no job is running."
        ).classes("text-sm flex-1")
        ui.button("Restart console", icon="restart_alt", on_click=restart).props("unelevated dense no-caps color=red-8")
