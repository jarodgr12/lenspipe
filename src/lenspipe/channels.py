"""Per-IF channel selections: ``"1:3-62; 2:5-60,62; 7-12:1-64"``.

One entry per ``;``: an IF number or inclusive IF range (1-based), a colon, and a
channel list in Stage 2's syntax (``1-10,15``), numbered 1-based *within* the IF.
IFs not mentioned fall back to the symmetric ``edge_channels`` exclusion. The
selections become absolute DifMAP channel numbers (1-based, contiguous across
IFs, ``(if-1)*channels_per_if + c``) and are emitted as pairs for ``select``,
which accepts any number of ranges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "FitSelection",
    "IfSelection",
    "excluded_channels_from_spec",
    "exclusion_tag",
    "include_spec_to_exclusions",
    "parse_if_channel_entries",
    "parse_if_channel_spec",
    "segments",
    "select_ranges",
    "selections_from_exclusions",
]


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


def parse_if_channel_entries(spec: str | None, if_count: int, channels_per_if: int) -> dict[int, list[int]]:
    """The include form ``"1:3-62; 2:5-60,62"``: listed IF -> channels (1-based within the IF)."""
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
    return within


def parse_if_channel_spec(
    spec: str | None, if_count: int, channels_per_if: int, edge_channels: int = 0
) -> list[IfSelection]:
    """One selection per IF, in IF order, from the include-form ``spec`` plus the edge default."""
    if if_count < 1 or channels_per_if < 1 or edge_channels < 0:
        raise ValueError("if_count and channels_per_if must be positive and edge_channels non-negative")
    if 2 * edge_channels >= channels_per_if:
        raise ValueError("Twice edge_channels must be smaller than channels_per_if.")
    within = parse_if_channel_entries(spec, if_count, channels_per_if)
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


def _compact_0based(values: list[int] | tuple[int, ...]) -> str:
    return ",".join(f"{a}-{b}" if a != b else str(a) for a, b in segments(tuple(sorted(values))))


def include_spec_to_exclusions(spec: str | None, if_count: int, channels_per_if: int) -> str | None:
    """Convert the pre-2.3 include form (``"1:3-62"``, DifMAP numbering) into the exclusion form.

    Each listed IF becomes ``spw<n-1>:<channels not listed, 0-based>``; an IF that
    keeps every channel produces nothing. Unlisted IFs were covered by the edge
    default before and still are, so they need no item. Returns None for no items.
    """
    within = parse_if_channel_entries(spec, if_count, channels_per_if)
    items: list[str] = []
    for if_number in sorted(within):
        kept = set(within[if_number])
        dropped = [c - 1 for c in range(1, channels_per_if + 1) if c not in kept]
        if dropped:
            items.append(f"spw{if_number - 1}:{_compact_0based(dropped)}")
    return "; ".join(items) or None


def excluded_channels_from_spec(
    spec: str | None, total_channels: int, channels_per_if: int, option_name: str = "exclude_channels"
) -> set[int]:
    """Absolute DifMAP channel numbers (1-based) named by an exclusion spec.

    The spec is the one Stage 3 uses: ``spw<N>:<channels>`` (CASA spectral window
    from 0, channels from 0 inside it), ``spw<N>`` for a whole window, items
    separated by ``;``. A plain number or range is an absolute DifMAP channel.
    Spectral windows are laid out ``channels_per_if`` wide across ``total_channels``.
    """
    from lenspipe.stage3.exclusions import parse_exclusion_spec

    parsed = parse_exclusion_spec(spec, option_name)
    if parsed.empty:
        return set()
    if channels_per_if < 1 or total_channels < 1:
        raise ValueError(f"{option_name}: channels_per_if and total_channels must be positive")
    n_spw = total_channels // channels_per_if
    excluded: set[int] = set()
    for channel in parsed.fit_indices:
        if not 1 <= channel <= total_channels:
            raise ValueError(f"{option_name}: channel {channel} is outside 1-{total_channels}")
        excluded.add(channel)
    for item in parsed.spw_items:
        channels = item.channels if item.channels is not None else tuple(range(channels_per_if))
        bad = [c for c in channels if c >= channels_per_if]
        if bad:
            raise ValueError(
                f"{option_name}: {item.text!r}: channel(s) {bad} are outside 0-{channels_per_if - 1} of a "
                f"{channels_per_if}-channel spectral window"
            )
        for spw in item.spws:
            if not 0 <= spw < n_spw:
                raise ValueError(
                    f"{option_name}: {item.text!r}: spw {spw} is outside 0-{n_spw - 1} (CASA numbering, from 0; "
                    f"{n_spw} windows are spw0 to spw{n_spw - 1})"
                )
            excluded.update(spw * channels_per_if + c + 1 for c in channels)
    return excluded


def selections_from_exclusions(
    spec: str | None, if_count: int, channels_per_if: int, edge_channels: int = 0,
    option_name: str = "exclude_channels",
) -> tuple[list[IfSelection], list[int]]:
    """Per-IF selections after the edge default and the exclusion spec are applied.

    Returns the IFs that still have channels, in IF order, and the IF numbers that
    were left with none (a whole-window exclusion, or everything excluded).
    """
    if if_count < 1 or channels_per_if < 1 or edge_channels < 0:
        raise ValueError("if_count and channels_per_if must be positive and edge_channels non-negative")
    if 2 * edge_channels >= channels_per_if:
        raise ValueError("Twice edge_channels must be smaller than channels_per_if.")
    total = if_count * channels_per_if
    excluded = excluded_channels_from_spec(spec, total, channels_per_if, option_name)
    selections: list[IfSelection] = []
    skipped: list[int] = []
    for if_number in range(1, if_count + 1):
        offset = (if_number - 1) * channels_per_if
        kept = tuple(
            offset + c for c in range(edge_channels + 1, channels_per_if - edge_channels + 1)
            if offset + c not in excluded
        )
        if kept:
            selections.append(IfSelection(if_number, kept))
        else:
            skipped.append(if_number)
    return selections, skipped


def exclusion_tag(spec: str | None) -> str:
    """Filename-safe rendering of an exclusion spec: ``spw3:0-3,60-63; spw7`` -> ``spw3_0-3_60-63_spw7``."""
    text = re.sub(r"[^0-9a-z-]+", "_", (spec or "").lower()).strip("_")
    return re.sub(r"_+", "_", text)


@dataclass(frozen=True)
class FitSelection:
    """One Stage 2 fit: its index and the channel runs DifMAP selects for it."""

    fit_index: int
    segments: tuple[tuple[int, int], ...]  # absolute 1-based inclusive runs

    @property
    def first(self) -> int:
        return self.segments[0][0]

    @property
    def last(self) -> int:
        return self.segments[-1][1]

    @property
    def n_channels(self) -> int:
        return sum(b - a + 1 for a, b in self.segments)

    @property
    def channels(self) -> list[int]:
        return [c for a, b in self.segments for c in range(a, b + 1)]

    @property
    def contiguous(self) -> bool:
        return len(self.segments) == 1

    @property
    def select_arguments(self) -> str:
        return ",".join(f"{a},{b}" for a, b in self.segments)

    def as_range(self) -> tuple[int, int, int]:
        return (self.fit_index, self.first, self.last)
