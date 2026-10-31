"""Project page: choose the project root and see what each epoch has produced."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import run, ui

from lenspipe import __version__
from lenspipe.config import CONFIG_FILENAME, default_toml
from lenspipe.project import utc_now
from lenspipe.ui.layout import MONO, frame
from lenspipe.ui.state import console

_started_utc = utc_now()  # update jobs created after this need a restart to take effect

COLUMNS = [
    {"name": "epoch", "label": "Epoch", "field": "epoch", "align": "left"},
    {"name": "mode", "label": "Mode", "field": "mode", "align": "left"},
    {"name": "input", "label": "Input", "field": "input", "align": "left"},
    {"name": "master", "label": "Master model", "field": "master", "align": "left"},
    {"name": "stage1", "label": "Stage 1", "field": "stage1", "align": "left"},
    {"name": "stage2", "label": "Stage 2 products", "field": "stage2", "align": "left"},
    {"name": "stage3", "label": "Stage 3 products", "field": "stage3", "align": "left"},
]

BADGE_SLOT = """
<q-td :props="props">
  <q-badge v-for="item in props.value" :key="item.text" :color="item.color"
           :outline="item.outline" class="q-mr-xs" style="font-family: monospace">
    {{ item.text }}
  </q-badge>
</q-td>
"""


STATUS_COLOURS = {
    "fresh": "green-7",
    "stale": "red-7",
    "upstream-stale": "amber-8",
    "missing-input": "red-7",
    "unknown": "blue-grey-6",
}
STATUS_SUFFIX = {
    "stale": " · stale",
    "upstream-stale": " · upstream stale",
    "missing-input": " · input missing",
    "unknown": " · unverified",
}


def _present(text: str | None, missing: str = "missing") -> list[dict[str, Any]]:
    if text:
        return [{"text": text, "color": "green-7", "outline": False}]
    return [{"text": missing, "color": "grey-6", "outline": True}]


def _product_badge(text: str, status: str | None) -> dict[str, Any]:
    """A badge coloured by provenance status; hover text carries the reasons."""
    status = status or "fresh"
    return {
        "text": text + STATUS_SUFFIX.get(status, ""),
        "color": STATUS_COLOURS.get(status, "green-7"),
        "outline": status == "unknown",
    }


def inventory_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in summary["epochs"]:
        stage1 = item["stage1"]
        if stage1:
            rms = stage1.get("rms_jy_per_beam")
            text = f"v{stage1.get('version') or '?'}"
            if isinstance(rms, (int, float)):
                text += f"  rms {rms:.3g} Jy/b"
            stage1_cell = [_product_badge(text, stage1.get("status"))]
        else:
            stage1_cell = _present(None)
        stage2_cell = [
            _product_badge(p["product"], p.get("status")) for p in item["stage2"]
        ] or _present(None, "none")
        stage3_cell = [
            _product_badge(
                (p["product"] or "?") + (f"/{p['analysis_tag']}" if p["analysis_tag"] else ""),
                p.get("status"),
            )
            for p in item["stage3"]
        ] or _present(None, "none")
        mode = item.get("calibration_mode")
        rows.append(
            {
                "key": f"{mode or '-'}/{item['source']}.{item['epoch']}",
                "epoch": f"{item['source']}.{item['epoch']}",
                "mode": mode or "",
                "input": _present(item["input"]),
                "master": _present(item["master_model"]),
                "stage1": stage1_cell,
                "stage2": stage2_cell,
                "stage3": stage3_cell,
            }
        )
    return rows


def _flag_card(report, verbose: bool) -> None:
    """One file's flag report: summary line, the fully flagged windows, optionally every window."""
    lines = report.lines()
    dead = report.fully_flagged_ifs
    with ui.card().classes("w-full q-pa-sm gap-1").props("flat bordered"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.badge(f"{len(dead)} empty" if dead else "ok", color="red-7" if dead else "green-7")
            ui.label(lines[0]).classes(MONO + " text-sm")
        if dead:
            ui.label(lines[1]).classes(MONO + " text-sm text-negative")
        partial = [item for item in report.ifs if item.fully_flagged_channels and not item.fully_flagged]
        if partial and not verbose:
            ui.label(
                f"{len(partial)} window(s) have some fully flagged channels: "
                + ", ".join(f"IF {i.if_number} (spw {i.spw}, {len(i.fully_flagged_channels)} ch)" for i in partial)
            ).classes(MONO + " text-sm opacity-80")
        if verbose:
            rows = [
                {
                    "if": item.if_number, "spw": item.spw, "channels": f"{item.first_channel}-{item.last_channel}",
                    "unflagged": f"{100 * item.unflagged_fraction:.1f}%",
                    "note": "FULLY FLAGGED" if item.fully_flagged else (
                        f"{len(item.fully_flagged_channels)} channel(s) fully flagged" if item.fully_flagged_channels else ""
                    ),
                }
                for item in report.ifs
            ]
            ui.table(
                columns=[
                    {"name": "if", "label": "IF", "field": "if", "align": "right"},
                    {"name": "spw", "label": "spw", "field": "spw", "align": "right"},
                    {"name": "channels", "label": "DifMAP channels", "field": "channels", "align": "left"},
                    {"name": "unflagged", "label": "Unflagged", "field": "unflagged", "align": "right"},
                    {"name": "note", "label": "", "field": "note", "align": "left"},
                ],
                rows=rows, row_key="if",
            ).props("dense flat bordered").classes("w-full")


@ui.page("/")
def project_page() -> None:
    with frame("Project", "/"):
        with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                root_input = ui.input("Project root", value=str(console.root)).props(
                    "dense outlined"
                ).classes("flex-grow font-mono")
                ui.button("Open", icon="folder_open", on_click=lambda: open_root()).props("no-caps")
                ui.button("Refresh", icon="refresh", on_click=lambda: refresh()).props(
                    "outline no-caps"
                )
            recent = [r for r in console.recent_roots() if r != str(console.root)]
            if recent:
                ui.select(
                    recent, label="Recent roots", on_change=lambda e: root_input.set_value(e.value)
                ).props("dense outlined options-dense").classes("w-full font-mono")
            root_status = ui.label().classes("text-sm")

        with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                ui.label("Version").classes("text-subtitle2")
                version_label = ui.label(f"lenspipe {__version__}; checking for a newer release...").classes("text-sm")
                version_badge = ui.badge("", color="amber-8")
                version_badge.set_visibility(False)
                ui.space()
                ui.button("Check again", icon="refresh", on_click=lambda: refresh_version(True)).props("outline no-caps dense")
                update_button = ui.button("Update", icon="system_update_alt", on_click=lambda: update_now()).props("no-caps dense")
                update_button.tooltip(
                    "Runs 'lenspipe update' as a job: reinstalls lenspipe from where it was installed from. "
                    "The console keeps running the old version until it is restarted."
                )
                restart_button = ui.button("Restart console", icon="restart_alt", on_click=lambda: restart_now()).props(
                    "outline no-caps dense"
                )
                restart_button.tooltip(
                    "Starts this console again with the installed version; the page reloads by itself. "
                    "Running jobs are separate processes and keep going."
                )
            version_note = ui.label().classes("text-caption opacity-70")

        header = ui.row().classes("w-full items-center gap-3")
        table_holder = ui.column().classes("w-full")
        combined_holder = ui.column().classes("w-full gap-1")

        with ui.expansion("Runs (separate parameter sets with their own outputs)", icon="account_tree").classes(
            "w-full"
        ).props("dense") as runs_panel:
            ui.label(
                "A run is runs/<label>/ inside the project: a copy of lenspipe.toml frozen when the run is created, "
                "the project's inputs/ shared, and its own stage1/, stage2/ and stage3/. Queue one job per label "
                "from the Run page to compare parameter sets; the Parameters page never changes a run once it exists."
            ).classes("text-caption opacity-70")
            with ui.row().classes("items-center gap-3"):
                new_run_label = ui.input("New run label", placeholder="e.g. sched-A").props("dense outlined").classes("w-64")
                ui.button("Create with the current parameters", icon="add", on_click=lambda: create_run_now()).props(
                    "outline no-caps dense"
                )
                ui.label("(creates the run now without queuing a job)").classes("text-caption opacity-70")
            runs_status = ui.label().classes("text-sm")
            runs_holder = ui.column().classes("w-full gap-2")

        with ui.expansion("Flags (which spectral windows are empty)", icon="flag").classes("w-full").props("dense") as flags_panel:
            ui.label(
                "Reads the weights of the UV-FITS files: weights at or below zero are flags, exactly what DifMAP "
                "sees. A window that is fully flagged gives a Stage 2 fit nothing to work on (\"no unflagged "
                "visibilities\"). IF numbers are DifMAP's (from 1), spw numbers CASA's (from 0). A large visit is "
                "one sequential read of the file."
            ).classes("text-caption opacity-70")
            with ui.row().classes("items-center gap-3"):
                flags_source = ui.select(
                    {"calibrated": "Stage 1 calibrated data (cal.uvf)", "raw": "Raw inputs (inputs/*.uvfits)"},
                    value="calibrated", label="Files",
                ).props("dense outlined").classes("w-72")
                flags_epochs = ui.select([], label="Epochs (empty = all)", multiple=True, clearable=True).props(
                    "dense outlined use-chips"
                ).classes("w-64")
                flags_verbose = ui.switch("Every window", value=False)
                flags_button = ui.button("Check flags", icon="search", on_click=lambda: refresh_flags()).props("no-caps")
            flags_status = ui.label().classes("text-sm opacity-70")
            flags_holder = ui.column().classes("w-full gap-2")

        with ui.expansion("Environment", icon="health_and_safety").classes("w-full").props("dense") as env_panel:
            env_holder = ui.column().classes("w-full gap-1")

        async def refresh_flags() -> None:
            from lenspipe.flags import flag_report
            from lenspipe.stage1 import discover_inputs as discover_raw
            from lenspipe.stage2 import discover_inputs as discover_calibrated

            cfg = console.config()
            epochs = set(flags_epochs.value or []) or None
            try:
                if flags_source.value == "raw":
                    paths = discover_raw(console.root, cfg.stage1.input_pattern, epochs)
                else:
                    paths = discover_calibrated(
                        console.root, cfg.stage2.input_pattern, epochs, cfg.stage1.calibration_mode
                    )
            except FileNotFoundError as exc:
                flags_status.set_text(str(exc))
                return
            if not paths:
                flags_status.set_text("No UV-FITS files found for that selection.")
                return
            flags_button.disable()
            flags_holder.clear()
            dead_total = 0
            try:
                for index, path in enumerate(paths, start=1):
                    flags_status.set_text(f"Reading {path.name} ({index} of {len(paths)}) ...")
                    report = await run.io_bound(flag_report, path)
                    dead_total += len(report.fully_flagged_ifs)
                    with flags_holder:
                        _flag_card(report, bool(flags_verbose.value))
            finally:
                flags_button.enable()
            flags_status.set_text(
                f"{len(paths)} file(s) checked; "
                + (f"{dead_total} fully flagged window(s)" if dead_total else "no fully flagged window")
            )
            flags_panel.text = "Flags (which spectral windows are empty)" + (f"  ({dead_total} empty)" if dead_total else "")

        async def refresh_version(force: bool = False) -> None:
            from lenspipe.updates import check_for_update

            version_label.set_text(f"lenspipe {__version__}; checking for a newer release...")
            status = await run.io_bound(check_for_update, refresh=force)
            version_label.set_text(status.summary())
            version_badge.set_text(f"{status.latest} available" if status.update_available else "up to date")
            version_badge.props(f"color={'amber-8' if status.update_available else 'green-7'}")
            version_badge.set_visibility(status.latest is not None)
            update_button.set_enabled(status.can_update)
            finished = [j for j in console.jobs() if j.stage == "update" and j.status == "completed"]
            version_note.set_text(
                "An update job has finished: restart the console to load the new version."
                if finished and finished[0].created_utc > _started_utc
                else "Update reinstalls lenspipe from the repository (what 'lenspipe update' does), then restart the console."
            )

        async def update_now() -> None:
            from lenspipe.ui.state import ChainStep

            first = await run.io_bound(console.submit_chain, [ChainStep(["update"], "Update lenspipe", "update")])
            if first is not None:
                ui.navigate.to(f"/jobs?job={first.id}")

        async def restart_now() -> None:
            import asyncio

            from lenspipe.updates import restart_console

            ui.notify("Restarting the console; this page reloads in a few seconds", type="info")
            ui.run_javascript("setTimeout(() => location.reload(), 6000)")
            await asyncio.sleep(0.5)
            await run.io_bound(restart_console)

        async def refresh_environment() -> None:
            from lenspipe.doctor import run_checks

            checks = await run.io_bound(run_checks, console.root, console.config())
            env_holder.clear()
            colours = {"ok": "green-7", "warn": "amber-8", "fail": "red-7"}
            with env_holder:
                for check in checks:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.badge(check.status.upper(), color=colours[check.status]).classes("q-mt-xs")
                        with ui.column().classes("gap-0"):
                            ui.label(f"{check.name}: {check.detail}").classes(MONO + " text-sm")
                            if check.fix:
                                ui.label(f"fix: {check.fix}").classes("text-sm text-grey-7")
            failing = [c for c in checks if c.status == "fail"]
            env_panel.text = "Environment" + (f"  ({len(failing)} problem(s))" if failing else "")
            if failing:
                env_panel.open()

        def _runs_with_diffs() -> list[tuple[Any, str]]:
            return [(item, item.config_diff()) for item in console.runs()]

        async def refresh_runs() -> None:
            found = await run.io_bound(_runs_with_diffs)
            runs_panel.text = "Runs (separate parameter sets with their own outputs)" + (f"  ({len(found)})" if found else "")
            runs_holder.clear()
            with runs_holder:
                if not found:
                    ui.label("No runs yet.").classes("text-caption opacity-70")
                for item, diff in found:
                    counts = item.to_dict()
                    with ui.card().classes("w-full q-pa-sm gap-1").props("flat bordered"):
                        with ui.row().classes("w-full items-center gap-3 no-wrap"):
                            ui.label(item.label).classes("text-subtitle2 font-mono")
                            created = (item.created_utc or "")[:16].replace("T", " ")
                            ui.label(f"created {created or 'unknown'}").classes("text-caption opacity-70")
                            for name in ("stage1", "stage2", "stage3", "combined"):
                                ui.badge(f"{name} {counts[name]}", color="green-7" if counts[name] else "grey-6").props(
                                    "" if counts[name] else "outline"
                                )
                            if diff:
                                ui.badge("parameters differ from the project's", color="amber-8")
                            else:
                                ui.badge("parameters same as the project's", color="blue-grey-6").props("outline")
                            ui.space()
                            ui.link("Run page", f"/run?run={item.label}").classes("text-sm")
                            ui.link("Results", f"/results?run={item.label}").classes("text-sm")
                        ui.label(str(item.root)).classes(MONO + " text-xs opacity-60")
                        if diff:
                            with ui.expansion("How the project's parameters differ from this run's frozen copy").classes(
                                "w-full"
                            ).props("dense"):
                                ui.code(diff, language="diff").classes("w-full text-xs")

        async def create_run_now() -> None:
            from lenspipe.runs import create_run

            label = (new_run_label.value or "").strip()
            if not label:
                runs_status.set_text("Type a label first.")
                return
            try:
                item, created = await run.io_bound(create_run, console.root, label)
            except (ValueError, FileNotFoundError) as exc:
                runs_status.set_text(str(exc))
                runs_status.classes(add="text-negative")
                return
            runs_status.classes(remove="text-negative")
            runs_status.set_text(
                f"{'Created' if created else 'Already exists:'} {item.root} (lenspipe.toml frozen from the project)"
            )
            new_run_label.set_value("")
            await refresh_runs()

        async def refresh() -> None:
            summary = await run.io_bound(console.inventory)
            header.clear()
            with header:
                if summary.get("is_run"):
                    ui.badge("this project is a run; its parent is one level up", color="amber-8")
                if summary["config_present"]:
                    ui.badge(CONFIG_FILENAME, color="green-7")
                    error = console.config_error()
                    if error:
                        ui.label(error).classes("text-negative text-sm")
                else:
                    ui.badge("no lenspipe.toml (defaults apply)", color="amber-8")
                    ui.button(
                        "Create default config", icon="note_add", on_click=create_config
                    ).props("dense outline no-caps")
                active = console.active_jobs()
                if active:
                    ui.badge(f"{len(active)} job(s) active", color="amber-8")
                freshness = summary.get("freshness", {})
                stale_count = sum(
                    freshness.get(key, 0) for key in ("stale", "upstream-stale", "missing-input")
                )
                if stale_count:
                    ui.badge(f"{stale_count} stale product(s)", color="red-7").tooltip(
                        "Inputs changed since these products were made; re-run the stage."
                    )
                elif freshness.get("fresh"):
                    ui.badge("all products fresh", color="green-7")
                ui.label(f"{len(summary['epochs'])} epoch(s)").classes("text-sm opacity-70")
            flags_epochs.set_options(sorted({row["epoch"] for row in summary["epochs"]}))
            table_holder.clear()
            with table_holder:
                if not summary["epochs"]:
                    ui.label(
                        f"No inputs found. Place <source>.gmod and <source>.<epoch>.uvfits files in "
                        f"{console.layout.inputs}"
                    ).classes(MONO + " opacity-70")
                else:
                    table = ui.table(
                        columns=COLUMNS, rows=inventory_rows(summary), row_key="key"
                    ).props("dense flat bordered").classes("w-full")
                    for name in ("input", "master", "stage1", "stage2", "stage3"):
                        table.add_slot(f"body-cell-{name}", BADGE_SLOT)
            combined_holder.clear()
            with combined_holder:
                for item in summary["combined"]:
                    tag = f"/{item['analysis_tag']}" if item.get("analysis_tag") else ""
                    status = item.get("status") or "fresh"
                    ui.label(
                        f"combined  [{item.get('calibration_mode') or '-'}]  "
                        f"{item['source']}.{item['product']}{tag}  "
                        f"visits={item['n_visits']}  {item['path']}"
                        + STATUS_SUFFIX.get(status, "")
                    ).classes(MONO + " opacity-80" + (" text-negative" if status != "fresh" else ""))

        async def open_root() -> None:
            path = Path(root_input.value or "").expanduser()
            if not path.is_dir():
                root_status.set_text(f"Not a directory: {path}")
                root_status.classes(add="text-negative")
                return
            console.open_root(path)
            ui.navigate.to("/")

        async def create_config() -> None:
            path = console.layout.config_path
            if path.exists():
                ui.notify(f"{path} already exists", type="warning")
            else:
                path.write_text(default_toml(), encoding="utf-8")
                ui.notify(f"Wrote {path}", type="positive")
            await refresh()

        async def auto_refresh() -> None:
            if console.active_jobs():
                await refresh()

        ui.timer(0.0, refresh, once=True)
        ui.timer(0.05, refresh_runs, once=True)
        ui.timer(0.1, refresh_environment, once=True)
        ui.timer(0.2, refresh_version, once=True)
        ui.timer(5.0, auto_refresh)
