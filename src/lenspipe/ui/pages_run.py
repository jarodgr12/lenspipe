"""Run page: compose stage jobs, preview their command lines, submit them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import run as background
from nicegui import ui

from lenspipe.config import LenspipeConfig, load_config
from lenspipe.stage2 import product_tag
from lenspipe.stage3.io import available_stage2_products
from lenspipe.ui.commands import RunRequest, build_steps, calibrate_step, command_line
from lenspipe.ui.layout import frame
from lenspipe.ui.state import console

FIGURE_FORMATS = ["pdf", "png", "svg"]


def _stage2_products(calibration_mode: str, root: Path | None = None) -> list[str]:
    """Product tags present on disk under ``stage2/<calibration_mode>/`` of the project or a run."""
    try:
        products = available_stage2_products(root or console.root, None, calibration_mode)
    except Exception:  # noqa: BLE001 - "no products yet" is a normal state here
        return []
    return sorted({tag for tags in products.values() for tag in tags})


def _standard_tags(config: LenspipeConfig) -> list[str]:
    """The channel and IF product tags the configured Stage 2 settings would write.

    Both are always offered, so Stage 3 can be pointed at an IF product even when
    the Stage 2 card is set to channel mode (or Stage 2 is not part of the run).
    """
    data = config.stage2.model_dump()
    tags = []
    for mode in ("channel", "if"):
        data["mode"] = mode
        if mode == "if":
            data["channels"] = None
        tags.append(product_tag(type(config.stage2).model_validate(data)))
    return tags


def product_options(on_disk: list[str], expected: str | None, standard: list[str]) -> list[str]:
    """Options for the Stage 2 product select: disk products, the pending tag, and both standard tags."""
    return sorted(set(on_disk) | ({expected} if expected else set()) | set(standard))


def _expected_tag(
    config: LenspipeConfig, mode: str, channels_per_if: int | None, channels: str | None, edge: int | None,
    exclude: str | None = None,
) -> str:
    data = config.stage2.model_dump()
    data["mode"] = mode
    data["exclude_channels"] = exclude or None
    if mode == "channel":
        data["channels"] = channels or None
    else:
        data["channels"] = None
        if channels_per_if:
            data["channels_per_if"] = int(channels_per_if)
        if edge is not None:
            data["exclude_edge_channels"] = int(edge)
    try:
        return product_tag(type(config.stage2).model_validate(data))
    except ValueError:
        return product_tag(config.stage2)


def _override(value: Any, default: Any) -> Any:
    """A per-run value only becomes a flag when it differs from lenspipe.toml."""
    return None if value == default else value


def _int_or_none(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _size_text(size: float | list[float]) -> str:
    """Render the configured cutout size the way the Size field and --image-size expect it."""
    values = [size] if isinstance(size, (int, float)) else list(size)
    return ",".join(f"{float(v):g}" for v in values)


EXCLUSION_EXAMPLES = [
    ("spw3:0-3,60-63", "channels 0-3 and 60-63 of spectral window 3"),
    ("spw0-47:0-7", "channels 0-7 of every window from spw 0 to spw 47 (48 windows are spw0 to spw47)"),
    ("spw7", "all of spectral window 7; spw7-9 for three whole windows"),
    ("spw0-47:0-7; spw3:60-63; spw7", "several items, separated by ';'"),
    ("2817-2880", "a plain number or range is a DifMAP channel (from 1)"),
]
EXCLUSION_TOOLTIP = (
    "Items separated by ';'. 'spw3:0-3,60-63' = CASA spectral window 3, channels 0-3 and 60-63 inside it "
    "(both numbered from 0, as in CASA flagging). 'spw0-47:0-7' = the same channels in windows 0 to 47. "
    "'spw7' = the whole window, 'spw7-9' = three whole windows. A plain number or range is a "
    "Stage 2 fit index (from 1). On an IF product only whole windows can be excluded."
)


def _examples(title: str, rows: list[tuple[str, str]]):
    """A compact hint block: a title line, then monospace example / meaning pairs."""
    with ui.column().classes("gap-0 q-mt-xs") as block:
        ui.label(title).classes("text-caption opacity-70")
        with ui.element("div").classes("grid gap-x-4").style("grid-template-columns: max-content 1fr"):
            for example, meaning in rows:
                ui.label(example).classes("text-caption font-mono")
                ui.label(meaning).classes("text-caption opacity-70")
    return block


def _epoch_exclusions(text: str) -> list[str]:
    """Split per-epoch entries into repeatable EPOCH:SPEC flags.

    Entries are separated by whitespace *between* entries, but a spec may itself
    contain spaces after its semicolons (``E:spw14:0-63; spw15``), so a token
    that does not start a new ``EPOCH:`` entry is glued onto the previous one.
    """
    entries: list[str] = []
    for token in text.split():
        head, separator, _ = token.partition(":")
        starts_entry = bool(separator) and bool(head) and not head.lower().startswith("spw")
        if starts_entry or not entries:
            entries.append(token)
        else:
            entries[-1] = f"{entries[-1]} {token}"
    return entries


@ui.page("/run")
def run_page(run: str | None = None) -> None:
    """``?run=<label>`` composes jobs for that run: its frozen config is the page's baseline."""
    from lenspipe.runs import create_run, validate_label

    with frame("Run", "/run"):
        requested_run = (run or "").strip()
        existing_runs = {item.label: item for item in console.runs()}
        current_run = existing_runs.get(requested_run)
        config_root = current_run.root if current_run else console.root
        try:
            config = load_config(config_root)
        except Exception as exc:  # noqa: BLE001
            ui.label(f"Config could not be loaded; using defaults. {exc}").classes("text-negative")
            config = LenspipeConfig()
        summary = console.inventory()
        epoch_options = {
            item["epoch"]: f"{item['source']}.{item['epoch']}" for item in summary["epochs"]
        }
        cpus = console.cpu_count()
        dense = "dense outlined"

        with ui.row().classes("w-full no-wrap gap-6 items-start"):
            with ui.column().classes("gap-3 flex-1 min-w-0"):
                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
                    ui.label("Run (a separate parameter set with its own outputs)").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        run_label = ui.input(
                            "Run label (type a name, e.g. A)", value=requested_run, placeholder="empty = the project itself",
                        ).props("dense outlined clearable stack-label").classes("w-80")
                        run_label.tooltip(
                            "Type a name for a new run, or pick one on the right. A run is runs/<label>/ inside the "
                            "project: lenspipe.toml is copied into it when it is created (so later edits on the "
                            "Parameters page do not touch queued jobs) and its stage1/, stage2/ and stage3/ are its own. "
                            "Queue one job per label to compare parameter sets overnight."
                        )
                        existing_select = ui.select(
                            {"": "project itself", **{label: label for label in existing_runs}},
                            label="Existing runs", value=requested_run if requested_run in existing_runs else "",
                        ).props("dense outlined").classes("w-56")
                        existing_select.set_visibility(bool(existing_runs))
                        run_note = ui.label().classes("text-caption opacity-70")
                    with ui.expansion(
                        "The run's frozen parameters differ from the Parameters page", icon="difference"
                    ).classes("w-full").props("dense") as run_diff_panel:
                        run_diff_code = ui.code("", language="diff").classes("w-full text-xs")
                        ui.label(
                            "Jobs for this run use the frozen file on the left. The Parameters page edits the "
                            "project's file; to carry those edits into this run, replace its frozen copy:"
                        ).classes("text-caption opacity-70")
                        ui.button(
                            "Replace the run's frozen parameters with the current ones", icon="sync",
                            on_click=lambda: refreeze(),
                        ).props("outline no-caps dense")

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
                    ui.label("Stages").classes("text-subtitle2")
                    with ui.row().classes("gap-6"):
                        stage_boxes = {
                            1: ui.checkbox("Stage 1  self-cal", value=True),
                            2: ui.checkbox("Stage 2  channel fits", value=True),
                            3: ui.checkbox("Stage 3  spectra", value=False),
                        }
                    epochs = ui.select(
                        epoch_options, label="Epochs (empty = all)", multiple=True, value=[]
                    ).props("dense outlined use-chips options-dense").classes("w-full")
                    with ui.row().classes("gap-6 items-center"):
                        overwrite = ui.switch("Overwrite existing products")
                        dry_run = ui.switch("Dry run (print DifMAP commands only)")
                        workers = ui.number(
                            "Epochs in parallel", value=config.run.epoch_workers, min=1, step=1
                        ).props(dense).classes("w-40")
                        ui.label(f"{cpus} CPUs").classes("text-caption opacity-70")

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered") as stage1_card:
                    ui.label("Stage 1").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        calibration_mode = ui.select(
                            {"full-band": "full-band", "per-if": "per-IF"},
                            label="Calibration mode", value=config.stage1.calibration_mode,
                        ).props("dense outlined").classes("w-40")
                        if_count = ui.number("IFs", value=config.stage1.per_if.if_count, min=1, step=1).props(
                            "dense outlined"
                        ).classes("w-24")
                        s1_channels_per_if = ui.number(
                            "Channels per IF", value=config.stage1.per_if.channels_per_if, min=1, step=1
                        ).props("dense outlined").classes("w-36")
                        s1_edge = ui.number(
                            "Edge channels", value=config.stage1.per_if.edge_channels, min=0, step=1
                        ).props("dense outlined").classes("w-32")
                        s1_exclude = ui.input(
                            "Per-IF channels to leave out", value=config.stage1.per_if.exclude_channels or "",
                            placeholder="spw0-47:0-7; spw7",
                        ).props("dense outlined stack-label").classes("w-[26rem]")
                        s1_exclude.tooltip(
                            "Channels left out of each IF's self-cal and per-IF fit in per-IF mode, on top of the "
                            "edge default. " + EXCLUSION_TOOLTIP
                        )
                        tree_note = ui.label().classes("text-caption opacity-70")
                    per_if_examples = _examples(
                        "How to write what to leave out (CASA numbering: spw and channel both start at 0)", EXCLUSION_EXAMPLES
                    )
                    with ui.row().classes("gap-4 items-center"):
                        final_if = ui.switch(
                            "Final per-IF self-cal", value=config.stage1.final_if_selfcal.enabled
                        )
                        final_if_channels = ui.input(
                            "Final-IF channels to leave out",
                            value=config.stage1.final_if_selfcal.exclude_channels or "",
                            placeholder=f"edge {config.stage1.final_if_selfcal.edge_channels} always; e.g. spw3:0-3,60-63; spw7",
                        ).props("dense outlined stack-label").classes("w-[28rem]")
                        final_if_channels.tooltip(
                            "Channels left out of each IF's self-cal, on top of the edge default. " + EXCLUSION_TOOLTIP
                        )
                        reimage = ui.switch("Common-grid imaging pass", value=config.stage1.reimage)
                        reimage.tooltip(
                            "After calibration, image every calibrated epoch again on one shared cell so the maps "
                            "line up across epochs (*.cln.fits). Off keeps only the native maps from the "
                            "calibration pass; Stage 3 map figures then use those."
                        )
                        reimage_only = ui.switch("Re-image only (common grid)", value=False)
                        ui.label(
                            f"{config.stage1.final_if_selfcal.if_count} IFs x "
                            f"{config.stage1.final_if_selfcal.channels_per_if} channels; "
                            "self-cal schedule and unflag are set on the Parameters page"
                        ).classes("text-caption opacity-70")
                    final_if_examples = _examples(
                        "How to write what to leave out (CASA numbering: spw and channel both start at 0)",
                        EXCLUSION_EXAMPLES,
                    )

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered") as stage2_card:
                    ui.label("Stage 2").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        mode = ui.select(["if", "channel"], label="Mode", value=config.stage2.mode).props(
                            dense
                        ).classes("w-32")
                        channels_per_if = ui.number(
                            "Channels per IF", value=config.stage2.channels_per_if, min=1, step=1
                        ).props(dense).classes("w-40")
                        channels = ui.input(
                            "Channels", value=config.stage2.channels or "", placeholder="1-10,15"
                        ).props(dense).classes("w-40")
                        shards = ui.input(
                            "Shards", value=str(config.stage2.shards), placeholder="auto"
                        ).props(dense).classes("w-28")
                        ui.label(f"'auto' or 1-{cpus} ({cpus} CPUs)").classes("text-caption opacity-70")
                    with ui.expansion("Advanced", icon="tune").classes("w-full").props("dense"):
                        with ui.row().classes("gap-4 items-center q-pt-sm"):
                            edge = ui.number(
                                "Edge channels dropped per IF", value=config.stage2.exclude_edge_channels,
                                min=0, step=1,
                            ).props(dense).classes("w-56")
                            iterations = ui.number(
                                "Modelfit iterations", value=config.stage2.modelfit_iterations, min=1, step=1
                            ).props(dense).classes("w-44")
                        with ui.row().classes("gap-4 items-center"):
                            s2_exclude = ui.input(
                                "Channels to leave out", value=config.stage2.exclude_channels or "",
                                placeholder="spw3:0-3,60-63; spw7",
                            ).props(dense + " stack-label").classes("w-[28rem]")
                            s2_exclude.tooltip(
                                "Left out of the fits in either mode; in IF mode a window left with nothing is not "
                                "fitted. " + EXCLUSION_TOOLTIP
                            )
                        _examples("How to write what to leave out (CASA numbering: spw and channel both start at 0)", EXCLUSION_EXAMPLES)
                        with ui.row().classes("gap-6 items-center"):
                            unflag = ui.switch("Unflag before fitting", value=config.stage2.unflag)
                            keep_models = ui.switch("Keep every fitted model", value=config.stage2.keep_models)
                            plots = ui.switch("Quick-look plots", value=config.stage2.plot_spectrum)
                            s2_error_bars = ui.switch("Error bars", value=config.stage2.plot_error_bars)
                        ui.label(
                            "Unflag runs 'unflag *' on the calibrated file before the fits; off keeps "
                            "the flags Stage 1 wrote. Changing it changes every channel flux."
                        ).classes("text-caption opacity-70")

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered") as stage3_card:
                    ui.label("Stage 3").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        error_source = ui.select(
                            ["rms", "difmap"], label="Error source", value=config.stage3.error_source
                        ).props(dense).classes("w-36")
                        product = ui.select(
                            product_options(
                                _stage2_products(config.stage1.calibration_mode), None, _standard_tags(config)
                            ),
                            label="Stage 2 product", with_input=True, new_value_mode="add-unique",
                        ).props(dense).classes("w-56")
                        plot_workers = ui.number(
                            "Plot processes", value=config.run.plot_workers, min=1, step=1
                        ).props(dense).classes("w-36")
                        ui.label(f"{cpus} CPUs").classes("text-caption opacity-70")
                    with ui.expansion("Advanced", icon="tune").classes("w-full").props("dense"):
                        with ui.row().classes("gap-4 items-center q-pt-sm"):
                            fit_method = ui.select(
                                ["least_squares", "emcee"], label="Fit method", value=config.stage3.fit_method
                            ).props(dense).classes("w-44")
                            reference_frequency = ui.number(
                                "Reference frequency (GHz)", value=config.stage3.reference_frequency_ghz,
                                min=0.001, step=0.1, format="%.3f",
                            ).props(dense).classes("w-52")
                            formats = ui.select(
                                FIGURE_FORMATS, label="Figure formats", multiple=True,
                                value=list(config.stage3.figure_formats),
                            ).props("dense outlined use-chips options-dense").classes("w-56")
                        with ui.row().classes("gap-4 items-center"):
                            exclude_channels = ui.input(
                                "Exclude channels (all epochs)", value=config.stage3.exclude_channels or "",
                                placeholder="spw3:0-3,60-63; spw7",
                            ).props(dense + " stack-label").classes("w-72")  # stack-label: the example shows in the empty field
                            exclude_channels.tooltip(EXCLUSION_TOOLTIP)
                            exclude_epoch_channels = ui.input(
                                "Per-epoch exclusions", value=" ".join(config.stage3.exclude_epoch_channels),
                                placeholder="E:spw14:0-63 F:1-4",
                            ).props(dense + " stack-label").classes("w-80")
                            exclude_epoch_channels.tooltip(
                                "One entry per epoch, separated by spaces: EPOCH:spec, where spec uses the "
                                "same syntax as the all-epochs field. " + EXCLUSION_TOOLTIP
                            )
                        with ui.row().classes("gap-6 items-center"):
                            annotations = ui.switch("Annotated spectra", value=config.stage3.annotations)
                            s3_error_bars = ui.switch("Error bars", value=config.stage3.plot_error_bars)
                        with ui.row().classes("gap-12 items-end q-mt-sm"):
                            with ui.column().classes("gap-1 w-56"):
                                point_opacity_label = ui.label().classes("text-caption")
                                point_opacity = ui.slider(min=0.0, max=1.0, step=0.05, value=config.stage3.point_opacity)
                            with ui.column().classes("gap-1 w-56"):
                                bar_opacity_label = ui.label().classes("text-caption")
                                error_bar_opacity = ui.slider(min=0.0, max=1.0, step=0.05, value=config.stage3.error_bar_opacity)
                        ui.label(
                            "Opacity of the data points and of their error bars, set separately (0 = invisible, 1 = solid)"
                        ).classes("text-caption opacity-70")
                        _examples(
                            "How to write exclusions (CASA numbering: spw and channel both start at 0)",
                            [
                                ("spw3:0-3,60-63", "channels 0-3 and 60-63 of spectral window 3"),
                                ("spw7", "all of spectral window 7"),
                                ("spw3:0-3,60-63; spw7; 1-4", "several items, separated by ';' (plain numbers are Stage 2 fit indices, from 1)"),
                                ("E:spw14:0-63; spw15   F:1-4", "per-epoch field: EPOCH:spec, one entry per epoch, separated by spaces"),
                            ],
                        )
                        ui.label("Clean and residual map figures (from the Stage 1 FITS images)").classes(
                            "text-caption q-mt-sm"
                        )
                        with ui.row().classes("gap-4 items-center"):
                            images_on = ui.switch("Map figures", value=config.stage3.images.enabled)
                            image_center = ui.input(
                                "Centre", value=config.stage3.images.center or "",
                                placeholder="image centre, or 64.24,5.57, or 15h58m00s +37d20m00s",
                            ).props(dense).classes("w-80")
                            image_size = ui.input(
                                "Size (arcsec)", value=_size_text(config.stage3.images.size_arcsec),
                                placeholder="2 or 2,1.5",
                            ).props(dense).classes("w-36")
                            image_cmap = ui.input("Colour map", value=config.stage3.images.cmap).props(dense).classes("w-36")
                            image_pmax = ui.number(
                                "Clean pmax", value=config.stage3.images.pmax, min=1, max=100, step=0.1,
                                format="%.1f",
                            ).props(dense).classes("w-32")
                            image_residual_pmax = ui.number(
                                "Residual pmax", value=config.stage3.images.residual_pmax, min=1, max=100,
                                step=0.1, format="%.1f",
                            ).props(dense).classes("w-36")
                            image_reference_epoch = ui.select(
                                {"": "first visit", **{e: e for e in epoch_options}},
                                label="Colour-scale reference visit",
                                value=config.stage3.images.reference_epoch or "",
                            ).props(dense).classes("w-52")

                with ui.row().classes("gap-2 items-center"):
                    submit_button = ui.button("Submit", icon="play_arrow", on_click=lambda: submit()).props(
                        "no-caps"
                    )
                    notes_label = ui.label().classes("text-caption opacity-70")

                if config.casa.interpreter:
                    with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
                        ui.label("Calibrate (CASA)").classes("text-subtitle2")
                        ui.label(
                            f"{config.casa.interpreter} {config.casa.script or '<script unset>'}"
                        ).classes("font-mono text-xs opacity-70")
                        with ui.row().classes("gap-4 items-center"):
                            casa_steps = ui.input("Steps", placeholder="all").props(dense)
                            casa_list = ui.switch("List steps only")
                            ui.button(
                                "Submit calibration", icon="play_arrow", on_click=lambda: submit_casa()
                            ).props("outline no-caps")

            with ui.column().classes("gap-2 w-[420px] shrink-0"):
                ui.label("Command preview").classes("text-subtitle2")
                preview = ui.label().classes(
                    "w-full font-mono text-xs whitespace-pre-wrap break-all q-pa-sm rounded-borders"
                ).style("background: rgba(127,127,127,0.12)")

        def request() -> RunRequest:
            stages = [n for n, box in stage_boxes.items() if box.value]
            mode_value = mode.value or config.stage2.mode
            s2, s3 = config.stage2, config.stage3
            chosen_formats = [f for f in FIGURE_FORMATS if f in (formats.value or [])]
            return RunRequest(
                root=console.root,
                stages=stages,
                run=(run_label.value or "").strip() or None,
                epochs=list(epochs.value or []),
                overwrite=bool(overwrite.value),
                dry_run=bool(dry_run.value),
                workers=_int_or_none(workers.value),
                stage1_final_if_selfcal=_override(bool(final_if.value), config.stage1.final_if_selfcal.enabled),
                stage1_final_if_exclude=_override(
                    (final_if_channels.value or "").strip(), config.stage1.final_if_selfcal.exclude_channels or ""
                ),
                calibration_mode=_override(calibration_mode.value, config.stage1.calibration_mode),
                stage1_if_count=_override(_int_or_none(if_count.value), config.stage1.per_if.if_count),
                stage1_channels_per_if=_override(
                    _int_or_none(s1_channels_per_if.value), config.stage1.per_if.channels_per_if
                ),
                stage1_edge_channels=_override(_int_or_none(s1_edge.value), config.stage1.per_if.edge_channels),
                stage1_per_if_exclude=_override((s1_exclude.value or "").strip(), config.stage1.per_if.exclude_channels or ""),
                stage1_reimage_only=bool(reimage_only.value),
                stage1_reimage=_override(bool(reimage.value), config.stage1.reimage),
                stage2_mode=mode_value,
                stage2_shards=(shards.value or "").strip() or None,
                stage2_channels_per_if=_int_or_none(channels_per_if.value),
                stage2_channels=(channels.value or "").strip() or None,
                stage2_exclude_channels=_override((s2_exclude.value or "").strip(), s2.exclude_channels or ""),
                stage2_exclude_edge_channels=_override(_int_or_none(edge.value), s2.exclude_edge_channels),
                stage2_modelfit_iterations=_override(_int_or_none(iterations.value), s2.modelfit_iterations),
                stage2_unflag=_override(bool(unflag.value), s2.unflag),
                stage2_keep_models=_override(bool(keep_models.value), s2.keep_models),
                stage2_plots=_override(bool(plots.value), s2.plot_spectrum),
                stage2_error_bars=_override(bool(s2_error_bars.value), s2.plot_error_bars),
                stage3_error_source=error_source.value,
                stage3_product=product.value or None,
                stage3_workers=_int_or_none(plot_workers.value),
                stage3_fit_method=_override(fit_method.value, s3.fit_method),
                stage3_reference_frequency=_override(
                    float(reference_frequency.value) if reference_frequency.value else None,
                    s3.reference_frequency_ghz,
                ),
                stage3_exclude_channels=_override((exclude_channels.value or "").strip() or None, s3.exclude_channels),
                stage3_exclude_epoch_channels=(
                    [] if _epoch_exclusions(exclude_epoch_channels.value or "") == list(s3.exclude_epoch_channels)
                    else _epoch_exclusions(exclude_epoch_channels.value or "")
                ),
                stage3_annotations=_override(bool(annotations.value), s3.annotations),
                stage3_error_bars=_override(bool(s3_error_bars.value), s3.plot_error_bars),
                stage3_point_opacity=_override(round(float(point_opacity.value), 2), s3.point_opacity),
                stage3_error_bar_opacity=_override(round(float(error_bar_opacity.value), 2), s3.error_bar_opacity),
                stage3_formats=(
                    ",".join(chosen_formats)
                    if chosen_formats and chosen_formats != list(s3.figure_formats) else None
                ),
                stage3_images=_override(bool(images_on.value), s3.images.enabled),
                stage3_image_center=_override((image_center.value or "").strip() or None, s3.images.center or None),
                stage3_image_size=_override(
                    (image_size.value or "").replace(" ", "") or None, _size_text(s3.images.size_arcsec)
                ),
                stage3_image_cmap=_override((image_cmap.value or "").strip() or None, s3.images.cmap),
                stage3_image_pmax=_override(
                    float(image_pmax.value) if image_pmax.value not in (None, "") else None, s3.images.pmax
                ),
                stage3_image_residual_pmax=_override(
                    float(image_residual_pmax.value) if image_residual_pmax.value not in (None, "") else None,
                    s3.images.residual_pmax,
                ),
                stage3_image_reference_epoch=_override(
                    (image_reference_epoch.value or "") or None, s3.images.reference_epoch or None
                ),
            )

        disk_products: dict[str, list[str]] = {
            config.stage1.calibration_mode: _stage2_products(config.stage1.calibration_mode, config_root)
        }

        def run_state() -> tuple[str | None, str | None]:
            """(label, problem): the chosen run label and why it cannot be used, if it cannot."""
            label = (run_label.value or "").strip()
            if not label:
                return None, None
            try:
                return validate_label(label), None
            except ValueError as exc:
                return label, str(exc)

        def describe_run() -> None:
            label, problem = run_state()
            run_diff_panel.set_visibility(False)
            if problem:
                run_note.set_text(problem)
                run_note.classes(add="text-negative", remove="opacity-70")
                tree_note.set_text("")
                return
            run_note.classes(remove="text-negative", add="opacity-70")
            if label is None:
                run_note.set_text(
                    "Outputs go to the project's own stage1/, stage2/ and stage3/; each job reads lenspipe.toml "
                    "when it starts."
                )
                tree_note.set_text("Stage 2 and 3 read the same mode's tree; results live under stage1/<mode>/")
                return
            tree_note.set_text(f"Stage 2 and 3 read the same mode's tree; results live under runs/{label}/stage1/<mode>/")
            item = existing_runs.get(label)
            if item is None:
                run_note.set_text(
                    f"New run: on submit runs/{label}/ is created with the parameters on the Parameters page "
                    "frozen into it; its outputs stay separate from the project's."
                )
                return
            created = (item.created_utc or "")[:16].replace("T", " ")
            diff = item.config_diff()
            run_note.set_text(
                f"Existing run runs/{label}/ (created {created or 'unknown'}); jobs use its frozen parameters"
                + (", which differ from the Parameters page (see below)." if diff else ", identical to the Parameters page.")
            )
            if diff:
                run_diff_code.set_content(diff)
                run_diff_panel.set_visibility(True)

        def refresh_preview() -> None:
            describe_run()
            point_opacity_label.set_text(f"Point opacity {float(point_opacity.value):.2f}")
            bar_opacity_label.set_text(f"Error-bar opacity {float(error_bar_opacity.value):.2f}")
            stage1_card.set_visibility(stage_boxes[1].value)
            for widget in (if_count, s1_channels_per_if, s1_edge, s1_exclude, per_if_examples):
                widget.set_visibility(calibration_mode.value == "per-if")
            final_if_channels.set_visibility(bool(final_if.value))
            final_if_examples.set_visibility(bool(final_if.value))
            stage2_card.set_visibility(stage_boxes[2].value)
            stage3_card.set_visibility(stage_boxes[3].value)
            channels.set_visibility(mode.value == "channel")
            channels_per_if.set_visibility(mode.value != "channel")
            edge.set_visibility(mode.value != "channel")
            expected = None
            if stage_boxes[2].value:
                expected = _expected_tag(
                    config, mode.value, channels_per_if.value,
                    (channels.value or "").strip() or None, _int_or_none(edge.value),
                    (s2_exclude.value or "").strip() or None,
                )
            current_mode = str(calibration_mode.value or config.stage1.calibration_mode)
            if current_mode not in disk_products:  # one scan per calibration mode per page view
                disk_products[current_mode] = _stage2_products(current_mode, config_root)
            options = product_options(disk_products[current_mode], expected, _standard_tags(config))
            typed = product.value if product.value and product.value not in options else None
            if typed:
                options = sorted({*options, typed})  # keep a tag the user typed by hand
            if options != product.options:
                product.set_options(options)
            if not product.value or product.value not in options:
                product.set_value(expected or (options[0] if options else None))
            steps, notes = build_steps(request())
            preview.set_text(
                "\n\n".join(command_line(step.argv) for step in steps) or "(nothing selected)"
            )
            notes_label.set_text("  ".join(notes))
            submit_button.set_enabled(bool(steps) and run_state()[1] is None)

        def on_run_settled() -> None:
            # Called when the label is confirmed (Enter, leaving the box, or the picker). Another
            # *existing* run means another baseline config for every field, so the page reloads
            # for it; a label that is not a run yet keeps the project's config and just updates the notes.
            label, problem = run_state()
            if problem:
                refresh_preview()
                return
            target = label if label in existing_runs else ""
            if target != (requested_run if requested_run in existing_runs else ""):
                ui.navigate.to(f"/run?run={target}" if target else (f"/run?run={label}" if label else "/run"))
            else:
                refresh_preview()

        def on_pick_existing() -> None:
            label = existing_select.value or ""
            run_label.set_value(label)
            if label != requested_run:
                ui.navigate.to(f"/run?run={label}" if label else "/run")

        async def refreeze() -> None:
            from lenspipe.runs import refreeze_run

            label, problem = run_state()
            if not label or problem:
                return
            await background.io_bound(refreeze_run, console.root, label)
            ui.notify(f"runs/{label}/lenspipe.toml now matches the project's", type="positive")
            ui.navigate.to(f"/run?run={label}")

        for widget in (
            *stage_boxes.values(), epochs, overwrite, dry_run, workers, calibration_mode, if_count,
            s1_channels_per_if, s1_edge, s1_exclude, final_if, final_if_channels, reimage, reimage_only, mode, channels_per_if,
            channels, shards, edge, s2_exclude, iterations, unflag, keep_models, plots, s2_error_bars,
            error_source, product, plot_workers, fit_method, reference_frequency, formats,
            exclude_channels, exclude_epoch_channels, annotations, s3_error_bars, point_opacity, error_bar_opacity,
            images_on, image_center, image_size, image_cmap, image_pmax, image_residual_pmax,
            image_reference_epoch,
        ):
            widget.on_value_change(refresh_preview)
        run_label.on_value_change(refresh_preview)  # live note and preview while typing
        run_label.on("keydown.enter", on_run_settled)
        run_label.on("blur", on_run_settled)
        existing_select.on_value_change(on_pick_existing)
        refresh_preview()

        async def submit() -> None:
            label, problem = run_state()
            if problem:
                ui.notify(problem, type="negative")
                return
            if label:
                # Create the run now, so the parameters frozen into it are the ones on screen at submit,
                # not whatever lenspipe.toml says when the queued job finally starts.
                try:
                    _, created = await background.io_bound(create_run, console.root, label)
                except (ValueError, FileNotFoundError) as exc:
                    ui.notify(str(exc), type="negative")
                    return
                if created:
                    ui.notify(f"Created run runs/{label}/ with the current parameters frozen", type="positive")
            steps, _ = build_steps(request())
            if not steps:
                return
            first = await background.io_bound(console.submit_chain, steps)
            if first is not None:
                ui.navigate.to(f"/jobs?job={first.id}")

        async def submit_casa() -> None:
            step = calibrate_step(console.root, (casa_steps.value or "").strip() or None, bool(casa_list.value))
            first = await background.io_bound(console.submit_chain, [step])
            if first is not None:
                ui.navigate.to(f"/jobs?job={first.id}")
