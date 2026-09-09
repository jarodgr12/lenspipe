"""Project page: choose the project root and see what each epoch has produced."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import run, ui

from lenspipe.config import CONFIG_FILENAME, default_toml
from lenspipe.ui.layout import MONO, frame
from lenspipe.ui.state import console

COLUMNS = [
    {"name": "epoch", "label": "Epoch", "field": "epoch", "align": "left"},
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
        rows.append(
            {
                "epoch": f"{item['source']}.{item['epoch']}",
                "input": _present(item["input"]),
                "master": _present(item["master_model"]),
                "stage1": stage1_cell,
                "stage2": stage2_cell,
                "stage3": stage3_cell,
            }
        )
    return rows


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

        header = ui.row().classes("w-full items-center gap-3")
        table_holder = ui.column().classes("w-full")
        combined_holder = ui.column().classes("w-full gap-1")

        with ui.expansion("Environment", icon="health_and_safety").classes("w-full").props("dense") as env_panel:
            env_holder = ui.column().classes("w-full gap-1")

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

        async def refresh() -> None:
            summary = await run.io_bound(console.inventory)
            header.clear()
            with header:
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
            table_holder.clear()
            with table_holder:
                if not summary["epochs"]:
                    ui.label(
                        f"No inputs found. Place <source>.gmod and <source>.<epoch>.uvfits files in "
                        f"{console.layout.inputs}"
                    ).classes(MONO + " opacity-70")
                else:
                    table = ui.table(
                        columns=COLUMNS, rows=inventory_rows(summary), row_key="epoch"
                    ).props("dense flat bordered").classes("w-full")
                    for name in ("input", "master", "stage1", "stage2", "stage3"):
                        table.add_slot(f"body-cell-{name}", BADGE_SLOT)
            combined_holder.clear()
            with combined_holder:
                for item in summary["combined"]:
                    tag = f"/{item['analysis_tag']}" if item.get("analysis_tag") else ""
                    status = item.get("status") or "fresh"
                    ui.label(
                        f"combined  {item['source']}.{item['product']}{tag}  "
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
        ui.timer(0.1, refresh_environment, once=True)
        ui.timer(5.0, auto_refresh)
