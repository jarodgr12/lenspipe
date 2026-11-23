"""One file to send when something goes wrong: ``lenspipe diagnostics``.

``collect`` writes a zip holding what is needed to diagnose a failed or crashed run
without asking for anything else: the doctor checks, the config, the recent jobs
with their logs and parameters, the DifMAP logs (clipped to their start and end),
a listing of the project tree, a size check of every UV-FITS file against its own
header (a truncated file is what a hard crash leaves behind), and what the machine
says about itself (memory, disks, temperatures, the kernel messages of the boots
before this one).

``start_health_log`` is the other half: while a job runs it appends a line every
few seconds to ``<job>/health.jsonl`` (load, free memory, temperatures, disk) and
syncs it to disk, so the last minutes before a machine dies are on record.

Nothing here raises, and nothing leaves the machine: the zip is a local file.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lenspipe import __version__

__all__ = ["collect", "start_health_log", "uvfits_size_check"]

HEALTH_FILENAME = "health.jsonl"
HEALTH_INTERVAL_S = 15.0
COMMAND_TIMEOUT_S = 20
LOG_HEAD_BYTES = 100_000
LOG_TAIL_BYTES = 300_000
MAX_LOG_FILES = 300
MAX_TREE_ENTRIES_PER_DIR = 200
PREVIOUS_BOOTS = 3
ERROR_LINE = re.compile(r"error|cannot|can't|unable|no such|illegal|failed|abort|not found|denied|no space", re.I)
LOG_PATTERNS = ("*.difmap.log", "*.metadata.json", "plan.json", "shard_*.done.json")

# (archive name, argv). Each runs with a timeout; a missing program is recorded, not an error.
LINUX_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("uname.txt", ("uname", "-a")),
    ("lscpu.txt", ("lscpu",)),
    ("free.txt", ("free", "-h")),
    ("swapon.txt", ("swapon", "--show")),
    ("df.txt", ("df", "-hT")),
    ("lsblk.txt", ("lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,ROTA")),
    ("uptime.txt", ("uptime",)),
    ("sensors.txt", ("sensors",)),
    ("last-reboots.txt", ("last", "-x", "-n", "40", "reboot", "shutdown")),
    ("journal-boots.txt", ("journalctl", "--list-boots", "--no-pager")),
    ("journal-current-kernel-warnings.txt", ("journalctl", "-b", "0", "-k", "-p", "4", "-n", "400", "--no-pager")),
    ("dmesg-warnings.txt", ("dmesg", "--level=emerg,alert,crit,err,warn")),
    ("sar-cpu.txt", ("sar", "-u")),
    ("sar-memory.txt", ("sar", "-r")),
    ("sar-load.txt", ("sar", "-q")),
    ("sar-io.txt", ("sar", "-b")),
    ("sar-cpu-yesterday.txt", ("sar", "-u", "-1")),
    ("sar-memory-yesterday.txt", ("sar", "-r", "-1")),
    ("sar-io-yesterday.txt", ("sar", "-b", "-1")),
)
MAC_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("uname.txt", ("uname", "-a")),
    ("sw_vers.txt", ("sw_vers",)),
    ("cpu.txt", ("sysctl", "-n", "machdep.cpu.brand_string", "hw.ncpu", "hw.memsize")),
    ("vm_stat.txt", ("vm_stat",)),
    ("df.txt", ("df", "-h")),
    ("uptime.txt", ("uptime",)),
)
LINUX_FILES = ("/etc/os-release", "/proc/meminfo", "/proc/loadavg", "/proc/pressure/io", "/proc/pressure/memory")


def _now() -> datetime:
    return datetime.now(UTC)


def _read_text(path: Path, limit: int = 200_000) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _clip(path: Path, head: int = LOG_HEAD_BYTES, tail: int = LOG_TAIL_BYTES) -> bytes:
    """The start and end of a file; whole when it is small. DifMAP's first error is at the top."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= head + tail:
            return handle.read()
        first = handle.read(head)
        handle.seek(size - tail)
        last = handle.read()
    note = f"\n\n[... lenspipe diagnostics left out {size - head - tail} bytes of {size} ...]\n\n"
    return first + note.encode() + last


def _run(argv: tuple[str, ...]) -> str:
    if shutil.which(argv[0]) is None:
        return f"[{argv[0]} is not installed]\n"
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, errors="replace", timeout=COMMAND_TIMEOUT_S, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return f"[{' '.join(argv)} did not finish within {COMMAND_TIMEOUT_S} s]\n"
    except OSError as exc:
        return f"[{' '.join(argv)} could not run: {exc}]\n"
    text = done.stdout
    if done.stderr.strip():
        text += f"\n[stderr]\n{done.stderr}"
    if done.returncode != 0:
        text += f"\n[exit status {done.returncode}]\n"
    return text or "[no output]\n"


