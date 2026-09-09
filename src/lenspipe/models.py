"""DifMAP model files and the GROUP/COMPONENT hierarchy.

A master model is an ordinary DifMAP ``.gmod`` file whose data rows are each
preceded by two comment lines::

    ! GROUP A1
    ! COMPONENT A1a
    <DifMAP model row>

``GROUP`` names a lensed image; ``COMPONENT`` names one model row within it.
DifMAP discards comments when it writes a model, so the hierarchy is restored
from metadata after every fit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ModelComponent",
    "ModelFormatError",
    "ModelHierarchy",
    "is_model_data_line",
    "label_fitted_model",
    "model_component_fluxes",
    "parse_master_model",
    "parse_numeric_token",
    "safe_label",
    "transform_model_line",
    "write_flux_only_model",
]

_GROUP_PATTERN = re.compile(r"^!\s*GROUP\s+(\S+)\s*$", re.IGNORECASE)
_COMPONENT_PATTERN = re.compile(r"^!\s*COMPONENT\s+(\S+)\s*$", re.IGNORECASE)
_LABEL_LINE = re.compile(r"^!\s*(GROUP|COMPONENT)\b", re.IGNORECASE)


class ModelFormatError(ValueError):
    """Raised when a model does not follow the labelled format."""


@dataclass(frozen=True)
class ModelComponent:
    group: str
    name: str
    row_index: int


@dataclass(frozen=True)
class ModelHierarchy:
    components: tuple[ModelComponent, ...]

    @property
    def groups(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for component in self.components:
            grouped.setdefault(component.group, []).append(component.name)
        return grouped

    @property
    def component_names(self) -> list[str]:
        return [component.name for component in self.components]

    @property
    def group_indices(self) -> list[tuple[str, list[int]]]:
        """Return ``(group_name, [row_index, ...])`` in first-appearance order."""
        ordered: dict[str, list[int]] = {}
        for component in self.components:
            ordered.setdefault(component.group, []).append(component.row_index)
        return list(ordered.items())

    def to_records(self) -> list[dict[str, object]]:
        return [
            {"name": c.name, "group": c.group, "row_index": c.row_index}
            for c in self.components
        ]

    @classmethod
    def from_records(cls, records: list[dict[str, object]]) -> ModelHierarchy:
        components = sorted(records, key=lambda item: int(item["row_index"]))
        expected = list(range(len(components)))
        actual = [int(item["row_index"]) for item in components]
        if actual != expected:
            raise ModelFormatError("Component row indices are not contiguous from zero.")
        names = [str(item["name"]) for item in components]
        if len(names) != len(set(names)):
            raise ModelFormatError("Duplicate component names in hierarchy records.")
        return cls(
            tuple(
                ModelComponent(str(item["group"]), str(item["name"]), int(item["row_index"]))
                for item in components
            )
        )


def is_model_data_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("!")


def safe_label(name: str) -> str:
    """Convert a model/group label to a safe CSV field fragment."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip()).strip("_")
    if not cleaned:
        raise ValueError(f"Invalid empty label derived from: {name!r}")
    return cleaned


def parse_master_model(path: Path) -> ModelHierarchy:
    """Parse and validate GROUP/COMPONENT labels in a master model."""
    lines = path.read_text(encoding="utf-8").splitlines()

    current_group: str | None = None
    pending_component: str | None = None
    components: list[ModelComponent] = []
    group_names: set[str] = set()
    component_names: set[str] = set()
    row_index = 0

    for line_number, line in enumerate(lines, start=1):
        group_match = _GROUP_PATTERN.match(line.strip())
        if group_match:
            if pending_component is not None:
                raise ModelFormatError(
                    f"Line {line_number}: COMPONENT '{pending_component}' has no model row."
                )
            current_group = group_match.group(1)
            if current_group in group_names:
                raise ModelFormatError(f"Line {line_number}: duplicate GROUP '{current_group}'.")
            group_names.add(current_group)
            continue

        component_match = _COMPONENT_PATTERN.match(line.strip())
        if component_match:
            if current_group is None:
                raise ModelFormatError(f"Line {line_number}: COMPONENT appears before any GROUP.")
            if pending_component is not None:
                raise ModelFormatError(
                    f"Line {line_number}: COMPONENT '{pending_component}' has no model row."
                )
            pending_component = component_match.group(1)
            if pending_component in component_names:
                raise ModelFormatError(
                    f"Line {line_number}: duplicate COMPONENT '{pending_component}'."
                )
            component_names.add(pending_component)
            continue

        if is_model_data_line(line):
            if current_group is None or pending_component is None:
                raise ModelFormatError(
                    f"Line {line_number}: every model row must follow GROUP and COMPONENT labels."
                )
            components.append(ModelComponent(current_group, pending_component, row_index))
            row_index += 1
            pending_component = None

    if pending_component is not None:
        raise ModelFormatError(f"COMPONENT '{pending_component}' has no model row.")
    if not components:
        raise ModelFormatError("No labelled model components were found.")

    empty_groups = group_names - {component.group for component in components}
    if empty_groups:
        raise ModelFormatError(
            "GROUP(s) without components: " + ", ".join(sorted(empty_groups))
        )

    return ModelHierarchy(tuple(components))


