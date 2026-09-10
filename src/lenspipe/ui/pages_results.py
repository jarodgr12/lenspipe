"""Results page: browse Stage 2 / Stage 3 plots and tables for one epoch or the combined set.

Everything that touches the filesystem runs in a worker thread, figures are
shown as cached thumbnails linking to the full image, and tables are read only
when their panel is opened. The page must stay responsive while DifMAP jobs
saturate the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from nicegui import run, ui

from lenspipe.ui.layout import MONO, frame
from lenspipe.ui.state import console
from lenspipe.ui.thumbnails import thumbnail_for

MAX_ROWS = 200


@dataclass(frozen=True)
class Target:
    directory: Path
    prefix: str | None = None  # Stage 2 files share the epoch directory; filter by product prefix

    def pngs(self) -> list[Path]:
        candidates = sorted(self.directory.glob("*.png")) + sorted(self.directory.glob("plots/*.png"))
        return [p for p in candidates if self.prefix is None or p.name.startswith(self.prefix)]

    def csvs(self) -> list[Path]:
        candidates = (
            sorted(self.directory.glob("*.csv"))
            + sorted(self.directory.glob("fits/*.csv"))
            + sorted(self.directory.glob("tables/*.csv"))
        )
        return [p for p in candidates if self.prefix is None or p.name.startswith(self.prefix)]


def product_options(summary: dict[str, Any], epoch_key: str, stage: int) -> dict[str, str]:
    """Product choices for the selects; keys are stable identifiers, values labels."""
    if epoch_key.startswith("combined:"):
        source = epoch_key.split(":", 1)[1]
        return {
            (item["product"] or "?") + (f"/{item['analysis_tag']}" if item.get("analysis_tag") else ""): (
                f"{item['product']}" + (f" / {item['analysis_tag']}" if item.get("analysis_tag") else "")
            )
            for item in summary["combined"]
            if item["source"] == source
        }
    row = next((r for r in summary["epochs"] if f"{r['source']}.{r['epoch']}" == epoch_key), None)
    if row is None:
        return {}
    if stage == 2:
        return {p["product"]: p["product"] for p in row["stage2"]}
    return {
        (p["product"] or "?") + (f"/{p['analysis_tag']}" if p["analysis_tag"] else ""): (
            f"{p['product']}" + (f" / {p['analysis_tag']}" if p["analysis_tag"] else "")
        )
        for p in row["stage3"]
    }


def resolve_target(root: Path, epoch_key: str, stage: int, product_key: str) -> Target:
    if epoch_key.startswith("combined:"):
        source = epoch_key.split(":", 1)[1]
        return Target(root / "stage3" / "combined" / source / Path(product_key))
    if stage == 2:
        return Target(root / "stage2" / epoch_key, prefix=f"{epoch_key}.{product_key}.")
    return Target(root / "stage3" / epoch_key / Path(product_key))


def _epoch_options(summary: dict[str, Any]) -> dict[str, str]:
    options: dict[str, str] = {}
    for row in summary["epochs"]:
        if row["stage2"] or row["stage3"]:
            key = f"{row['source']}.{row['epoch']}"
            options[key] = key
    for item in summary["combined"]:
        options.setdefault(f"combined:{item['source']}", f"combined  {item['source']}")
    return options


def _scan(target: Target, root: Path) -> tuple[list[tuple[Path, Path, Path | None]], list[Path]]:
    """Filesystem work for one target, run in a worker thread: figures with thumbnails, and tables."""
    figures = []
    for png in target.pngs():
        pdf = png.with_suffix(".pdf")
        figures.append((png, thumbnail_for(root, png), pdf if pdf.is_file() else None))
    return figures, target.csvs()


def _read_table(csv: Path) -> tuple[pd.DataFrame, int]:
    frame_ = pd.read_csv(csv)
    return frame_.head(MAX_ROWS), len(frame_)


@ui.page("/results")
def results_page() -> None:
    with frame("Results", "/results"):
        state: dict[str, Any] = {"summary": None}
        with ui.row().classes("w-full items-center gap-4"):
            epoch = ui.select({}, label="Epoch").props("dense outlined").classes("w-64")
            stage = ui.select({2: "Stage 2", 3: "Stage 3"}, label="Stage", value=2).props(
                "dense outlined"
            ).classes("w-36")
            product = ui.select({}, label="Product").props("dense outlined").classes("w-72")
            path_label = ui.label().classes(MONO + " opacity-60")
        status = ui.label("Loading products...").classes("text-sm opacity-70")
        content = ui.column().classes("w-full gap-3")

        def sync_products() -> None:
            summary = state["summary"] or {"epochs": [], "combined": []}
            key = epoch.value or ""
            if key.startswith("combined:"):
                stage.set_value(3)
                stage.disable()
            else:
                stage.enable()
            options = product_options(summary, key, int(stage.value or 2))
            product.set_options(options)
            if product.value not in options:
                product.set_value(next(iter(options), None))

        async def render() -> None:
            content.clear()
            if not epoch.value or product.value is None:
                with content:
                    ui.label("No Stage 2 or Stage 3 products yet.").classes("opacity-70")
                path_label.set_text("")
                return
            target = resolve_target(console.root, epoch.value, int(stage.value or 2), str(product.value))
            path_label.set_text(str(target.directory))
            status.set_text("Reading directory...")
            figures, csvs = await run.io_bound(_scan, target, console.root)
            status.set_text("")
            with content:
                if not figures and not csvs:
                    ui.label("Nothing to show in this directory.").classes("opacity-70")
                if figures:
                    ui.label(f"{len(figures)} figure(s); click one for full size").classes("text-subtitle2")
                    with ui.grid().classes("w-full grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3"):
                        for png, thumb, pdf in figures:
                            with ui.card().classes("q-pa-sm gap-1").props("flat bordered"):
                                with ui.link(target=console.file_url(png), new_tab=True):
                                    ui.image(console.file_url(thumb)).classes("w-full").props('loading="lazy"')
                                with ui.row().classes("w-full items-center no-wrap gap-2"):
                                    ui.label(png.name).classes("font-mono text-xs truncate flex-grow")
                                    if pdf is not None:
                                        ui.link("PDF", console.file_url(pdf), new_tab=True).classes("text-xs")
                if csvs:
                    ui.label(f"{len(csvs)} table(s); open a panel to load it").classes("text-subtitle2")
                for csv in csvs:
                    relative = csv.relative_to(target.directory).as_posix()
                    panel = ui.expansion(relative).classes("w-full font-mono text-sm").props("dense")
                    holder = ui.column().classes("w-full gap-1")
                    holder.move(panel)

                    async def load_table(event, csv=csv, holder=holder) -> None:
                        if not event.value or holder.default_slot.children:
                            return
                        with holder:
                            spinner = ui.spinner(size="sm")
                        try:
                            head, total = await run.io_bound(_read_table, csv)
                        except Exception as exc:  # noqa: BLE001 - shown inline
                            spinner.delete()
                            with holder:
                                ui.label(f"Could not read: {exc}").classes("text-negative")
                            return
                        spinner.delete()
                        with holder:
                            with ui.row().classes("w-full items-center gap-3 text-xs"):
                                ui.label(f"{total} rows x {len(head.columns)} columns")
                                if total > MAX_ROWS:
                                    ui.label(f"showing first {MAX_ROWS}").classes("opacity-60")
                                ui.link("Download CSV", console.file_url(csv), new_tab=True)
                            table = ui.table.from_pandas(head, pagination=25)
                            table.props("dense flat bordered").classes("w-full text-xs")

                    panel.on_value_change(load_table)

        async def on_context_change() -> None:
            sync_products()
            await render()

        async def load() -> None:
            state["summary"] = await run.io_bound(console.inventory)
            options = _epoch_options(state["summary"])
            epoch.set_options(options)
            if epoch.value not in options:
                epoch.set_value(next(iter(options), None))
            sync_products()
            await render()

        epoch.on_value_change(on_context_change)
        stage.on_value_change(on_context_change)
        product.on_value_change(render)
        ui.timer(0.0, load, once=True)
