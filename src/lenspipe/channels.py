"""Per-IF channel selections: ``"1:3-62; 2:5-60,62; 7-12:1-64"``.

One entry per ``;``: an IF number or inclusive IF range (1-based), a colon, and a
channel list in Stage 2's syntax (``1-10,15``), numbered 1-based *within* the IF.
IFs not mentioned fall back to the symmetric ``edge_channels`` exclusion. The
selections become absolute DifMAP channel numbers (1-based, contiguous across
IFs, ``(if-1)*channels_per_if + c``) and are emitted as pairs for ``select``,
which accepts any number of ranges.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["IfSelection", "parse_if_channel_spec", "segments", "select_ranges"]


@dataclass(frozen=True)
class IfSelection:
    if_number: int  # 1-based
    channels: tuple[int, ...]  # absolute 1-based DifMAP channels, sorted

    @property
    def segments(self) -> list[tuple[int, int]]:
        return segments(self.channels)

    @property
    def first(self) -> int:
        return self.channels[0]

    @property
    def last(self) -> int:
        return self.channels[-1]

    @property
    def select_arguments(self) -> str:
        """``a,b,c,d`` for ``select i,a,b,c,d``."""
        return ",".join(f"{a},{b}" for a, b in self.segments)

    @property
    def label(self) -> str:
        return " ".join(f"{a}-{b}" if a != b else str(a) for a, b in self.segments)


def segments(channels: tuple[int, ...] | list[int]) -> list[tuple[int, int]]:
    """Collapse sorted channel numbers into inclusive runs."""
    runs: list[tuple[int, int]] = []
    for channel in channels:
        if runs and channel == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], channel)
        else:
            runs.append((channel, channel))
    return runs


def _channel_list(text: str, upper: int, where: str) -> list[int]:
    chosen: set[int] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"{where}: bad channel range {item!r}") from exc
            if end < start:
                raise ValueError(f"{where}: descending channel range {item!r}")
            chosen.update(range(start, end + 1))
        else:
            try:
                chosen.add(int(item))
            except ValueError as exc:
                raise ValueError(f"{where}: bad channel {item!r}") from exc
    if not chosen:
        raise ValueError(f"{where}: no channels selected")
    bad = sorted(c for c in chosen if c < 1 or c > upper)
    if bad:
        raise ValueError(f"{where}: channel(s) outside 1-{upper} within the IF: {bad}")
    return sorted(chosen)


def parse_if_channel_spec(
    spec: str | None, if_count: int, channels_per_if: int, edge_channels: int = 0
) -> list[IfSelection]:
    """One selection per IF, in IF order, from ``spec`` plus the edge default."""
    if if_count < 1 or channels_per_if < 1 or edge_channels < 0:
        raise ValueError("if_count and channels_per_if must be positive and edge_channels non-negative")
    if 2 * edge_channels >= channels_per_if:
        raise ValueError("Twice edge_channels must be smaller than channels_per_if.")
    within: dict[int, list[int]] = {}
    for raw in (spec or "").split(";"):
        entry = raw.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(f"channel entry {entry!r} needs the form IF:channels, e.g. '3:5-60'")
        if_text, channel_text = entry.split(":", 1)
        if_text = if_text.strip()
        if "-" in if_text:
            a_text, b_text = if_text.split("-", 1)
            try:
                if_first, if_last = int(a_text), int(b_text)
            except ValueError as exc:
                raise ValueError(f"bad IF range {if_text!r} in {entry!r}") from exc
        else:
            try:
                if_first = if_last = int(if_text)
            except ValueError as exc:
                raise ValueError(f"bad IF number {if_text!r} in {entry!r}") from exc
        if if_first < 1 or if_last > if_count or if_last < if_first:
            raise ValueError(f"IF range {if_text!r} outside 1-{if_count} in {entry!r}")
        channels = _channel_list(channel_text, channels_per_if, f"IF {if_text}")
        for if_number in range(if_first, if_last + 1):
            if if_number in within:
                raise ValueError(f"IF {if_number} is given channels more than once")
            within[if_number] = channels
    default = list(range(edge_channels + 1, channels_per_if - edge_channels + 1))
    selections: list[IfSelection] = []
    for if_number in range(1, if_count + 1):
        offset = (if_number - 1) * channels_per_if
        chosen = within.get(if_number, default)
        selections.append(IfSelection(if_number, tuple(offset + c for c in chosen)))
    return selections


def select_ranges(selections: list[IfSelection]) -> list[tuple[int, int]]:
    """Flat ``(first, last)`` per IF for callers that only need the span."""
    return [(s.first, s.last) for s in selections]
