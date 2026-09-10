"""Environment detection and health checks: what is installed, what is missing, how to fix it."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from lenspipe import __version__
from lenspipe.config import CONFIG_FILENAME, LenspipeConfig
from lenspipe.difmap.runner import difmap_version, resolve_executable
from lenspipe.project import Layout, decide_shards, physical_memory_bytes

__all__ = ["CheckResult", "detect_casa", "detect_difmap", "run_checks"]

OK, WARN, FAIL = "ok", "warn", "fail"
CASA_CANDIDATES = ("casa", "casa6", "casapy")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def detect_difmap(configured: str = "difmap") -> str | None:
    """Return a runnable DifMAP path from the configured name or common locations."""
    candidates = [configured, "difmap"]
    candidates += [
        str(p) for p in (
            Path.home() / "difmap" / "difmap",
            Path("/usr/local/bin/difmap"),
            Path("/opt/difmap/bin/difmap"),
            Path("/usr/local/difmap/difmap"),
        )
    ]
    for candidate in candidates:
        try:
            return resolve_executable(candidate)
        except FileNotFoundError:
            continue
    return None


def detect_casa() -> str | None:
    """Return an interpreter command that can run the CASA calibration script, if any."""
    for name in CASA_CANDIDATES:
        found = shutil.which(name)
        if found:
            return f"{found} --nogui --nologger -c"
    for pattern in ("/opt/casa*/bin/casa", str(Path.home() / "casa*" / "bin" / "casa")):
        matches = sorted(Path("/").glob(pattern.lstrip("/"))) if pattern.startswith("/opt") else sorted(
            Path.home().glob(pattern[len(str(Path.home())) + 1 :])
        )
        if matches:
            return f"{matches[-1]} --nogui --nologger -c"
    if importlib.util.find_spec("casatasks") is not None:
        return f"{sys.executable}"
    return None


def _human(n_bytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n_bytes < 1024 or unit == "TiB":
            return f"{n_bytes:.1f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} TiB"


def run_checks(project_root: Path | None, config: LenspipeConfig | None = None) -> list[CheckResult]:
    """Check the machine and, when given, the project. Never raises."""
    config = config or LenspipeConfig()
    results: list[CheckResult] = []

    version = sys.version.split()[0]
    results.append(
        CheckResult(
            "python", OK if sys.version_info >= (3, 11) else FAIL, f"{version} at {sys.executable}",
            None if sys.version_info >= (3, 11) else "lenspipe needs Python 3.11 or newer",
        )
    )
    results.append(CheckResult("lenspipe", OK, f"version {__version__}"))

    difmap_path = detect_difmap(config.project.difmap.executable)
    if difmap_path is None:
        results.append(
            CheckResult(
                "difmap", FAIL, f"not found (configured name: {config.project.difmap.executable!r})",
                "install DifMAP and put it on the PATH, or set [project.difmap] executable in "
                f"{CONFIG_FILENAME}",
            )
        )
    else:
        info = difmap_version(difmap_path)
        if info.get("version"):
            results.append(CheckResult("difmap", OK, f"{difmap_path}, version {info['version']}"))
        else:
            banner = info.get("banner") or "no output"
            results.append(
                CheckResult(
                    "difmap", WARN, f"{difmap_path} started but printed no DifMAP banner ({banner[:60]})",
                    "run the executable by hand and check it starts as DifMAP",
                )
            )

    casa_command = config.casa.interpreter or detect_casa()
    if casa_command:
        first = casa_command.split()[0]
        runnable = shutil.which(first) or (Path(first).is_file() and os.access(first, os.X_OK))
        results.append(
            CheckResult(
                "casa", OK if runnable else WARN, casa_command,
                None if runnable else "the configured [casa] interpreter is not executable",
            )
        )
    else:
        results.append(
            CheckResult("casa", WARN, "not found; calibration jobs are disabled",
                        "install CASA, or set [casa] interpreter if it lives somewhere unusual")
        )

    results.append(
        CheckResult(
            "emcee", OK if importlib.util.find_spec("emcee") else WARN,
            "installed" if importlib.util.find_spec("emcee") else "not installed; Bayesian fits unavailable",
            None if importlib.util.find_spec("emcee") else "uv sync --all-extras  (or pip install emcee)",
        )
    )

    try:
        from matplotlib import font_manager

        stix = font_manager.findfont("STIXGeneral", fallback_to_default=False)
        results.append(CheckResult("fonts", OK, f"STIXGeneral at {Path(stix).name}"))
    except Exception:  # noqa: BLE001 - any font failure is a warning
        results.append(CheckResult("fonts", WARN, "STIXGeneral not found; plots fall back to DejaVu",
                                   "install the STIX fonts or accept the fallback"))

    cores = os.cpu_count() or 1
    memory = physical_memory_bytes()
    results.append(
        CheckResult("machine", OK, f"{cores} cores, {_human(memory) if memory else 'unknown'} RAM")
    )

    if project_root is None:
        return results

    layout = Layout.at(project_root)
    if not layout.root.is_dir():
        results.append(CheckResult("project", FAIL, f"{layout.root} does not exist",
                                   "lenspipe init <project> --inputs <dir with .uvfits and .gmod>"))
        return results
    results.append(
        CheckResult(
            "config", OK if layout.config_path.is_file() else WARN,
            str(layout.config_path) if layout.config_path.is_file() else "no lenspipe.toml; defaults apply",
            None if layout.config_path.is_file() else f"lenspipe init {layout.root}",
        )
    )
    uvfits = sorted(layout.inputs.glob("*.uvfits")) if layout.inputs.is_dir() else []
    models = sorted(layout.inputs.glob("*.gmod")) if layout.inputs.is_dir() else []
    if not uvfits:
        results.append(CheckResult("inputs", FAIL, f"no *.uvfits in {layout.inputs}",
                                   "copy or link <source>.<epoch>.uvfits files into inputs/"))
    else:
        sources = {p.name.rsplit(".", 2)[0] for p in uvfits}
        missing = sorted(s for s in sources if not (layout.inputs / f"{s}.gmod").is_file())
        status = FAIL if missing else OK
        detail = f"{len(uvfits)} UV-FITS file(s), {len(models)} master model(s)"
        results.append(
            CheckResult("inputs", status, detail,
                        f"add inputs/{missing[0]}.gmod with GROUP/COMPONENT labels" if missing else None)
        )
        largest = max(p.stat().st_size for p in uvfits)
        decision = decide_shards(
            config.stage2.shards, 3072, input_bytes=largest,
            epoch_workers=config.run.epoch_workers,
            memory_fraction=config.stage2.memory_fraction,
            memory_multiple=config.stage2.memory_multiple,
        )
        results.append(
            CheckResult(
                "sharding", OK,
                f"largest input {_human(largest)}; Stage 2 would use {decision.shards} shard(s) per epoch "
                f"with {config.run.epoch_workers} epoch(s) at once "
                f"(memory multiple {decision.memory_multiple:g}: {decision.memory_multiple_source})",
                None if decision.memory_multiple_source and "measured" in decision.memory_multiple_source
                else "the first Stage 2 run measures DifMAP's real footprint; later runs size shards from it",
            )
        )
    try:
        usage = shutil.disk_usage(layout.root)
        free_ok = usage.free > 5 * (1 << 30)
        results.append(
            CheckResult("disk", OK if free_ok else WARN, f"{_human(usage.free)} free at {layout.root}",
                        None if free_ok else "less than 5 GiB free; Stage 1 writes a calibrated copy per epoch")
        )
    except OSError:
        pass
    return results
