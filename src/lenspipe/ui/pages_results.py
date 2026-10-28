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

    def reports(self) -> list[Path]:
        """Plain-text fit reports (parameters, errors, covariance matrix)."""
        candidates = sorted(self.directory.glob("fits/*.txt")) + sorted(self.directory.glob("tables/*.txt"))
        return [p for p in candidates if self.prefix is None or p.name.startswith(self.prefix)]


def split_epoch_key(epoch_key: str) -> tuple[str, str, str]:
    """``'<mode>/<source>.<epoch>'`` -> ("epoch", mode, prefix); ``'combined:<mode>:<source>'`` -> ("combined", mode, source).

    A key without a mode (pre-2.1 links such as ``MG0414.A`` or ``combined:MG0414``) is
    read as ``full-band`` so old bookmarks still resolve.
    """
    if epoch_key.startswith("combined:"):
        parts = epoch_key.split(":", 2)
        if len(parts) == 3:
            return "combined", parts[1], parts[2]
        return "combined", "full-band", parts[1]
    mode, sep, prefix = epoch_key.partition("/")
    if not sep:
        return "epoch", "full-band", epoch_key
    return "epoch", mode, prefix


def product_options(summary: dict[str, Any], epoch_key: str, stage: int) -> dict[str, str]:
    """Product choices for the selects; keys are stable identifiers, values labels."""
    kind, mode, name = split_epoch_key(epoch_key)
    if kind == "combined":
        return {
            (item["product"] or "?") + (f"/{item['analysis_tag']}" if item.get("analysis_tag") else ""): (
                f"{item['product']}" + (f" / {item['analysis_tag']}" if item.get("analysis_tag") else "")
            )
            for item in summary["combined"]
            if item["source"] == name and (item.get("calibration_mode") or "full-band") == mode
        }
    row = next(
        (
            r for r in summary["epochs"]
            if f"{r['source']}.{r['epoch']}" == name and (r.get("calibration_mode") or "full-band") == mode
        ),
        None,
    )
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
    kind, mode, name = split_epoch_key(epoch_key)
    if kind == "combined":
        return Target(root / "stage3" / mode / "combined" / name / Path(product_key))
    if stage == 2:
        return Target(root / "stage2" / mode / name, prefix=f"{name}.{product_key}.")
    return Target(root / "stage3" / mode / name / Path(product_key))


def _epoch_options(summary: dict[str, Any]) -> dict[str, str]:
    options: dict[str, str] = {}
    for row in summary["epochs"]:
        if row["stage2"] or row["stage3"]:
            mode = row.get("calibration_mode") or "full-band"
            prefix = f"{row['source']}.{row['epoch']}"
            options[f"{mode}/{prefix}"] = f"{prefix}  [{mode}]"
    for item in summary["combined"]:
        mode = item.get("calibration_mode") or "full-band"
        options.setdefault(f"combined:{mode}:{item['source']}", f"combined  {item['source']}  [{mode}]")
    return options


def _scan(target: Target, root: Path) -> tuple[list[tuple[Path, Path, Path | None]], list[Path], list[Path]]:
    """Filesystem work for one target, run in a worker thread: figures with thumbnails, tables, text reports."""
    figures = []
    for png in target.pngs():
        pdf = png.with_suffix(".pdf")
        figures.append((png, thumbnail_for(root, png), pdf if pdf.is_file() else None))
    return figures, target.csvs(), target.reports()


def _read_table(csv: Path) -> tuple[pd.DataFrame, int]:
    frame_ = pd.read_csv(csv)
    return frame_.head(MAX_ROWS), len(frame_)


def _interactive_figures(directory: Path) -> dict[str, dict[str, Any]]:
    """Plotly figures for a Stage 3 directory: per-visit spectra and ratios, or the combined set."""
    from lenspipe.ui.interactive import combined_figures, load_visit, ratio_figure, spectrum_figure

    if next(directory.glob("*.combined.stage3.metadata.json"), None) is not None:
        return combined_figures(directory)
    if next(directory.glob("*.stage3.metadata.json"), None) is None:
        return {}
    visit = load_visit(directory)
    figures = {"Spectra": spectrum_figure(visit)}
    ratios = ratio_figure(visit)
    if ratios is not None:
        figures["Flux ratios"] = ratios
    return figures


def _interactive_notes(directory: Path) -> list[str]:
    """Why an expected figure is absent, e.g. R_cusp skipped because the image names do not match."""
    from lenspipe.ui.interactive import combined_notes

    if next(directory.glob("*.combined.stage3.metadata.json"), None) is None:
        return []
    return combined_notes(directory)


