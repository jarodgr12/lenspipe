"""Stage 3 channel exclusions, written either as Stage 2 fit indices or by spectral window.

A spec is a list of items separated by ``;`` (``,`` also separates plain items):

* plain Stage 2 fit indices, 1-based, as before: ``1-4,61-64``
* a spectral window in CASA numbering (0-based), with channels 0-based inside
  it: ``spw3:0-3,60-63``; ``spw7`` or ``spw7:*`` is the whole window.

Recommended form: ``"spw3:0-3,60-63; spw7; 1-4"``. Per-epoch specs are
``EPOCH:<spec>`` (``"E:spw14:0-63; spw15"``), the epoch being the text before
the first colon.

Plain indices need no knowledge of the data. An ``spw`` item is resolved per
Stage 2 product: for a channel product spw ``N`` channel ``c`` is fit index
``N * channels_per_spw + c + 1``; for an IF product spw ``N`` is fit index
``N + 1`` and a channel sub-selection is refused, because an IF fit has no
channel structure left to exclude.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ExclusionSpec",
    "SpwItem",
    "SpwLayout",
    "describe_fit_indices",
    "parse_epoch_exclusion_specs",
    "parse_exclusion_spec",
    "resolve_exclusions",
    "spw_layout_from_metadata",
]

DEFAULT_CHANNELS_PER_SPW = 64
_SPW_ITEM = re.compile(
    r"^spw\s*(?P<spw>\d+)\s*(?:-\s*(?P<spw_last>\d+))?\s*(?::\s*(?P<channels>.*))?$", re.IGNORECASE
)


@dataclass(frozen=True)
class SpwLayout:
    """How Stage 2 fit indices map onto CASA spectral windows for one product."""

    channels_per_spw: int
    n_spw: int | None
    mode: str  # "channel" or "if"
    source: str  # where the layout came from, for metadata


@dataclass(frozen=True)
class SpwItem:
    spw: int  # CASA numbering, 0-based; first window of a range
    channels: tuple[int, ...] | None  # 0-based within the window; None = whole window
    text: str  # as written
    spw_last: int | None = None  # last window of a range ``spw<a>-<b>``; None = single window

    @property
    def spws(self) -> tuple[int, ...]:
        """Every window the item covers."""
        return tuple(range(self.spw, (self.spw_last if self.spw_last is not None else self.spw) + 1))

    @property
    def window_text(self) -> str:
        return f"spw {self.spw}" if self.spw_last is None else f"spw {self.spw}-{self.spw_last}"

    def describe(self) -> str:
        if self.channels is None:
            return f"{self.window_text} (whole window{'s' if self.spw_last is not None else ''})"
        return f"{self.window_text} ch {_compact(self.channels)}"


@dataclass(frozen=True)
class ExclusionSpec:
    """A parsed spec: plain fit indices plus spectral-window items, in source order."""

    text: str
    fit_indices: tuple[int, ...] = ()
    spw_items: tuple[SpwItem, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not self.fit_indices and not self.spw_items

    @property
    def needs_layout(self) -> bool:
        return bool(self.spw_items)

    def describe(self) -> list[str]:
        lines = [item.describe() for item in self.spw_items]
        if self.fit_indices:
            lines.append(f"fit index {_compact(self.fit_indices)}")
        return lines


def _compact(values: tuple[int, ...] | list[int]) -> str:
    ordered = sorted(set(int(v) for v in values))
    if not ordered:
        return ""
    runs: list[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        runs.append(f"{start}-{previous}" if start != previous else str(start))
        start = previous = value
    runs.append(f"{start}-{previous}" if start != previous else str(start))
    return ",".join(runs)


def _int_list(text: str, option_name: str, *, minimum: int) -> list[int]:
    values: set[int] = set()
    for chunk in text.split(","):
        token = chunk.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"Invalid range in {option_name}: {token!r}") from exc
            if start < minimum or end < minimum or end < start:
                raise ValueError(f"Invalid range in {option_name}: {token!r}")
            values.update(range(start, end + 1))
        else:
            try:
                value = int(token)
            except ValueError as exc:
                raise ValueError(f"Invalid index in {option_name}: {token!r}") from exc
            if value < minimum:
                raise ValueError(f"Invalid index in {option_name}: {token!r}")
            values.add(value)
    return sorted(values)


def parse_exclusion_spec(spec: str | None, option_name: str = "stage3.exclude_channels") -> ExclusionSpec:
    """Parse one spec. ``None`` or blank gives an empty spec."""
    text = (spec or "").strip()
    if not text:
        return ExclusionSpec(text="")
    fit_indices: set[int] = set()
    items: list[SpwItem] = []
    for piece in text.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        match = _SPW_ITEM.match(piece)
        if match is not None:
            spw = int(match.group("spw"))
            spw_last = int(match.group("spw_last")) if match.group("spw_last") is not None else None
            if spw_last is not None and spw_last < spw:
                raise ValueError(f"{option_name}: {piece!r}: window range runs backwards (spw{spw}-{spw_last})")
            channel_text = (match.group("channels") or "").strip()
            if channel_text in ("", "*", "all"):
                items.append(SpwItem(spw, None, piece, spw_last))
                continue
            channels = _int_list(channel_text, f"{option_name} ({piece!r})", minimum=0)
            if not channels:
                raise ValueError(f"{option_name}: {piece!r} names no channels")
            items.append(SpwItem(spw, tuple(channels), piece, spw_last))
            continue
        if piece.lower().startswith("spw"):
            raise ValueError(
                f"{option_name}: {piece!r} is not a spectral-window item; write spw<N>, spw<A>-<B>, spw<N>:* or "
                "spw<N>:<channels> (also spw<A>-<B>:<channels> for the same channels in several windows)"
            )
        plain = _int_list(piece, option_name, minimum=1)
        if not plain:
            raise ValueError(f"{option_name}: {piece!r} contains no fit indices")
        fit_indices.update(plain)
    if not fit_indices and not items:
        raise ValueError(f"{option_name} did not contain any valid indices.")
    return ExclusionSpec(text=text, fit_indices=tuple(sorted(fit_indices)), spw_items=tuple(items))


def parse_epoch_exclusion_specs(specifications: list[str]) -> dict[str, ExclusionSpec]:
    """Parse repeatable ``EPOCH:<spec>`` entries; several entries for one epoch are merged."""
    merged: dict[str, list[str]] = {}
    for specification in specifications:
        epoch, separator, spec = str(specification).partition(":")
        epoch, spec = epoch.strip(), spec.strip()
        if not separator or not epoch or not spec:
            raise ValueError(
                "--exclude-epoch-channels must use EPOCH:SPEC syntax, for example 'E:spw14:0-63; spw15' or 'E:897-960'."
            )
        merged.setdefault(epoch, []).append(spec)
    return {
        epoch: parse_exclusion_spec("; ".join(parts), f"--exclude-epoch-channels ({epoch})")
        for epoch, parts in sorted(merged.items())
    }


def spw_layout_from_metadata(stage2_meta: dict[str, Any], stage1_meta: dict[str, Any] | None = None) -> SpwLayout:
    """Channel layout of one Stage 2 product, from its metadata (then Stage 1's, then an assumption)."""
    mode = str(stage2_meta.get("mode") or "channel")
    layout = stage2_meta.get("spectral_windows")
    if isinstance(layout, dict) and layout.get("channels_per_spw"):
        n_spw = layout.get("n_spw")
        return SpwLayout(int(layout["channels_per_spw"]), int(n_spw) if n_spw else None, mode, "stage2 metadata")
    if mode == "if" and stage2_meta.get("channels_per_if"):
        n_fits = stage2_meta.get("n_fits")
        return SpwLayout(int(stage2_meta["channels_per_if"]), int(n_fits) if n_fits else None, mode, "stage2 if mode")
    if stage1_meta:
        final = stage1_meta.get("final_if_selfcal") or {}
        if final.get("enabled", True) and final.get("channels_per_if"):
            return SpwLayout(int(final["channels_per_if"]), final.get("n_ifs") or None, mode, "stage1 metadata")
        per_if = stage1_meta.get("per_if") or {}
        if per_if.get("channels_per_if"):
            return SpwLayout(int(per_if["channels_per_if"]), per_if.get("if_count") or None, mode, "stage1 metadata")
    total = stage2_meta.get("total_channels")
    n_spw = int(total) // DEFAULT_CHANNELS_PER_SPW if total and int(total) % DEFAULT_CHANNELS_PER_SPW == 0 else None
    return SpwLayout(DEFAULT_CHANNELS_PER_SPW, n_spw, mode, "assumed")


def resolve_exclusions(spec: ExclusionSpec, layout: SpwLayout | None, where: str = "") -> list[int]:
    """Turn a spec into sorted Stage 2 fit indices for one product."""
    indices: set[int] = set(spec.fit_indices)
    prefix = f"{where}: " if where else ""
    if spec.spw_items and layout is None:
        raise ValueError(f"{prefix}spectral-window exclusions need the Stage 2 metadata to resolve")
    for item in spec.spw_items:
        assert layout is not None
        for spw in item.spws:
            if layout.n_spw is not None and not 0 <= spw < layout.n_spw:
                raise ValueError(
                    f"{prefix}{item.text!r}: spw {spw} is outside 0-{layout.n_spw - 1} (CASA numbering, from 0)"
                )
            if layout.mode == "if":
                if item.channels is not None:
                    raise ValueError(
                        f"{prefix}{item.text!r}: this is an IF product (one fit per spectral window), so channels "
                        f"inside spw {spw} cannot be excluded separately; write 'spw{spw}' to drop the "
                        "whole window, or exclude channels on a channel product"
                    )
                indices.add(spw + 1)
                continue
            channels = item.channels if item.channels is not None else tuple(range(layout.channels_per_spw))
            bad = [c for c in channels if c >= layout.channels_per_spw]
            if bad:
                raise ValueError(
                    f"{prefix}{item.text!r}: channel(s) {bad} are outside 0-{layout.channels_per_spw - 1} of a "
                    f"{layout.channels_per_spw}-channel spectral window"
                )
            indices.update(spw * layout.channels_per_spw + c + 1 for c in channels)
    return sorted(indices)


def describe_fit_indices(indices: list[int] | tuple[int, ...], layout: SpwLayout | None) -> list[str]:
    """Human-readable ``spw N ch a-b`` lines for resolved fit indices."""
    ordered = sorted(set(int(i) for i in indices))
    if not ordered:
        return []
    if layout is None:
        return [f"fit index {_compact(ordered)}"]
    if layout.mode == "if":
        return [f"spw {i - 1} (whole window, fit index {i})" for i in ordered]
    by_spw: dict[int, list[int]] = {}
    for index in ordered:
        spw, channel = divmod(index - 1, layout.channels_per_spw)
        by_spw.setdefault(spw, []).append(channel)
    lines = []
    for spw, channels in sorted(by_spw.items()):
        if len(channels) == layout.channels_per_spw:
            lines.append(f"spw {spw} (whole window)")
        else:
            lines.append(f"spw {spw} ch {_compact(channels)}")
    return lines
