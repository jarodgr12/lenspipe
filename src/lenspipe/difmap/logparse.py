"""Parse DifMAP logs.

Two kinds of information are recovered:

* Tagged scalars printed by our own command scripts, for example
  ``print "STAGE2_RMS", 17, imstat(rms)``, which DifMAP echoes as
  ``STAGE2_RMS 17 0.00123``.
* The final per-component ``Flux (Jy) / Value Stdev`` table DifMAP prints
  after ``modelfit``. DifMAP occasionally interleaves asynchronous map-size
  warnings in the middle of a printed row; the parser reassembles rows.
"""

from __future__ import annotations

import re

__all__ = [
    "STAGE1_RMS_MARKER",
    "STAGE2_RMS_MARKER",
    "extract_last_numeric",
    "modelfit_flux_measurements_by_fit",
    "repair_merged_warnings",
    "tagged_scalar",
    "tagged_values",
]

STAGE1_RMS_MARKER = "STAGE1_RMS"
STAGE2_RMS_MARKER = "STAGE2_RMS"

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"
_LAST_NUMERIC = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$")


def _to_float(text: str) -> float:
    return float(text.replace("D", "E").replace("d", "e"))


PIXEL_WARNING = "Your choice of large map pixels excluded"
PIXEL_WARNING_CONTINUATION = "The y-axis pixel size should ideally"


def repair_merged_warnings(log_text: str) -> str:
    """Undo DifMAP's stderr warning landing inside a stdout line in a merged log.

    DifMAP writes the map-pixel warning to stderr unbuffered while stdout is
    block-buffered, so in a log that merged both streams the two warning lines
    appear at an arbitrary character position of a stdout line, splitting it in
    two. Removing the warning lines and rejoining the halves restores the
    original stdout text exactly. Logs captured with separate streams are
    returned unchanged.
    """
    if PIXEL_WARNING not in log_text:
        return log_text
    lines = log_text.splitlines()
    repaired: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        position = line.find(PIXEL_WARNING)
        if position < 0 or line.lstrip().startswith("! [stderr]"):
            repaired.append(line)
            index += 1
            continue
        prefix = line[:position]
        index += 1
        while index < len(lines) and lines[index].lstrip().startswith(PIXEL_WARNING_CONTINUATION):
            index += 1
        if index < len(lines):
            # Push the rejoined line back so a second warning in the same line is handled too.
            lines[index] = prefix + lines[index]
        else:
            repaired.append(prefix)
    return "\n".join(repaired) + ("\n" if log_text.endswith("\n") else "")


def tagged_values(log_text: str, marker: str) -> dict[int, float]:
    """Extract ``marker <index> <value>`` lines as ``{index: value}``."""
    log_text = repair_merged_warnings(log_text)
    pattern = re.compile(
        rf"^\s*!?\s*{re.escape(marker)}\s+(?P<index>\d+)\s+(?P<value>{_NUMBER})\s*$"
    )
    values: dict[int, float] = {}
    for raw_line in log_text.splitlines():
        match = pattern.fullmatch(raw_line)
        if match is None:
            continue
        index = int(match.group("index"))
        if index in values:
            raise ValueError(f"Duplicate tagged value for {marker} {index}.")
        values[index] = _to_float(match.group("value"))
    return values


def tagged_scalar(log_text: str, marker: str) -> float | None:
    """Extract the last ``marker <value>`` line, or None if absent."""
    log_text = repair_merged_warnings(log_text)
    pattern = re.compile(rf"^\s*!?\s*{re.escape(marker)}\s+(?P<value>{_NUMBER})\s*$")
    result: float | None = None
    for raw_line in log_text.splitlines():
        match = pattern.fullmatch(raw_line)
        if match is not None:
            result = _to_float(match.group("value"))
    return result


def extract_last_numeric(log_text: str) -> float | None:
    """Legacy Stage 1 fallback: the last line that is purely a number."""
    for line in reversed([line.strip() for line in log_text.splitlines()]):
        if _LAST_NUMERIC.fullmatch(line):
            return float(line)
    return None