# --------------------------------------------------------------------------- health log


def _temperatures() -> dict[str, float]:
    """Hottest reading of each Linux hwmon chip (CPU package, NVMe, ...), in Celsius."""
    hottest: dict[str, float] = {}
    for chip in Path("/sys/class/hwmon").glob("hwmon*"):
        name = (_read_text(chip / "name", 100) or chip.name).strip()
        for reading in chip.glob("temp*_input"):
            try:
                value = int(reading.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            hottest[name] = max(hottest.get(name, value), value)
    return hottest


def _meminfo() -> dict[str, int]:
    """MemAvailable, SwapFree and friends from /proc/meminfo, in MiB."""
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty", "Writeback"}
    out: dict[str, int] = {}
    for line in (_read_text(Path("/proc/meminfo")) or "").splitlines():
        key, _, rest = line.partition(":")
        if key in wanted:
            try:
                out[key] = int(rest.split()[0]) // 1024
            except (ValueError, IndexError):
                continue
    return out


def _pressure(kind: str) -> float | None:
    """Share of the last 10 s in which some task waited on ``kind`` (io or memory)."""
    match = re.search(r"some avg10=([\d.]+)", _read_text(Path(f"/proc/pressure/{kind}"), 500) or "")
    return float(match.group(1)) if match else None


def health_sample(where: Path) -> dict[str, Any]:
    sample: dict[str, Any] = {"utc": _now().isoformat(timespec="seconds")}
    try:
        sample["load"] = [round(v, 2) for v in os.getloadavg()]
    except OSError:
        pass
    try:
        sample["disk_free_gib"] = round(shutil.disk_usage(where).free / 2**30, 2)
    except OSError:
        pass
    memory = _meminfo()
    if memory:
        sample["memory_mib"] = memory
    for kind in ("io", "memory"):
        value = _pressure(kind)
        if value is not None:
            sample[f"{kind}_pressure"] = value
    temperatures = _temperatures()
    if temperatures:
        sample["temp_c"] = temperatures
    return sample


def start_health_log(job_dir: Path, interval: float = HEALTH_INTERVAL_S) -> threading.Event:
    """Append a health sample to ``<job_dir>/health.jsonl`` every ``interval`` seconds until the
    process exits or the returned event is set. Each line is synced to disk so it survives a
    machine that dies without warning."""
    stop = threading.Event()

    def loop() -> None:
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            with (job_dir / HEALTH_FILENAME).open("a", encoding="utf-8") as handle:
                while True:
                    handle.write(json.dumps(health_sample(job_dir)) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                    if stop.wait(interval):
                        return
        except OSError:
            return

    threading.Thread(target=loop, name="lenspipe-health", daemon=True).start()
    return stop


# --------------------------------------------------------------------------- UV-FITS size check


def uvfits_size_check(path: Path) -> dict[str, Any]:
    """Compare a UV-FITS file's size with what its own header says it holds."""
    report: dict[str, Any] = {"path": str(path)}
    if path.is_symlink():
        report["link_target"] = os.readlink(path)
    if not path.exists():
        report["verdict"] = "MISSING (broken link)" if path.is_symlink() else "MISSING"
        return report
    try:
        from astropy.io import fits

        size = path.stat().st_size
        report["size_bytes"] = size
        with path.open("rb") as raw:
            header = fits.Header.fromfile(raw)
            data_offset = raw.tell()
        cells = 1
        for axis in range(2, int(header["NAXIS"]) + 1):
            cells *= int(header[f"NAXIS{axis}"])
        data = abs(int(header["BITPIX"])) // 8 * int(header.get("GCOUNT", 1)) * (int(header.get("PCOUNT", 0)) + cells)
        report["expected_at_least_bytes"] = data_offset + data
        report["groups"] = int(header.get("GCOUNT", 1))
        report["axes"] = {
            str(header.get(f"CTYPE{axis}", axis)).strip(): int(header[f"NAXIS{axis}"])
            for axis in range(2, int(header["NAXIS"]) + 1)
        }
        short = data_offset + data - size
        report["verdict"] = f"TRUNCATED: {short} bytes short of what the header declares" if short > 0 else "ok"
    except Exception as exc:  # noqa: BLE001 - an unreadable header is itself the finding
        report["verdict"] = f"UNREADABLE: {exc}"
    return report


# --------------------------------------------------------------------------- collection


def _tree(root: Path) -> str:
    """Every directory of the project with its files' sizes and times; long directories are cut."""
    lines: list[str] = []
    for directory, subdirs, files in os.walk(root):
        subdirs.sort()
        here = Path(directory)
        entries = sorted(files)
        total = 0
        rows: list[str] = []
        for name in entries:
            path = here / name
            try:
                info = path.lstat()
            except OSError:
                continue
            total += info.st_size
            if len(rows) < MAX_TREE_ENTRIES_PER_DIR:
                stamp = datetime.fromtimestamp(info.st_mtime, UTC).strftime("%Y-%m-%d %H:%M:%S")
                target = ""
                if path.is_symlink():
                    target = f" -> {os.readlink(path)}" + ("" if path.exists() else "  [BROKEN LINK]")
                rows.append(f"  {info.st_size:>14}  {stamp}  {name}{target}")
        relative = here.relative_to(root)
        lines.append(f"{relative}/  ({len(entries)} file(s), {total} bytes)")
        lines.extend(rows)
        if len(entries) > len(rows):
            lines.append(f"  ... {len(entries) - len(rows)} more file(s) not listed")
    return "\n".join(lines) + "\n"


def _log_files(roots: list[Path]) -> list[Path]:
    found: dict[Path, float] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in LOG_PATTERNS:
            for path in root.rglob(pattern):
                try:
                    found[path] = path.stat().st_mtime
                except OSError:
                    continue
    return sorted(found, key=lambda p: found[p], reverse=True)[:MAX_LOG_FILES]


def _error_lines(text: str, limit: int = 6) -> list[str]:
    hits = [line.strip() for line in text.splitlines() if ERROR_LINE.search(line)]
    return hits[:limit]


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Bundle:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.names: list[str] = []
        self.problems: list[str] = []

    def add(self, name: str, content: str | bytes) -> None:
        self.archive.writestr(name, content)
        self.names.append(name)

    def attempt(self, name: str, make: Any) -> None:
        try:
            self.add(name, make())
        except Exception as exc:  # noqa: BLE001 - one unreadable item must not lose the rest
            self.problems.append(f"{name}: {type(exc).__name__}: {exc}")


def _collect_system(bundle: _Bundle) -> None:
    commands = MAC_COMMANDS if sys.platform == "darwin" else LINUX_COMMANDS
    for name, argv in commands:
        bundle.attempt(f"system/{name}", lambda argv=argv: _run(argv))
    if sys.platform.startswith("linux"):
        for boot in range(1, PREVIOUS_BOOTS + 1):
            # The end of the kernel log of an earlier boot is what the machine said before it went down.
            bundle.attempt(
                f"system/journal-boot-minus-{boot}-kernel-tail.txt",
                lambda boot=boot: _run(("journalctl", "-b", f"-{boot}", "-k", "-n", "400", "--no-pager")),
            )
            bundle.attempt(
                f"system/journal-boot-minus-{boot}-errors.txt",
                lambda boot=boot: _run(("journalctl", "-b", f"-{boot}", "-p", "3", "-n", "300", "--no-pager")),
            )
        for source in LINUX_FILES:
            text = _read_text(Path(source))
            if text is not None:
                bundle.add("system/" + source.strip("/").replace("/", "_") + ".txt", text)
    bundle.attempt("system/health-now.json", lambda: json.dumps(health_sample(Path.home()), indent=1))
    bundle.attempt("system/uv-tools.txt", lambda: _run(("uv", "tool", "list")))
    home = Path.home() / ".lenspipe"
    if home.is_dir():
        for path in sorted(home.iterdir()):
            if path.is_file() and path.stat().st_size < 1_000_000:
                bundle.attempt(f"home-lenspipe/{path.name}", lambda path=path: path.read_bytes())


def _collect_jobs(bundle: _Bundle, jobs_dir: Path, limit: int, findings: list[str]) -> None:
    if not jobs_dir.is_dir():
        return
    directories = sorted(
        (d for d in jobs_dir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True
    )[:limit]
    for directory in directories:
        for path in sorted(directory.iterdir()):
            if path.is_file():
                bundle.attempt(f"jobs/{directory.name}/{path.name}", lambda path=path: _clip(path, 200_000, 800_000))
        try:
            record = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status, pid = record.get("status"), record.get("pid")
        line = f"job {directory.name}: {status}"
        if status == "running" and not _pid_alive(pid):
            line += " but its process is gone (the machine or the console went down mid-run)"
        elif record.get("error"):
            line += f" ({record['error']})"
        if status != "completed":
            findings.append(line)


def _collect_project(bundle: _Bundle, root: Path, jobs: int, findings: list[str]) -> None:
    from lenspipe.config import CONFIG_FILENAME, load_config
    from lenspipe.doctor import run_checks
    from lenspipe.project import Layout

    layout = Layout.at(root)
    config = None
    try:
        config = load_config(layout.root, None)
    except Exception as exc:  # noqa: BLE001 - a broken config is a finding
        findings.append(f"config does not load: {exc}")
    try:
        checks = run_checks(layout.root, config)
        bundle.add("doctor.json", json.dumps([c.to_dict() for c in checks], indent=1))
        findings.extend(f"doctor {c.status.upper()} {c.name}: {c.detail}" for c in checks if c.status != "ok")
    except Exception as exc:  # noqa: BLE001 - the rest of the bundle matters more
        bundle.problems.append(f"doctor.json: {type(exc).__name__}: {exc}")

    config_text = _read_text(layout.config_path)
    if config_text is not None:
        bundle.add(f"project/{CONFIG_FILENAME}", config_text)
    bundle.attempt("project/tree.txt", lambda: _tree(layout.root))

    uvfits = sorted(layout.inputs.glob("*.uvfits")) if layout.inputs.is_dir() else []
    uvfits += sorted(layout.root.glob("stage1/*/*/*.cal.uvf")) + sorted(layout.root.glob("runs/*/stage1/*/*/*.cal.uvf"))
    reports = [uvfits_size_check(path) for path in uvfits]
    bundle.add("project/uvfits-check.json", json.dumps(reports, indent=1))
    findings.extend(f"{r['path']}: {r['verdict']}" for r in reports if r["verdict"] != "ok")

    try:
        _collect_jobs(bundle, layout.jobs, jobs, findings)
    except Exception as exc:  # noqa: BLE001
        bundle.problems.append(f"jobs: {type(exc).__name__}: {exc}")

    roots = [layout.stage1, layout.stage2, layout.stage3, layout.root / "runs"]
    if config is not None and config.stage2.scratch_dir:
        roots.append(Path(config.stage2.scratch_dir).expanduser())
    for path in _log_files(roots):
        try:
            name = "logs/" + str(path.relative_to(layout.root))
        except ValueError:
            name = "logs/outside-project/" + str(path).strip("/")
        try:
            content = _clip(path)
        except OSError as exc:
            bundle.problems.append(f"{name}: {exc}")
            continue
        bundle.add(name, content)
        if path.name.endswith(".difmap.log"):
            hits = _error_lines(content[:LOG_HEAD_BYTES].decode("utf-8", errors="replace"))
            if hits:
                findings.append(f"{name}: first error lines: " + " | ".join(hits))


def collect(project_root: Path | None, *, out_dir: Path | None = None, jobs: int = 10) -> Path:
    """Write the diagnostics zip and return its path. ``project_root`` None collects the machine only."""
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname() or "host")
    where = Path(out_dir).expanduser() if out_dir else (Path(project_root) if project_root else Path.cwd())
    where.mkdir(parents=True, exist_ok=True)
    target = where / f"lenspipe-diagnostics-{host}-{stamp}.zip"
    findings: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        bundle = _Bundle(archive)
        if project_root is not None:
            try:
                _collect_project(bundle, Path(project_root), jobs, findings)
            except Exception as exc:  # noqa: BLE001 - still deliver the machine part
                bundle.problems.append(f"project: {type(exc).__name__}: {exc}")
        else:
            from lenspipe.doctor import run_checks

            bundle.attempt("doctor.json", lambda: json.dumps([c.to_dict() for c in run_checks(None)], indent=1))
        _collect_system(bundle)
        summary = [
            f"lenspipe diagnostics, {_now().isoformat(timespec='seconds')}",
            f"lenspipe {__version__}, Python {sys.version.split()[0]}, {platform.platform()}",
            f"host {socket.gethostname()}, project {project_root or '(none)'}",
            "",
            "Things that look wrong:" if findings else "Nothing was flagged automatically.",
            *(f"  - {line}" for line in findings),
            "",
            "Could not be collected:" if bundle.problems else "Everything was collected.",
            *(f"  - {line}" for line in bundle.problems),
            "",
            f"{len(bundle.names)} file(s) in this archive:",
            *(f"  {name}" for name in bundle.names),
        ]
        archive.writestr("SUMMARY.txt", "\n".join(summary) + "\n")
    return target
