"""Generate NiceGUI inputs from pydantic models and read them back as plain data.

The form never coerces beyond what the widget type implies; anything else is left
to ``LenspipeConfig.model_validate`` so that validation messages come from one
place. Empty inputs mean "unset": ``None`` for optional fields and "omit, use the
default" for required ones.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from nicegui import ui
from pydantic import BaseModel
from pydantic.fields import FieldInfo

__all__ = ["ModelForm", "classify"]

MISSING = object()
Loc = tuple[Any, ...]


@dataclass
class Kind:
    name: str
    optional: bool = False
    options: list[Any] = field(default_factory=list)
    item: Any = None


def classify(annotation: Any) -> Kind:
    """Map a field annotation onto one of the widget kinds used by the form."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        optional = len(args) < len(typing.get_args(annotation))
        if len(args) == 1:
            kind = classify(args[0])
            kind.optional = optional
            return kind
        literals = [
            value for arg in args if typing.get_origin(arg) is Literal for value in typing.get_args(arg)
        ]
        return Kind("int_or_literal", optional, options=literals)
    if origin is Literal:
        return Kind("literal", options=list(typing.get_args(annotation)))
    if origin is list:
        (item,) = typing.get_args(annotation) or (str,)
        if isinstance(item, type) and issubclass(item, BaseModel):
            return Kind("table", item=item)
        return Kind("list", item=item)
    if origin is tuple:
        return Kind("tuple", item=typing.get_args(annotation))
    if annotation is bool:
        return Kind("bool")
    if annotation is int:
        return Kind("int")
    if annotation is float:
        return Kind("float")
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return Kind("model", item=annotation)
    return Kind("str")


def _title(name: str) -> str:
    return name.replace("_", " ")


@dataclass
class Binding:
    loc: Loc
    kind: Kind
    read: Callable[[], Any]
    write: Callable[[Any], None]
    error: ui.label