def modelfit_flux_measurements_by_fit(
    log_text: str,
    expected_component_count: int,
    marker: str = STAGE2_RMS_MARKER,
) -> dict[int, list[tuple[float, float]]]:
    """Extract final per-component ``(flux, stdev)`` values for each fit.

    Only the final post-``modelfit`` tables are parsed; iteration dumps are
    ignored. The following ``marker`` line associates the completed table with
    its fit index. Raises ``ValueError`` on any structural inconsistency.
    """
    if expected_component_count < 1:
        raise ValueError("expected_component_count must be positive.")
    log_text = repair_merged_warnings(log_text)

    component_pattern = re.compile(
        rf"^\s*!?\s*(?P<flux>{_NUMBER})\s+(?P<stdev>{_NUMBER})\s+"
        rf"(?P<east>{_NUMBER})\s+(?P<east_stdev>{_NUMBER})\s+"
        rf"(?P<north>{_NUMBER})\s+(?P<north_stdev>{_NUMBER})\s+"
        rf"(?P<shape>[A-Za-z][A-Za-z0-9_-]*)\s+"
    )
    marker_pattern = re.compile(
        rf"^\s*!?\s*{re.escape(marker)}\s+(?P<index>\d+)\s+(?P<value>{_NUMBER})\s*$"
    )
    final_table_rule = re.compile(r"^\s*#-+")
    # DifMAP writes its map-pixel warning to stderr while stdout is block-buffered,
    # so in a merged log the warning can split the "#-----" rule into a bare "#" line
    # followed by the dashes. A dash-only line right after a comment line is the rule.
    bare_rule = re.compile(r"-{4,}(?:\s+-{4,})*")
    numeric_start = re.compile(r"^\s*[+-]?(?:\d|\.)")

    values: dict[int, list[tuple[float, float]]] = {}
    in_final_table = False
    table_rows: list[tuple[float, float]] = []
    pending_numeric = ""
    previous_was_comment = False

    def parse_component_row(text: str) -> tuple[float, float] | None:
        match = component_pattern.match(text)
        if match is None:
            return None
        return _to_float(match.group("flux")), _to_float(match.group("stdev"))

    for raw_line in log_text.splitlines():
        if "Your choice of large map pixels excluded" in raw_line:
            raw_line = raw_line.split("Your choice of large map pixels excluded", 1)[0]
        if raw_line.lstrip().startswith("The y-axis pixel size should ideally"):
            continue
        if raw_line.lstrip().startswith("! [stderr]"):
            continue

        stripped = raw_line.strip()
        if previous_was_comment and bare_rule.fullmatch(stripped):
            in_final_table = True
            table_rows = []
            pending_numeric = ""
            previous_was_comment = False
            continue
        if stripped:
            previous_was_comment = stripped.startswith("#") and not final_table_rule.match(raw_line)

        marker_match = marker_pattern.fullmatch(raw_line)
        if marker_match is not None:
            fit_index = int(marker_match.group("index"))
            if fit_index in values:
                raise ValueError(f"Duplicate final Flux/Stdev table for fit {fit_index}.")
            if not in_final_table:
                raise ValueError(
                    f"Fit {fit_index}: RMS marker encountered without a preceding "
                    "final Flux/Stdev table."
                )
            if pending_numeric:
                raise ValueError(
                    f"Fit {fit_index}: incomplete component row remained before RMS marker: "
                    f"{pending_numeric!r}"
                )
            if len(table_rows) != expected_component_count:
                raise ValueError(
                    f"Fit {fit_index}: final Flux/Stdev table contains {len(table_rows)} "
                    f"component rows; hierarchy defines {expected_component_count}."
                )
            values[fit_index] = list(table_rows)
            in_final_table = False
            table_rows = []
            pending_numeric = ""
            continue

        if final_table_rule.match(raw_line):
            in_final_table = True
            table_rows = []
            pending_numeric = ""
            continue

        if not in_final_table:
            continue
        if not numeric_start.match(raw_line) and not pending_numeric:
            continue

        if pending_numeric:
            parsed = None
            for attempt in (
                pending_numeric + raw_line.lstrip(),
                pending_numeric + " " + raw_line.strip(),
            ):
                parsed = parse_component_row(attempt)
                if parsed is not None:
                    break
            if parsed is not None:
                table_rows.append(parsed)
                pending_numeric = ""
                continue
            parsed_new = parse_component_row(raw_line)
            if parsed_new is not None:
                table_rows.append(parsed_new)
                pending_numeric = ""
                continue
            if numeric_start.match(raw_line):
                pending_numeric += raw_line.strip()
            continue

        parsed = parse_component_row(raw_line)
        if parsed is not None:
            table_rows.append(parsed)
        else:
            pending_numeric = raw_line.rstrip()

    return values