def label_fitted_model(path: Path, hierarchy: ModelHierarchy) -> None:
    """Reinsert GROUP/COMPONENT labels into a DifMAP-written model, in place."""
    lines = path.read_text(encoding="utf-8").splitlines()
    data_indices = [i for i, line in enumerate(lines) if is_model_data_line(line)]
    if len(data_indices) != len(hierarchy.components):
        raise ModelFormatError(
            f"DifMAP output contains {len(data_indices)} rows, but the master "
            f"model defines {len(hierarchy.components)} components."
        )

    data_index_set = set(data_indices)
    output_lines: list[str] = []
    component_number = 0
    previous_group: str | None = None
    for index, line in enumerate(lines):
        if index not in data_index_set:
            if _LABEL_LINE.match(line.strip()):
                continue
            output_lines.append(line)
            continue

        component = hierarchy.components[component_number]
        if component.group != previous_group:
            output_lines.append(f"! GROUP {component.group}")
            previous_group = component.group
        output_lines.append(f"! COMPONENT {component.name}")
        output_lines.append(line)
        component_number += 1

    path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------
# Numeric rows


def parse_numeric_token(token: str) -> float:
    """Parse a DifMAP numeric token, stripping a trailing v/V and Fortran D exponents."""
    cleaned = token.strip()
    if cleaned.lower().endswith("v"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.replace("D", "E").replace("d", "e")
    return float(cleaned)


def model_component_fluxes(model_path: Path) -> list[float]:
    """Return first-column flux densities for all non-comment model rows."""
    fluxes: list[float] = []
    for line in model_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or line.lstrip().startswith("!"):
            continue
        fields = stripped.split()
        if not fields:
            continue
        try:
            fluxes.append(parse_numeric_token(fields[0]))
        except ValueError as exc:
            raise ValueError(
                f"Could not parse the first field as flux density in {model_path}: {line}"
            ) from exc
    if not fluxes:
        raise ValueError(f"No model components found in {model_path}")
    return fluxes


# ----------------------------------------------------------------------------
# Flux-only (Stage 2) model


def _strip_variable_suffix(token: str) -> str:
    return token[:-1] if token.lower().endswith("v") else token


def _make_variable(token: str) -> str:
    return _strip_variable_suffix(token) + "v"


def transform_model_line(line: str) -> tuple[str, bool]:
    """Convert one DifMAP model row into a flux-only variable component.

    DifMAP rows contain up to nine standard fields::

        Flux Radius Theta Major Ratio Phi T [Freq SpecIndex]

    Flux is made variable, structural fields are fixed, the component type is
    preserved, and an explicit spectral index is fixed to zero. Short-form
    delta rows are supported without treating their last field as SpecIndex.
    Returns ``(new_line, changed)``.
    """
    if not line.strip() or line.lstrip().startswith("!"):
        return line, False

    newline = ""
    body = line
    if body.endswith("\r\n"):
        newline, body = "\r\n", body[:-2]
    elif body.endswith("\n"):
        newline, body = "\n", body[:-1]

    matches = list(re.finditer(r"\S+", body))
    if not matches:
        return line, False
    tokens = [match.group(0) for match in matches]

    for index, token in enumerate(tokens[:9]):
        cleaned = _strip_variable_suffix(token).replace("D", "E").replace("d", "e")
        try:
            float(cleaned)
        except ValueError as exc:
            raise ValueError(
                f"Non-numeric field {index + 1} in DifMAP model row: {line.rstrip()}"
            ) from exc

    replacements: dict[int, str] = {0: _make_variable(tokens[0])}
    for index in range(1, min(6, len(tokens))):
        replacements[index] = _strip_variable_suffix(tokens[index])
    if len(tokens) >= 7:
        replacements[6] = _strip_variable_suffix(tokens[6])
    if len(tokens) >= 8:
        replacements[7] = _strip_variable_suffix(tokens[7])
    if len(tokens) >= 9:
        replacements[8] = "0"

    output = body
    for index in sorted(replacements, reverse=True):
        match = matches[index]
        output = output[: match.start()] + replacements[index] + output[match.end() :]

    transformed = output + newline
    return transformed, transformed != line


def write_flux_only_model(source_model: Path, destination: Path) -> tuple[int, int]:
    """Write the flux-only variant of ``source_model`` to ``destination``.

    Returns ``(component_line_count, modified_line_count)``.
    """
    if not source_model.is_file():
        raise FileNotFoundError(f"Model not found: {source_model}")
    if source_model.resolve() == destination.resolve():
        raise ValueError("Source and destination model paths must differ.")

    text = source_model.read_text(encoding="utf-8")
    transformed: list[str] = []
    component_lines = 0
    modified_lines = 0
    for line in text.splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("!"):
            component_lines += 1
        new_line, changed = transform_model_line(line)
        transformed.append(new_line)
        modified_lines += int(changed)

    if text and not transformed:
        new_line, changed = transform_model_line(text)
        transformed = [new_line]
        component_lines = int(bool(text.strip()) and not text.lstrip().startswith("!"))
        modified_lines = int(changed)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(transformed), encoding="utf-8")
    return component_lines, modified_lines