def _interactive_bundle(directory: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    return _interactive_figures(directory), _interactive_notes(directory)


@ui.page("/results")
def results_page(epoch: str | None = None, stage: int = 2, product: str | None = None) -> None:
    """Browse products. Query parameters preselect a view, e.g. ``/results?epoch=MG0414.A&stage=3``."""
    requested = {"epoch": epoch, "stage": 3 if int(stage) == 3 else 2, "product": product}
    with frame("Results", "/results"):
        state: dict[str, Any] = {"summary": None}
        with ui.row().classes("w-full items-center gap-4"):
            epoch = ui.select({}, label="Epoch").props("dense outlined").classes("w-64")
            stage = ui.select({2: "Stage 2", 3: "Stage 3"}, label="Stage", value=requested["stage"]).props(
                "dense outlined"
            ).classes("w-36")
            product = ui.select({}, label="Product").props("dense outlined").classes("w-72")
            path_label = ui.label().classes(MONO + " opacity-60")
        status = ui.label("Loading products...").classes("text-sm opacity-70")
        content = ui.column().classes("w-full gap-3")

        def sync_products() -> None:
            summary = state["summary"] or {"epochs": [], "combined": []}
            key = epoch.value or ""
            if split_epoch_key(key)[0] == "combined":
                stage.set_value(3)
                stage.disable()
            else:
                stage.enable()
            options = product_options(summary, key, int(stage.value or 2))
            product.set_options(options)
            if product.value not in options:
                product.set_value(next(iter(options), None))

        async def render() -> None:
            # Several widgets change value during load and each triggers a render; only the
            # latest one may draw, otherwise the figures appear once per overlapping call.
            state["render"] = state.get("render", 0) + 1
            my_render = state["render"]
            if not epoch.value or product.value is None:
                content.clear()
                with content:
                    ui.label("No Stage 2 or Stage 3 products yet.").classes("opacity-70")
                path_label.set_text("")
                return
            target = resolve_target(console.root, epoch.value, int(stage.value or 2), str(product.value))
            path_label.set_text(str(target.directory))
            status.set_text("Reading directory...")
            figures, csvs, reports = await run.io_bound(_scan, target, console.root)
            interactive: dict[str, dict[str, Any]] = {}
            interactive_notes: list[str] = []
            interactive_error: str | None = None
            if int(stage.value or 2) == 3:
                try:
                    interactive, interactive_notes = await run.io_bound(_interactive_bundle, target.directory)
                except Exception as exc:  # noqa: BLE001 - static figures still show
                    interactive_error = f"{type(exc).__name__}: {exc}"
            if my_render != state["render"]:
                return
            status.set_text("")
            content.clear()
            with content:
                if interactive:
                    with ui.expansion("Interactive (hover a point for spw and channel)", icon="insights", value=True).classes("w-full"):
                        for title, figure in interactive.items():
                            ui.label(title).classes("text-subtitle2 q-mt-sm")
                            ui.plotly(figure).classes("w-full").style("height: 420px")
                elif interactive_error:
                    ui.label(f"Interactive view unavailable: {interactive_error}").classes("text-warning text-sm")
                for note in interactive_notes:
                    ui.label(note).classes("text-sm text-amber-9")
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
                if reports:
                    ui.label(f"{len(reports)} fit report(s): parameters, errors and covariance matrices").classes("text-subtitle2")
                for report in reports:
                    relative = report.relative_to(target.directory).as_posix()
                    with ui.expansion(relative, icon="description").classes("w-full").props("dense") as report_panel:
                        with ui.row().classes("w-full justify-end"):
                            ui.link("open file", console.file_url(report), new_tab=True).classes("text-xs")
                        ui.code(report.read_text(encoding="utf-8", errors="replace"), language="text").classes(
                            "w-full text-xs"
                        )
                    if relative.endswith(".fits.txt") or relative.endswith("combined_fits.txt"):
                        report_panel.open()
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
            wanted_epoch = requested["epoch"]
            if wanted_epoch and wanted_epoch not in options:
                # Accept a key without a mode (older links): map it onto the full-band tree.
                kind, mode, name = split_epoch_key(wanted_epoch)
                wanted_epoch = f"combined:{mode}:{name}" if kind == "combined" else f"{mode}/{name}"
            if wanted_epoch in options:
                epoch.set_value(wanted_epoch)
            elif epoch.value not in options:
                epoch.set_value(next(iter(options), None))
            sync_products()
            if requested["product"] in (product.options or {}):
                product.set_value(requested["product"])
            await render()

        epoch.on_value_change(on_context_change)
        stage.on_value_change(on_context_change)
        product.on_value_change(render)
        ui.timer(0.0, load, once=True)
