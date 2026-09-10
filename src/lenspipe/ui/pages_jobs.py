"""Jobs page: list, inspect, cancel and re-run jobs recorded under <project>/.jobs/."""

from __future__ import annotations

from typing import Any

from nicegui import run, ui

from lenspipe.jobs import JobRecord
from lenspipe.ui.layout import MONO, duration, fmt_utc, frame, status_chip
from lenspipe.ui.state import console

LIST_LIMIT = 100


@ui.page("/jobs")
def jobs_page(job: str | None = None) -> None:
    with frame("Jobs", "/jobs"):
        selected: dict[str, Any] = {"id": job, "signature": None, "log": None, "progress": None}
        list_signature: dict[str, Any] = {"value": None}

        notices = ui.column().classes("w-full gap-1")
        with ui.row().classes("w-full no-wrap items-start gap-4"):
            with ui.column().classes("w-[440px] max-w-full gap-1 shrink-0"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("Jobs, newest first").classes("text-subtitle2")
                    count_label = ui.label().classes("text-caption opacity-70")
                job_list = ui.column().classes("w-full gap-1")
            detail = ui.column().classes("flex-grow gap-2 min-w-0")

        def render_notices() -> None:
            notices.clear()
            pending = console.pending_chain_titles()
            if not pending and not console.notices:
                return
            with notices:
                if pending:
                    ui.label("Waiting for the previous stage: " + ", ".join(pending)).classes(
                        "text-caption text-amber-9"
                    )
                for text in console.notices[-5:]:
                    ui.label(text).classes("text-caption text-negative")

        def render_list(records: list[JobRecord]) -> None:
            job_list.clear()
            count_label.set_text(f"{len(records)} shown")
            with job_list:
                if not records:
                    ui.label("No jobs yet. Submit one from the Run page.").classes("opacity-70")
                for record in records:
                    active = record.id == selected["id"]
                    card = ui.card().classes(
                        "w-full q-pa-sm gap-0 cursor-pointer items-stretch overflow-hidden"
                    ).props("flat bordered")
                    if active:
                        card.style("border-color: var(--q-primary)")
                    with card:
                        card.on("click", lambda _, rid=record.id: select(rid))
                        with ui.row().classes("w-full items-center no-wrap gap-2 min-w-0"):
                            status_chip(record.status)
                            ui.label(record.title).classes("text-sm font-medium truncate min-w-0")
                        ui.label(record.command_line).classes(
                            "w-full font-mono text-xs opacity-70 truncate"
                        )
                        ui.label(
                            f"{fmt_utc(record.created_utc)}  ·  {duration(record.started_utc, record.finished_utc)}"
                        ).classes("w-full text-caption opacity-60")

        def render_detail(record: JobRecord | None) -> None:
            detail.clear()
            selected["log"] = None
            selected["progress"] = None
            with detail:
                if record is None:
                    ui.label("Select a job to see its progress and log.").classes("opacity-70")
                    return
                with ui.row().classes("w-full items-center gap-3"):
                    status_chip(record.status)
                    ui.label(record.title).classes("text-subtitle1")
                    ui.space()
                    if record.status in {"queued", "running"}:
                        ui.button("Cancel", icon="stop", on_click=lambda: cancel(record.id)).props(
                            "outline color=negative no-caps dense"
                        )
                    ui.button("Re-run", icon="replay", on_click=lambda: rerun(record)).props(
                        "outline no-caps dense"
                    )
                ui.label(record.command_line).classes(MONO + " select-all")
                with ui.grid(columns=4).classes("w-full gap-x-6 gap-y-1 text-sm"):
                    for name, value in (
                        ("Job id", record.id),
                        ("Created", fmt_utc(record.created_utc)),
                        ("Started", fmt_utc(record.started_utc)),
                        ("Finished", fmt_utc(record.finished_utc)),
                        ("Duration", duration(record.started_utc, record.finished_utc)),
                        ("Return code", "-" if record.returncode is None else str(record.returncode)),
                        ("PID", str(record.pid or "-")),
                    ):
                        with ui.column().classes("gap-0"):
                            ui.label(name).classes("text-caption opacity-60")
                            ui.label(value).classes("font-mono text-xs break-all")
                ui.label(f"log: {record.log_path}").classes("font-mono text-xs opacity-60 break-all")
                if record.error:
                    ui.label(record.error).classes("text-negative text-sm font-mono")
                selected["progress_box"] = ui.column().classes("w-full gap-1")
                ui.label("Log tail").classes("text-caption opacity-60")
                selected["log_box"] = ui.log(max_lines=400).classes(
                    "w-full h-[50vh] font-mono text-xs"
                )

        def update_live(record: JobRecord, progress: dict, text: str, force: bool = False) -> None:
            """Apply already-read progress and log tail; file reads happen off the event loop."""
            if force or progress != selected["progress"]:
                selected["progress"] = progress
                box = selected["progress_box"]
                box.clear()
                with box:
                    for label, item in progress.items():
                        total = item.get("total") or 0
                        done = item.get("done") or 0
                        fraction = (done / total) if total else 0.0
                        with ui.row().classes("w-full items-center no-wrap gap-3"):
                            ui.label(label).classes("font-mono text-xs w-48 truncate")
                            ui.linear_progress(
                                value=min(1.0, fraction), show_value=False, size="10px"
                            ).classes("flex-grow")
                            ui.label(f"{done}/{total}").classes("text-xs w-20 text-right")
                        if item.get("detail"):
                            ui.label(str(item["detail"])).classes(
                                "text-caption opacity-60 font-mono pl-2 truncate"
                            )
            if force or text != selected["log"]:
                selected["log"] = text
                log = selected["log_box"]
                log.clear()
                if text:
                    log.push(text)

        async def select(job_id: str) -> None:
            selected["id"] = job_id
            selected["signature"] = None
            list_signature["value"] = None
            await poll()

        async def cancel(job_id: str) -> None:
            if console.manager is None:
                return
            await run.io_bound(console.manager.cancel, job_id)
            ui.notify(f"Cancelled {job_id}", type="warning")
            await select(job_id)

        async def rerun(record: JobRecord) -> None:
            if console.manager is None:
                return
            new = await run.io_bound(
                console.manager.submit, record.argv, title=record.title, stage=record.stage
            )
            ui.notify(f"Submitted {new.id}", type="positive")
            await select(new.id)

        def _read_state() -> list[JobRecord]:
            console.tick()
            return console.jobs(LIST_LIMIT)

        async def poll() -> None:
            # Every file read runs in a worker thread: while DifMAP saturates the machine,
            # blocking the event loop here is what makes the browser report "Connection lost".
            records = await run.io_bound(_read_state)
            signature = tuple((r.id, r.status) for r in records) + (selected["id"],)
            if signature != list_signature["value"]:
                list_signature["value"] = signature
                render_list(records)
                render_notices()
            current = next((r for r in records if r.id == selected["id"]), None)
            if current is None and selected["id"] is None and records:
                selected["id"] = records[0].id
                current = records[0]
                list_signature["value"] = None
            detail_signature = (
                (current.id, current.status, current.finished_utc) if current else None
            )
            if detail_signature != selected["signature"]:
                selected["signature"] = detail_signature
                render_detail(current)
                if current is not None:
                    progress, text = await run.io_bound(lambda: (current.progress(), current.tail(300)))
                    update_live(current, progress, text, force=True)
            elif current is not None and current.status == "running":
                progress, text = await run.io_bound(lambda: (current.progress(), current.tail(300)))
                update_live(current, progress, text)

        ui.timer(0.0, poll, once=True)
        ui.timer(1.0, poll)