class ModelForm:
    """Widgets for one pydantic model, laid out in the current NiceGUI container."""

    def __init__(self, model: type[BaseModel], values: dict[str, Any], loc: Loc = ()) -> None:
        self.model = model
        self.loc = loc
        self.bindings: list[Binding] = []
        self.section_error = ui.label().classes("text-negative text-sm whitespace-pre-line")
        self.section_error.set_visibility(False)
        self._build(model, values, loc)

    # -- building -----------------------------------------------------------

    def _build(self, model: type[BaseModel], values: dict[str, Any], loc: Loc) -> None:
        nested = [(n, f) for n, f in model.model_fields.items() if classify(f.annotation).name in {"model", "table"}]
        plain = [(n, f) for n, f in model.model_fields.items() if classify(f.annotation).name not in {"model", "table"}]
        with ui.grid().classes("w-full grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2"):
            for name, info in plain:
                self._field(name, info, values.get(name, MISSING), loc + (name,))
        for name, info in nested:
            kind = classify(info.annotation)
            with ui.card().classes("w-full q-pa-md gap-2").props("flat bordered"):
                ui.label(_title(name)).classes("text-subtitle2")
                if info.description:
                    ui.label(info.description).classes("text-caption opacity-70")
                if kind.name == "model":
                    self._build(kind.item, values.get(name) or {}, loc + (name,))
                else:
                    self._table(name, kind.item, values.get(name) or [], loc + (name,))

    def _field(self, name: str, info: FieldInfo, value: Any, loc: Loc) -> None:
        kind = classify(info.annotation)
        label = _title(name)
        with ui.column().classes("gap-0"):
            if kind.name == "bool":
                widget = ui.switch(label, value=bool(value) if value is not MISSING else False)
                read = lambda w=widget: bool(w.value)  # noqa: E731
                write = lambda v, w=widget: w.set_value(bool(v))  # noqa: E731
            elif kind.name in {"int", "float"}:
                widget = ui.number(label, value=None if value is MISSING else value).props("dense outlined")
                widget.classes("w-full")
                if kind.name == "int":
                    widget.props("step=1")

                def read(w=widget, k=kind) -> Any:
                    if w.value is None or w.value == "":
                        return None if k.optional else MISSING
                    return int(w.value) if k.name == "int" and float(w.value).is_integer() else w.value

                write = lambda v, w=widget: w.set_value(v)  # noqa: E731
            elif kind.name == "literal":
                options = kind.options
                widget = ui.select(options, label=label, value=value if value in options else options[0])
                widget.props("dense outlined").classes("w-full")
                read = lambda w=widget: w.value  # noqa: E731
                write = lambda v, w=widget: w.set_value(v)  # noqa: E731
            elif kind.name == "tuple":
                arity = len(kind.item)
                current = list(value) if isinstance(value, (list, tuple)) else [None] * arity
                with ui.row().classes("w-full no-wrap gap-2"):
                    parts = [
                        ui.number(f"{label} [{i}]", value=current[i] if i < len(current) else None)
                        .props("dense outlined")
                        .classes("w-full")
                        for i in range(arity)
                    ]

                def read(ps=parts) -> Any:
                    vals = [p.value for p in ps]
                    return MISSING if all(v is None for v in vals) else vals

                def write(v, ps=parts) -> None:
                    for p, item in zip(ps, list(v) if v else [None] * len(ps), strict=False):
                        p.set_value(item)

                widget = parts[0]
            elif kind.name == "list":
                text = ", ".join(str(v) for v in value) if isinstance(value, list) else ""
                widget = ui.input(label, value=text, placeholder="comma-separated").props("dense outlined")
                widget.classes("w-full")

                def read(w=widget, k=kind) -> Any:
                    items = [part.strip() for part in (w.value or "").split(",") if part.strip()]
                    if k.item is float or k.item is int:
                        return [_number(part, k.item) for part in items]
                    return items

                write = lambda v, w=widget: w.set_value(", ".join(str(x) for x in (v or [])))  # noqa: E731
            else:  # str, int_or_literal
                text = "" if value is MISSING or value is None else str(value)
                hint = " or ".join(str(o) for o in kind.options) if kind.options else None
                widget = ui.input(label, value=text, placeholder=hint).props("dense outlined")
                widget.classes("w-full")

                def read(w=widget, k=kind) -> Any:
                    raw = (w.value or "").strip()
                    if not raw:
                        return None if k.optional else MISSING
                    if k.name == "int_or_literal" and raw not in k.options:
                        return _number(raw, int)
                    return raw

                write = lambda v, w=widget: w.set_value("" if v is None else str(v))  # noqa: E731
            if info.description:
                ui.label(info.description).classes("text-caption opacity-70 leading-tight")
            error = ui.label().classes("text-negative text-caption")
            error.set_visibility(False)
        self.bindings.append(Binding(loc, kind, read, write, error))

    def _table(self, name: str, row_model: type[BaseModel], rows: list[dict], loc: Loc) -> None:
        columns = list(row_model.model_fields.items())
        state: list[dict[str, Any]] = [dict(row) for row in rows]
        widgets: list[dict[str, Any]] = []
        holder = ui.column().classes("w-full gap-1")

        def snapshot() -> list[dict[str, Any]]:
            return [{col: w.value for col, w in row.items()} for row in widgets]

        def render() -> None:
            widgets.clear()
            holder.clear()
            with holder:
                with ui.row().classes("w-full no-wrap items-center gap-2 text-caption opacity-70"):
                    ui.label("#").classes("w-6")
                    for col, _ in columns:
                        ui.label(_title(col)).classes("w-36")
                for index, row in enumerate(state):
                    with ui.row().classes("w-full no-wrap items-center gap-2"):
                        ui.label(str(index + 1)).classes("w-6 text-caption")
                        cells: dict[str, Any] = {}
                        for col, info in columns:
                            kind = classify(info.annotation)
                            value = row.get(col, info.get_default(call_default_factory=True))
                            if kind.name == "bool":
                                cells[col] = ui.switch(value=bool(value)).classes("w-36")
                            else:
                                cells[col] = ui.number(value=value).props("dense outlined").classes("w-36")
                        widgets.append(cells)
                        ui.button(icon="delete", on_click=lambda i=index: remove(i)).props(
                            "flat dense round color=negative"
                        )
                ui.button("Add step", icon="add", on_click=add).props("flat dense no-caps")

        def remove(index: int) -> None:
            state[:] = snapshot()
            del state[index]
            render()

        def add() -> None:
            state[:] = snapshot()
            state.append(
                {col: info.get_default(call_default_factory=True) for col, info in columns}
            )
            render()

        def write(value: Any) -> None:
            state[:] = [dict(row) for row in (value or [])]
            render()

        render()
        error = ui.label().classes("text-negative text-caption")
        error.set_visibility(False)
        self.bindings.append(Binding(loc, Kind("table", item=row_model), snapshot, write, error))

    # -- data ---------------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        """Nested plain data with unset fields omitted."""
        data: dict[str, Any] = {}
        for binding in self.bindings:
            value = binding.read()
            if value is MISSING:
                continue
            target = data
            for key in binding.loc[len(self.loc) : -1]:
                target = target.setdefault(key, {})
            target[binding.loc[-1]] = value
        return data

    def set_values(self, values: dict[str, Any]) -> None:
        for binding in self.bindings:
            value: Any = values
            for key in binding.loc[len(self.loc) :]:
                value = value.get(key) if isinstance(value, dict) else None
            binding.write(value)
        self.clear_errors()

    def clear_errors(self) -> None:
        for binding in self.bindings:
            binding.error.set_text("")
            binding.error.set_visibility(False)
        self.section_error.set_text("")
        self.section_error.set_visibility(False)

    def show_errors(self, errors: list[dict[str, Any]]) -> int:
        """Attach pydantic errors to their fields; returns how many belong to this section."""
        self.clear_errors()
        matched = 0
        unmatched: list[str] = []
        for err in errors:
            loc = tuple(err.get("loc", ()))
            if loc[: len(self.loc)] != self.loc:
                continue
            matched += 1
            message = str(err.get("msg", "invalid"))
            hit = _best_binding(self.bindings, loc)
            if hit is None:
                unmatched.append(f"{'.'.join(str(p) for p in loc) or self.model.__name__}: {message}")
                continue
            suffix = loc[len(hit.loc) :]
            if suffix:
                message = f"{'.'.join(str(p) for p in suffix)}: {message}"
            existing = hit.error.text
            hit.error.set_text(f"{existing}\n{message}" if existing else message)
            hit.error.set_visibility(True)
        if unmatched:
            self.section_error.set_text("\n".join(unmatched))
            self.section_error.set_visibility(True)
        return matched


def _best_binding(bindings: list[Binding], loc: Loc) -> Binding | None:
    best: Binding | None = None
    for binding in bindings:
        if loc[: len(binding.loc)] == binding.loc and (best is None or len(binding.loc) > len(best.loc)):
            best = binding
    return best


def _number(text: str, cast: type) -> Any:
    try:
        return cast(text)
    except ValueError:
        return text
