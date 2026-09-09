"""Run page: compose stage jobs, preview their command lines, submit them."""

from __future__ import annotations

from nicegui import run, ui

from lenspipe.config import LenspipeConfig
from lenspipe.stage2 import product_tag
from lenspipe.stage3.io import available_stage2_products
from lenspipe.ui.commands import RunRequest, build_steps, calibrate_step, command_line
from lenspipe.ui.layout import frame
from lenspipe.ui.state import console


def _stage2_products() -> list[str]:
    try:
        products = available_stage2_products(console.root)
    except Exception:  # noqa: BLE001 - "no products yet" is a normal state here
        return []
    return sorted({tag for tags in products.values() for tag in tags})


def _expected_tag(config: LenspipeConfig, mode: str, channels_per_if: int | None, channels: str | None) -> str:
    data = config.stage2.model_dump()
    data["mode"] = mode
    if mode == "channel":
        data["channels"] = channels or None
    else:
        data["channels"] = None
        if channels_per_if:
            data["channels_per_if"] = int(channels_per_if)
    try:
        return product_tag(type(config.stage2).model_validate(data))
    except ValueError:
        return product_tag(config.stage2)


@ui.page("/run")
def run_page() -> None:
    with frame("Run", "/run"):
        try:
            config = console.config()
        except Exception as exc:  # noqa: BLE001
            ui.label(f"Config could not be loaded; using defaults. {exc}").classes("text-negative")
            config = LenspipeConfig()
        summary = console.inventory()
        epoch_options = {
            item["epoch"]: f"{item['source']}.{item['epoch']}" for item in summary["epochs"]
        }
        cpus = console.cpu_count()

        with ui.row().classes("w-full no-wrap gap-6 items-start"):
            with ui.column().classes("gap-3 flex-1 min-w-0"):
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
                        ).props("dense outlined").classes("w-40")
                        ui.label(f"{cpus} CPUs").classes("text-caption opacity-70")

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered") as stage2_card:
                    ui.label("Stage 2").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        mode = ui.select(["if", "channel"], label="Mode", value=config.stage2.mode).props(
                            "dense outlined"
                        ).classes("w-32")
                        channels_per_if = ui.number(
                            "Channels per IF", value=config.stage2.channels_per_if, min=1, step=1
                        ).props("dense outlined").classes("w-40")
                        channels = ui.input(
                            "Channels", value=config.stage2.channels or "", placeholder="1-10,15"
                        ).props("dense outlined").classes("w-40")
                        shards = ui.input(
                            "Shards", value=str(config.stage2.shards), placeholder="auto"
                        ).props("dense outlined").classes("w-28")
                        ui.label(f"'auto' or 1-{cpus} ({cpus} CPUs)").classes("text-caption opacity-70")

                with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered") as stage3_card:
                    ui.label("Stage 3").classes("text-subtitle2")
                    with ui.row().classes("gap-4 items-center"):
                        error_source = ui.select(
                            ["rms", "difmap"], label="Error source", value=config.stage3.error_source
                        ).props("dense outlined").classes("w-36")
                        product = ui.select(
                            _stage2_products(), label="Stage 2 product", with_input=True,
                            new_value_mode="add-unique",
                        ).props("dense outlined").classes("w-56")
                        plot_workers = ui.number(
                            "Plot processes", value=config.run.plot_workers, min=1, step=1
                        ).props("dense outlined").classes("w-36")
                        ui.label(f"{cpus} CPUs").classes("text-caption opacity-70")

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
                            casa_steps = ui.input("Steps", placeholder="all").props("dense outlined")
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
            return RunRequest(
                root=console.root,
                stages=stages,
                epochs=list(epochs.value or []),
                overwrite=bool(overwrite.value),
                dry_run=bool(dry_run.value),
                workers=int(workers.value) if workers.value else None,
                stage2_mode=mode_value,
                stage2_shards=(shards.value or "").strip() or None,
                stage2_channels_per_if=int(channels_per_if.value) if channels_per_if.value else None,
                stage2_channels=(channels.value or "").strip() or None,
                stage3_error_source=error_source.value,
                stage3_product=product.value or None,
                stage3_workers=int(plot_workers.value) if plot_workers.value else None,
            )

        def refresh_preview() -> None:
            stage2_card.set_visibility(stage_boxes[2].value)
            stage3_card.set_visibility(stage_boxes[3].value)
            channels.set_visibility(mode.value == "channel")
            channels_per_if.set_visibility(mode.value != "channel")
            if stage_boxes[2].value:
                expected = _expected_tag(
                    config, mode.value, channels_per_if.value, (channels.value or "").strip() or None
                )
                options = sorted(set(product.options) | {expected})
                if options != product.options:
                    product.set_options(options)
                if not product.value or product.value not in options:
                    product.set_value(expected)
            elif not product.value and product.options:
                product.set_value(product.options[0])
            steps, notes = build_steps(request())
            preview.set_text(
                "\n\n".join(command_line(step.argv) for step in steps) or "(nothing selected)"
            )
            notes_label.set_text("  ".join(notes))
            submit_button.set_enabled(bool(steps))

        for widget in (
            *stage_boxes.values(), epochs, overwrite, dry_run, workers, mode, channels_per_if,
            channels, shards, error_source, product, plot_workers,
        ):
            widget.on_value_change(refresh_preview)
        refresh_preview()

        async def submit() -> None:
            steps, _ = build_steps(request())
            if not steps:
                return
            first = await run.io_bound(console.submit_chain, steps)
            if first is not None:
                ui.navigate.to(f"/jobs?job={first.id}")

        async def submit_casa() -> None:
            step = calibrate_step(console.root, (casa_steps.value or "").strip() or None, bool(casa_list.value))
            first = await run.io_bound(console.submit_chain, [step])
            if first is not None:
                ui.navigate.to(f"/jobs?job={first.id}")
