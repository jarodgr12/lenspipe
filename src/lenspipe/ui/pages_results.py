"""Results page: browse Stage 2 / Stage 3 plots and tables for one epoch or the combined set."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from nicegui import run, ui

from lenspipe.ui.layout import MONO, frame
from lenspipe.ui.state import console

MAX_ROWS = 500


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


@ui.page("/results")
def results_page() -> None:
    with frame("Results", "/results"):
        summary = console.inventory()
        epoch_options: dict[str, str] = {}
        for row in summary["epochs"]:
            if row["stage2"] or row["stage3"]:
                key = f"{row['source']}.{row['epoch']}"
                epoch_options[key] = key
        for item in summary["combined"]:
            epoch_options.setdefault(f"combined:{item['source']}", f"combined  {item['source']}")

        with ui.row().classes("w-full items-center gap-4"):
            epoch = ui.select(epoch_options, label="Epoch", value=next(iter(epoch_options), None)).props(
                "dense outlined"
            ).classes("w-64")
            stage = ui.select({2: "Stage 2", 3: "Stage 3"}, label="Stage", value=2).props(
                "dense outlined"
            ).classes("w-36")
            product = ui.select({}, label="Product").props("dense outlined").classes("w-72")
            path_label = ui.label().classes(MONO + " opacity-60")
        content = ui.column().classes("w-full gap-3")

        def sync_products() -> None:
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
            pngs, csvs = target.pngs(), target.csvs()
            with content:
                if not pngs and not csvs:
                    ui.label("Nothing to show in this directory.").classes("opacity-70")
                if pngs:
                    ui.label(f"{len(pngs)} figure(s)").classes("text-subtitle2")
                    with ui.grid().classes("w-full grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3"):
                        for png in pngs:
                            with ui.card().classes("q-pa-sm gap-1").props("flat bordered"):
                                ui.image(console.file_url(png)).classes("w-full")
                                with ui.row().classes("w-full items-center no-wrap gap-2"):
                                    ui.label(png.name).classes("font-mono text-xs truncate flex-grow")
                                    pdf = png.with_suffix(".pdf")
                                    if pdf.is_file():
                                        ui.link("PDF", console.file_url(pdf), new_tab=True).classes("text-xs")
                if csvs:
                    ui.label(f"{len(csvs)} table(s)").classes("text-subtitle2")
                for csv in csvs:
                    relative = csv.relative_to(target.directory).as_posix()
                    with ui.expansion(relative).classes("w-full").props("dense") as panel:
                        panel.classes("font-mono text-sm")
                        try:
                            frame_ = await run.io_bound(pd.read_csv, csv)
                        except Exception as exc:  # noqa: BLE001 - shown inline
                            ui.label(f"Could not read: {exc}").classes("text-negative")
                            continue
                        with ui.row().classes("w-full items-center gap-3 text-xs"):
                            ui.label(f"{len(frame_)} rows x {len(frame_.columns)} columns")
                            if len(frame_) > MAX_ROWS:
                                ui.label(f"showing first {MAX_ROWS}").classes("opacity-60")
                            ui.link("Download CSV", console.file_url(csv), new_tab=True)
                        table = ui.table.from_pandas(frame_.head(MAX_ROWS), pagination=25)
                        table.props("dense flat bordered").classes("w-full text-xs")

        async def on_context_change() -> None:
            sync_products()
            await render()

        epoch.on_value_change(on_context_change)
        stage.on_value_change(on_context_change)
        product.on_value_change(render)
        sync_products()
        ui.timer(0.0, render, once=True)
