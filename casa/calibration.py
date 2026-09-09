#!/usr/bin/env python3
"""EVLA Ku-band calibration for 22A-388 (B1555+375), CASA 6.5.7.

Modernised from ``legacy/scriptForCalibration.A.py``. Runs either under
``casa -c calibration.py --config <toml>`` (tasks are shell globals) or under
a plain Python 3.11+ that can ``import casatasks``. ``--list-steps`` and
config validation work without CASA installed.

Steps and calibration logic follow the legacy script exactly (tables, fields,
channel ranges, solints, interp, gainfield, append flags). Only step 10
(export) changes: one full-resolution UVFITS per epoch for DifMAP Stage 1.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Field ids in the split MS (mstransform reindexes): 0 = flux calibrator
# (3C286), 1 = phase calibrator, 2 = target. Fixed by the legacy logic.
FLUX_FIELD = "0"
PHASE_FIELD = "1"
TARGET_FIELD = "2"

APRIORI = ["antpos.cal", "gaincurve.cal", "opacity.cal", "rq.cal"]
# Inner channels only (spw edges excluded) for the short-phase and gain solves.
SHORT_PHASE_SPW = "*:28~36"
GAIN_SPW = (
    "0:10~53,1~14:4~60,15~16:10~53,17~30:4~60,31~32:10~53,33~46:4~60,47:10~53"
)
MINSNR = 2.0  # legacy solver threshold (CASA default is 3.0)


# --- CASA access (works under `casa -c` and under `import casatasks`) ------
class _Casa:
    """Resolve CASA tasks lazily so the module imports without CASA."""

    def __getattr__(self, name: str) -> Callable[..., Any]:
        try:
            import casatasks  # noqa: PLC0415 - optional dependency

            if hasattr(casatasks, name):
                return getattr(casatasks, name)
        except ImportError:
            pass
        main = sys.modules.get("__main__")
        for namespace in (vars(main) if main else {}, globals()):
            if name in namespace:
                return namespace[name]
        raise RuntimeError(
            f"CASA task {name!r} is not available: run under `casa -c` or "
            "install casatasks."
        )


casa = _Casa()


def log(message: str, priority: str = "INFO") -> None:
    try:
        casa.casalog.post(message, priority, "calibration")
    except RuntimeError:
        print(message)


def casa_version() -> str | None:
    for module in ("casatasks", "casatools"):
        try:
            return str(__import__(module).version_string())
        except Exception:  # noqa: BLE001 - absent/odd CASA builds
            continue
    return None


# --- Configuration (TOML) --------------------------------------------------
@dataclass(frozen=True)
class Config:
    msfile: str
    mssplit: str
    source: str
    epoch: str
    fields: str
    spw: str
    scans: str
    refant: str
    opacity_spw: str
    flux_model: str
    flux_standard: str
    flags: tuple[dict[str, Any], ...]
    legacy_split: bool
    overwrite: bool
    statwt_minsamp: int
    raw: dict[str, Any]

    @property
    def listobs_file(self) -> str:
        return f"{self.mssplit}.txt"

    @property
    def target_ms(self) -> str:
        return f"{self.source}.{self.epoch}.ms"

    @property
    def uvfits(self) -> str:
        return f"{self.source}.{self.epoch}.uvfits"

    @property
    def record_file(self) -> str:
        return f"{self.mssplit}.calibration.json"


def _require(table: dict[str, Any], section: str, key: str) -> Any:
    if key not in table:
        raise ValueError(f"config: missing [{section}] {key}")
    return table[key]


def load_config(path: Path) -> Config:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    obs = raw.get("observation", {})
    fluxcal = raw.get("fluxcal", {})
    apriori = raw.get("apriori", {})
    export = raw.get("export", {})
    flags = raw.get("flags", [])
    if not isinstance(flags, list) or not all(isinstance(f, dict) for f in flags):
        raise ValueError("config: [[flags]] must be an array of tables")
    for cmd in flags:
        if "mode" not in cmd:
            raise ValueError(f"config: flag command without mode: {cmd}")
    return Config(
        msfile=_require(obs, "observation", "msfile"),
        mssplit=_require(obs, "observation", "mssplit"),
        source=_require(obs, "observation", "source"),
        epoch=_require(obs, "observation", "epoch"),
        fields=_require(obs, "observation", "fields"),
        spw=_require(obs, "observation", "spw"),
        scans=_require(obs, "observation", "scans"),
        refant=_require(obs, "observation", "refant"),
        opacity_spw=apriori.get("opacity_spw", obs.get("spw")),
        flux_model=_require(fluxcal, "fluxcal", "model"),
        flux_standard=fluxcal.get("standard", "Perley-Butler 2010"),
        flags=tuple(flags),
        legacy_split=bool(export.get("legacy_split", False)),
        overwrite=bool(export.get("overwrite", False)),
        statwt_minsamp=int(export.get("statwt_minsamp", 8)),
        raw=raw,
    )


# --- Step registry ---------------------------------------------------------
StepFn = Callable[[Config], None]


@dataclass(frozen=True)
class Step:
    number: int
    title: str
    func: StepFn


STEPS: dict[int, Step] = {}


def step(number: int, title: str) -> Callable[[StepFn], StepFn]:
    def register(func: StepFn) -> StepFn:
        if number in STEPS:
            raise ValueError(f"step {number} registered twice")
        STEPS[number] = Step(number, title, func)
        return func

    return register


def _gaincal(cfg: Config, caltable: str, field: str, spw: str, solint: str,
             calmode: str, gaintable: list[str], **extra: Any) -> None:
    """gaincal with the legacy solver settings (refant, minsnr=2) filled in."""
    casa.gaincal(
        vis=cfg.mssplit, caltable=caltable, field=field, spw=spw, solint=solint,
        refant=cfg.refant, minsnr=MINSNR, calmode=calmode, gaintable=gaintable,
        **extra,
    )


@step(0, "Set the variables and initial split (mstransform)")
def step_split(cfg: Config) -> None:
    casa.mstransform(
        vis=cfg.msfile, outputvis=cfg.mssplit, field=cfg.fields, spw=cfg.spw,
        scan=cfg.scans, datacolumn="data",
    )
    casa.listobs(vis=cfg.mssplit, verbose=True, listfile=cfg.listobs_file,
                 overwrite=True)


@step(1, "A priori correction of opacity, antenna elevation and antenna "
         "positions (gencal)")
def step_apriori(cfg: Config) -> None:
    # Needs network access: opacity from weather, antenna position corrections.
    tau = casa.plotweather(vis=cfg.mssplit, doPlot=True)
    casa.gencal(vis=cfg.mssplit, caltable="opacity.cal", caltype="opac",
                spw=cfg.opacity_spw, parameter=tau)
    casa.gencal(vis=cfg.mssplit, caltable="antpos.cal", caltype="antpos")
    casa.gencal(vis=cfg.mssplit, caltable="gaincurve.cal", caltype="gceff")
    casa.gencal(vis=cfg.mssplit, caltable="rq.cal", caltype="rq")


@step(2, "Flag bad data (flagdata)")
def step_flag(cfg: Config) -> None:
    for cmd in cfg.flags:
        log(f"flagdata {cmd}")
        casa.flagdata(vis=cfg.mssplit, **cmd)


@step(3, "Insert model of the flux calibrator (setjy)")
def step_setjy(cfg: Config) -> None:
    casa.setjy(vis=cfg.mssplit, field=FLUX_FIELD, standard=cfg.flux_standard,
               model=cfg.flux_model, usescratch=True)


@step(4, "Short phase correction (gaincal)")
def step_short_phase(cfg: Config) -> None:
    _gaincal(cfg, "intphase.cal", FLUX_FIELD, SHORT_PHASE_SPW, "int", "p",
             APRIORI)


@step(5, "Delay correction (gaincal)")
def step_delays(cfg: Config) -> None:
    _gaincal(cfg, "delays.cal", FLUX_FIELD, "", "inf", "p",
             APRIORI + ["intphase.cal"], combine="scan", gaintype="K")


@step(6, "Bandpass calibration (bandpass)")
def step_bandpass(cfg: Config) -> None:
    casa.bandpass(
        vis=cfg.mssplit, caltable="bpass.cal", field=FLUX_FIELD, solint="inf",
        combine="scan", refant=cfg.refant, minsnr=MINSNR, bandtype="B",
        gaintable=APRIORI + ["intphase.cal", "delays.cal"],
    )


@step(7, "Gain (Amplitude and Phase) calibration (gaincal)")
def step_gains(cfg: Config) -> None:
    prior = APRIORI + ["delays.cal", "bpass.cal"]
    # One deliberate change from the legacy script: the target phase solve used
    # append=False, which replaced phase.cal with the target's solutions just
    # before the amp.cal solves and fluxscale needed the calibrators' ones.
    # All three fields now append (agreed 2026-09-09; see CHANGELOG 2.0.2).
    for fld, append in ((FLUX_FIELD, False), (PHASE_FIELD, True),
                        (TARGET_FIELD, True)):
        _gaincal(cfg, "phase.cal", fld, GAIN_SPW, "int", "p", prior,
                 append=append)
    interp = ["", "", "", "", "nearest", "nearest", "linear"]
    for fld, phase_field, append in ((FLUX_FIELD, FLUX_FIELD, False),
                                     (PHASE_FIELD, PHASE_FIELD, True),
                                     (TARGET_FIELD, PHASE_FIELD, True)):
        _gaincal(cfg, "amp.cal", fld, GAIN_SPW, "inf", "ap",
                 prior + ["phase.cal"], append=append,
                 gainfield=["", "", "", "", FLUX_FIELD, FLUX_FIELD, phase_field],
                 interp=interp)


@step(8, "Determine the absolute flux-scale of the calibrators (fluxscale)")
def step_fluxscale(cfg: Config) -> None:
    flux = casa.fluxscale(
        vis=cfg.mssplit, caltable="amp.cal", fluxtable="flux.cal",
        reference=[FLUX_FIELD], transfer=[PHASE_FIELD], incremental=True,
        fitorder=1,
    )
    casa.setjy(vis=cfg.mssplit, field=PHASE_FIELD, standard="fluxscale",
               fluxdict=flux, usescratch=True)
    # Second calibration chain, solved on the (now flux-scaled) phase calibrator.
    _gaincal(cfg, "intphase2.cal", PHASE_FIELD, SHORT_PHASE_SPW, "int", "p",
             APRIORI)
    _gaincal(cfg, "delays2.cal", PHASE_FIELD, "", "inf", "p",
             APRIORI + ["intphase2.cal"], combine="scan", gaintype="K")
    casa.bandpass(
        vis=cfg.mssplit, caltable="bpass2.cal", field=PHASE_FIELD, solint="inf",
        combine="scan", refant=cfg.refant, minsnr=MINSNR, bandtype="B",
        gaintable=APRIORI + ["intphase2.cal", "delays2.cal"],
    )
    prior2 = APRIORI + ["delays2.cal", "bpass2.cal"]
    _gaincal(cfg, "phase2.cal", PHASE_FIELD, GAIN_SPW, "int", "p", prior2)
    _gaincal(cfg, "amp2.cal", PHASE_FIELD, GAIN_SPW, "inf", "ap",
             prior2 + ["phase2.cal"],
             interp=["", "", "", "", "nearest", "nearest", "linear"])


@step(9, "Applying the calibration tables (applycal)")
def step_applycal(cfg: Config) -> None:
    interp = ["", "", "", "", "nearest", "nearest", "linear", "nearest"]
    chain1 = APRIORI + ["delays.cal", "bpass.cal", "phase.cal", "amp.cal"]
    chain2 = APRIORI + ["delays2.cal", "bpass2.cal", "phase2.cal", "amp2.cal"]
    plan = (
        (FLUX_FIELD, chain1, FLUX_FIELD),
        (PHASE_FIELD, chain2, PHASE_FIELD),
        (TARGET_FIELD, chain2, PHASE_FIELD),
    )
    for fld, tables, gf in plan:
        casa.applycal(
            vis=cfg.mssplit, field=fld, gaintable=tables,
            gainfield=["", "", "", "", gf, gf, gf, gf], interp=interp,
            calwt=False,
        )


def _export_one(cfg: Config, outputvis: str, fitsfile: str, spw: str,
                width: int) -> None:
    casa.split(vis=cfg.mssplit, outputvis=outputvis, datacolumn="corrected",
               field=TARGET_FIELD, spw=spw, width=width)
    casa.statwt(vis=outputvis, minsamp=cfg.statwt_minsamp, datacolumn="data",
                flagbackup=False)
    casa.exportuvfits(
        vis=outputvis, fitsfile=fitsfile, datacolumn="data", multisource=True,
        combinespw=True, writestation=True, padwithflags=True,
        overwrite=cfg.overwrite,
    )
    log(f"wrote {fitsfile}")


@step(10, "Split target and export UVFITS for DifMAP Stage 1")
def step_export(cfg: Config) -> None:
    if not cfg.legacy_split:
        # One file per epoch, all spws, no channel averaging (48 IFs x 64 ch).
        _export_one(cfg, cfg.target_ms, cfg.uvfits, spw="", width=1)
        return
    # Legacy layout for comparison: three 16-spw files averaged by 4 channels.
    log("export.legacy_split=true: writing three 4-channel-averaged files")
    for n, spw in enumerate(("0~15", "16~31", "32~47"), start=1):
        stem = f"{cfg.source}.{cfg.epoch}.{n}"
        _export_one(cfg, f"{stem}.ms", f"{stem}.uvfits", spw=spw, width=4)


# --- Runner and CLI --------------------------------------------------------
def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run(cfg: Config, steps: Iterable[int] | None = None,
        config_path: Path | None = None) -> dict[str, Any]:
    """Run the requested steps in ascending order; record to JSON as it goes."""
    requested = sorted(STEPS) if steps is None else sorted(set(steps))
    unknown = [n for n in requested if n not in STEPS]
    if unknown:
        raise ValueError(f"unknown steps {unknown}; valid: {sorted(STEPS)}")
    record: dict[str, Any] = {
        "config_file": str(config_path) if config_path else None,
        "config": cfg.raw, "casa_version": casa_version(),
        "python_version": sys.version.split()[0], "started": _now(),
        "requested_steps": requested, "steps": [],
    }
    record_path = Path(cfg.record_file)

    def save() -> None:
        record_path.write_text(json.dumps(record, indent=2, default=str) + "\n")

    for number in requested:
        st = STEPS[number]
        log(f"Step {number}: {st.title}")
        entry: dict[str, Any] = {"step": number, "title": st.title,
                                 "started": _now(), "status": "running"}
        record["steps"].append(entry)
        save()
        try:
            st.func(cfg)
        except Exception as exc:
            entry.update(status="failed", error=repr(exc), finished=_now())
            record["finished"] = _now()
            save()
            log(f"Step {number} failed: {exc!r}", "SEVERE")
            raise
        entry.update(status="ok", finished=_now())
        save()
    record["finished"] = _now()
    save()
    log(f"Calibration completed; record written to {record_path}")
    return record


def parse_steps(text: str) -> list[int]:
    """'2,3,4' or '0-3,10' -> [0, 1, 2, 3, 10]."""
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        out.extend(range(int(lo), int(hi or lo) + 1))
    return out


def list_steps(cfg: Config | None = None) -> str:
    lines = [f"{n:>3}  {STEPS[n].title}" for n in sorted(STEPS)]
    if cfg is not None:
        lines += ["", f"msfile:  {cfg.msfile}", f"mssplit: {cfg.mssplit}",
                  f"uvfits:  {cfg.uvfits}", f"record:  {cfg.record_file}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration.py",
        description="EVLA calibration for 22A-388 (CASA 6.5.7).",
    )
    parser.add_argument("--config", type=Path, help="observation TOML file")
    parser.add_argument("--steps", type=parse_steps,
                        help="steps to run, e.g. '2,3,4' or '0-3,10'")
    parser.add_argument("--list-steps", action="store_true",
                        help="print the step table and exit")
    # parse_known_args: ignore flags casa itself may leave in sys.argv.
    args, _ = parser.parse_known_args(argv)
    cfg = load_config(args.config) if args.config else None
    if args.list_steps:
        print(list_steps(cfg))
        return 0
    if cfg is None:
        parser.error("--config is required to run steps")
    run(cfg, args.steps, args.config)
    return 0


def _script_argv() -> list[str]:
    """Arguments after this script's name (handles `casa ... -c calibration.py`)."""
    for i, arg in enumerate(sys.argv):
        if Path(arg).name == "calibration.py":
            return sys.argv[i + 1:]
    return sys.argv[1:]


def _invoked_as_script() -> bool:
    if __name__ == "__main__":
        return True
    return "casashell" in sys.modules and any(
        Path(a).name == "calibration.py" for a in sys.argv
    )


if _invoked_as_script():
    sys.exit(main(_script_argv()))
