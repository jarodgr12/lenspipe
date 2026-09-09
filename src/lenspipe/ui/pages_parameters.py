"""Parameters page: edit lenspipe.toml through forms generated from the config schema."""

from __future__ import annotations

import difflib

import tomli_w
from nicegui import ui
from pydantic import ValidationError

from lenspipe.config import CONFIG_FILENAME, LenspipeConfig
from lenspipe.ui.forms import ModelForm
from lenspipe.ui.layout import MONO, frame
from lenspipe.ui.state import console

SECTIONS = ["project", "stage1", "stage2", "stage3", "run", "casa"]


def render_toml(config: LenspipeConfig) -> str:
    """TOML text for a validated configuration (``None`` cannot be written to TOML)."""
    return tomli_w.dumps(config.model_dump(mode="json", exclude_none=True))


def unified_diff(before: str, after: str, filename: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), f"{filename} (current)", f"{filename} (new)", lineterm=""
    )
    return "\n".join(lines)


@ui.page("/parameters")
def parameters_page() -> None:
    with frame("Parameters", "/parameters"):
        config_path = console.layout.config_path
        load_error = console.config_error()
        if load_error:
            ui.label(f"{CONFIG_FILENAME} could not be parsed; showing defaults. {load_error}").classes(
                "text-negative"
            )
            current = LenspipeConfig()
        else:
            current = console.config()
        values = current.model_dump(mode="json")

        with ui.row().classes("w-full items-center justify-between"):
            ui.label(str(config_path)).classes(MONO + " opacity-70")
            with ui.row().classes("gap-2"):
                ui.button("Reset to defaults", on_click=lambda: reset()).props("outline no-caps")
                ui.button("Save", icon="save", on_click=lambda: save()).props("no-caps")

        status = ui.label().classes("text-sm")
        status.set_visibility(False)

        forms: dict[str, ModelForm] = {}
        with ui.tabs(value="stage1").classes("w-full").props("dense align=left no-caps") as tabs:
            for name in SECTIONS:
                ui.tab(name)
        with ui.tab_panels(tabs, value="stage1").classes("w-full"):
            for name in SECTIONS:
                with ui.tab_panel(name).classes("p-2"):
                    model = LenspipeConfig.model_fields[name].annotation
                    forms[name] = ModelForm(model, values.get(name) or {}, loc=(name,))

        def collect() -> dict:
            return {name: form.collect() for name, form in forms.items()}

        def show_status(text: str, negative: bool = False) -> None:
            status.set_text(text)
            status.classes(remove="text-negative text-positive", add="text-negative" if negative else "text-positive")
            status.set_visibility(True)

        def validate() -> LenspipeConfig | None:
            data = collect()
            try:
                config = LenspipeConfig.model_validate(data)
            except ValidationError as exc:
                errors = exc.errors()
                first_section = None
                for name, form in forms.items():
                    if form.show_errors(errors) and first_section is None:
                        first_section = name
                if first_section:
                    tabs.set_value(first_section)
                show_status(f"{len(errors)} validation error(s); see highlighted fields.", negative=True)
                return None
            for form in forms.values():
                form.clear_errors()
            return config

        def save() -> None:
            config = validate()
            if config is None:
                return
            new_text = render_toml(config)
            try:
                old_text = config_path.read_text(encoding="utf-8")
            except OSError:
                old_text = ""
            diff = unified_diff(old_text, new_text, CONFIG_FILENAME)
            with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full"):
                ui.label(f"Write {CONFIG_FILENAME}").classes("text-subtitle1")
                if diff:
                    ui.code(diff, language="diff").classes("w-full max-h-[60vh] overflow-auto text-xs")
                else:
                    ui.label("No changes compared with the file on disk.").classes("opacity-70")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat no-caps")

                    def write() -> None:
                        try:
                            config_path.write_text(new_text, encoding="utf-8")
                        except OSError as exc:
                            show_status(f"Could not write {config_path}: {exc}", negative=True)
                        else:
                            show_status(f"Wrote {config_path}")
                            ui.notify(f"Saved {CONFIG_FILENAME}", type="positive")
                        dialog.close()

                    ui.button("Write", icon="save", on_click=write).props("no-caps")
            dialog.open()

        def reset() -> None:
            defaults = LenspipeConfig().model_dump(mode="json")
            for name, form in forms.items():
                form.set_values(defaults.get(name) or {})
            show_status("Defaults loaded into the form; nothing written yet.")
