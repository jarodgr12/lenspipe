#!/usr/bin/env python3
"""A stand-in for DifMAP that speaks just enough of its command language.

It reads a command script on stdin, prints output shaped like the real
program (including the final ``Flux/Stdev`` table after ``modelfit`` and the
echo of ``print`` statements), and writes the files that ``wobs``, ``wmodel``,
``wmap`` and ``wdmap`` would produce. Fitted fluxes are a deterministic
function of the selected channel range, so results do not depend on how the
work is split across processes.

Only the standard library is used so the script runs under any python3.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

BANNER = "Caltech difmap 2.5k (fake-difmap for lenspipe tests)"
SPLIT_WARNING = (
    "Your choice of large map pixels excluded 12.3% of the data.\n"
    "The y-axis pixel size should ideally be below 24.97 milli-arcsec."
)


class State:
    def __init__(self) -> None:
        self.observed: Path | None = None
        self.model_rows: list[list[str]] = []
        self.model_comments: list[str] = []
        self.selection: tuple[int, int] | None = None
        self.rms: float = 0.0
        self.fitted = False


def emit(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def parse_model(path: Path, state: State) -> None:
    rows: list[list[str]] = []
    comments: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            comments.append(line)
            continue
        rows.append(stripped.split())
    if not rows:
        emit(f"rmodel: no components in {path}")
        sys.exit(1)
    state.model_rows = rows
    state.model_comments = comments
    state.fitted = False


def base_flux(token: str) -> float:
    cleaned = token[:-1] if token.lower().endswith("v") else token
    return float(cleaned.replace("D", "E").replace("d", "e"))


def fitted_flux(base: float, index: int, selection: tuple[int, int] | None) -> float:
    first, last = selection or (1, 1)
    phase = 0.37 * first + 0.11 * last + 1.7 * index
    return base * (1.0 + 0.02 * math.sin(phase) + 0.0005 * index)


def fitted_stdev(flux: float, index: int, selection: tuple[int, int] | None) -> float:
    first, _ = selection or (1, 1)
    return abs(flux) * (0.01 + 0.002 * ((first + index) % 5))


def do_modelfit(state: State, iterations: int) -> None:
    if not state.model_rows:
        emit("modelfit: no model to fit")
        return
    crash_channel = os.environ.get("FAKE_DIFMAP_FAIL_ON_CHANNEL")
    if crash_channel and state.selection and state.selection[0] == int(crash_channel):
        emit("Simulated crash: segmentation fault in modelfit")
        sys.exit(4)
    state.fitted = True
    emit("Iteration 01: reduced chi-squared = 1.234567")
    emit(f"Iteration {iterations:02d}: reduced chi-squared = 1.100000")
    emit("#    Flux (Jy)          East (mas)         North (mas)        Shape")
    emit("#  Value    Stdev     Value    Stdev     Value    Stdev     Type    Major  Ratio  Phi")
    first = state.selection[0] if state.selection else 1
    if os.environ.get("FAKE_DIFMAP_STDERR_SPLIT") and first % 7 == 5:
        # Real DifMAP 2.5q behaviour seen on 1555 epoch B: the stderr warning is written
        # unbuffered while stdout is block-buffered, so in a merged stream it can split the
        # table rule. Emit it the way the real program does: to stderr, mid-line. Opt-in,
        # because the legacy scripts (merged streams) cannot survive it.
        sys.stdout.write("#")
        sys.stdout.flush()
        sys.stderr.write(SPLIT_WARNING + "\n")
        sys.stderr.flush()
        emit("-------------------------------------------------------------------------------")
    else:
        emit("#-------------------------------------------------------------------------------")
    for index, row in enumerate(state.model_rows):
        flux = fitted_flux(base_flux(row[0]), index, state.selection)
        stdev = fitted_stdev(flux, index, state.selection)
        radius = float(row[1].rstrip("vV")) if len(row) > 1 else 0.0
        theta = float(row[2].rstrip("vV")) if len(row) > 2 else 0.0
        east = radius * math.sin(math.radians(theta))
        north = radius * math.cos(math.radians(theta))
        shape = "gaussian" if len(row) > 6 and row[6].rstrip("vV") == "1" else "delta"
        text = (
            f" {flux:.8g} {stdev:.6g} {east:.5g} 0 {north:.5g} 0 {shape} "
            f"{row[3].rstrip('vV') if len(row) > 3 else '0'} "
            f"{row[4].rstrip('vV') if len(row) > 4 else '1'} "
            f"{row[5].rstrip('vV') if len(row) > 5 else '0'}"
        )
        north_text = f"{north:.5g}"
        shape_at = text.index(shape)
        north_at = text[:shape_at].rfind(north_text)
        if first % 5 == 3 and index == 1 and len(north_text) >= 2 and north_at > 0:
            # Reproduce DifMAP's asynchronous warning landing inside a numeric field.
            cut = north_at + len(north_text) // 2
            emit(text[:cut] + SPLIT_WARNING.split("\n")[0])
            emit(SPLIT_WARNING.split("\n")[1])
            emit(text[cut:])
        else:
            emit(text)


def do_invert(state: State) -> None:
    first, last = state.selection or (1, 1)
    state.rms = 0.001 * (1.0 + 0.1 * ((first * 7 + last) % 13) / 13.0)
    emit("Inverting map and beam")
    emit(f"Estimated noise={state.rms * 1000:.4f} mJy/beam.")


def do_print(state: State, argument_text: str) -> None:
    parts: list[str] = []
    for token in re.findall(r'"[^"]*"|[^,]+', argument_text):
        token = token.strip()
        if not token:
            continue
        if token.startswith('"') and token.endswith('"'):
            parts.append(token[1:-1])
        elif token == "imstat(rms)":
            parts.append(f"{state.rms:.8g}")
        else:
            parts.append(token)
    emit(" ".join(parts))


def write_model(state: State, path: Path) -> None:
    lines = [
        "! Flux (Jy) Radius (mas)  Theta (deg)  Major (mas)  Axial ratio   Phi (deg) T "
        "Freq (Hz)     SpecIndex",
    ]
    for index, row in enumerate(state.model_rows):
        tokens = list(row)
        if state.fitted:
            flux = fitted_flux(base_flux(row[0]), index, state.selection)
            suffix = "v" if row[0].lower().endswith("v") else ""
            tokens[0] = f"{flux:.8g}{suffix}"
        lines.append(" ".join(tokens))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_fits_like(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"SIMPLE  =                    T / fake {label}\n".encode().ljust(2880, b" "))


def main() -> int:
    state = State()
    emit(BANNER)
    for raw in sys.stdin:
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command == "observe":
            path = Path(argument)
            if not path.is_file():
                emit(f"observe: unable to open {path}")
                return 1
            state.observed = path
            emit(f"Reading UV FITS file: {path}")
            emit("AN table 1: 27 integrations on 351 of 351 possible baselines.")
        elif command == "select":
            fields = [f.strip() for f in argument.split(",")]
            if len(fields) >= 3:
                state.selection = (int(fields[1]), int(fields[2]))
                emit(f"Selecting polarization: I,  channels: {fields[1]}..{fields[2]}")
            else:
                state.selection = None
                emit("Selecting polarization: I,  channels: all")
        elif command == "unflag":
            emit("Unflagging all data.")
        elif command == "mapsize":
            emit(f"Map grid = {argument} pixels.")
        elif command == "uvw":
            emit(f"Uniform weighting binwidth: {argument}")
        elif command == "rmodel":
            parse_model(Path(argument), state)
            emit(f"A total of {len(state.model_rows)} model components were read from file {argument}")
        elif command == "selfcal":
            emit(f"Performing {'amp+phase' if argument.startswith('true') else 'phase'} self-cal")
            emit("Applying selfcal corrections to the data")
        elif command == "modelfit":
            do_modelfit(state, int(argument or "1"))
        elif command == "invert":
            do_invert(state)
        elif command == "restore":
            emit("Restoring with CLEAN beam")
        elif command == "print":
            do_print(state, argument)
        elif command == "wmodel":
            write_model(state, Path(argument))
            emit(f"Writing {len(state.model_rows)} model components to file: {argument}")
        elif command == "wobs":
            if state.observed is None:
                emit("wobs: no data observed")
                return 1
            target = Path(argument)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(state.observed, target)
            emit(f"Writing UV FITS file: {argument}")
        elif command in {"wmap", "wdmap"}:
            write_fits_like(Path(argument), command)
            emit(f"Writing {'clean' if command == 'wmap' else 'dirty'} map to FITS file: {argument}")
        elif command == "sleep":
            time.sleep(float(argument))
        elif command == "fail":
            emit("Deliberate failure requested")
            return 3
        elif command == "quit":
            emit("Exiting program")
            return 0
        else:
            emit(f"Unknown command: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
