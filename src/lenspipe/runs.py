"""Runs: named parameter sets with their own output trees inside one project.

A run is ``<project>/runs/<label>/``, a small project of its own::

    runs/<label>/lenspipe.toml   a frozen copy of the project's config, taken when the run was created
    runs/<label>/inputs          a link to the project's inputs/ (same data, nothing copied)
    runs/<label>/run.json        label, parent project, creation time
    runs/<label>/stage1/ ...     its own stage trees, laid out exactly like the project's

Because a run root is a project root, every stage command works on it unchanged;
``--run <label>`` on the CLI and the "Run label" field on the Run page only pick
the root. Queued jobs for different runs therefore read different frozen
configs and write to different directories, which is what makes it safe to set
up a parameter set, queue it, change the Parameters page, and queue another.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lenspipe import __version__
from lenspipe.config import CONFIG_FILENAME, default_toml

__all__ = [
    "RUNS_DIRNAME",
    "RUN_MARKER",
    "RunInfo",
    "create_run",
    "is_run_root",
    "list_runs",
    "product_counts",
    "refreeze_run",
    "run_root",
    "validate_label",
]

RUNS_DIRNAME = "runs"
RUN_MARKER = "run.json"
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_label(label: str) -> str:
    """A run label names a directory: letters, digits, '.', '_' and '-', 64 characters at most."""
    text = (label or "").strip()
    if not _LABEL.match(text) or text in {".", ".."}:
        raise ValueError(
            f"run label {label!r} is not valid: use letters, digits, '.', '_' or '-' (start with a letter or "
            "digit, at most 64 characters), e.g. 'sched-A' or 'edge2_gscale'"
        )
    return text


def run_root(project_root: Path | str, label: str) -> Path:
    return Path(project_root).expanduser().resolve() / RUNS_DIRNAME / validate_label(label)


def is_run_root(path: Path | str) -> bool:
    return (Path(path) / RUN_MARKER).is_file()


def _read_marker(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / RUN_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _config_text(root: Path) -> str:
    """The config a stage would read at ``root``: the file, or the defaults when there is none."""
    path = root / CONFIG_FILENAME
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default_toml()


@dataclass(frozen=True)
class RunInfo:
    label: str
    root: Path
    parent: Path
    created_utc: str | None

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILENAME

    def config_diff(self) -> str:
        """Unified diff from the run's frozen config to the project's current one ('' when identical)."""
        frozen = _config_text(self.root).splitlines(keepends=True)
        current = _config_text(self.parent).splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                frozen, current, fromfile=f"runs/{self.label}/{CONFIG_FILENAME}", tofile=CONFIG_FILENAME
            )
        )

    @property
    def config_differs(self) -> bool:
        return _config_text(self.root) != _config_text(self.parent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "root": str(self.root),
            "created_utc": self.created_utc,
            "config_differs": self.config_differs,
            **product_counts(self.root),
        }


def _info(parent: Path, root: Path) -> RunInfo:
    marker = _read_marker(root)
    return RunInfo(
        label=str(marker.get("label") or root.name), root=root, parent=parent,
        created_utc=marker.get("created_utc"),
    )


def list_runs(project_root: Path | str) -> list[RunInfo]:
    """Every run under ``<project>/runs/``, by label."""
    parent = Path(project_root).expanduser().resolve()
    runs = parent / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [_info(parent, child) for child in sorted(runs.iterdir()) if child.is_dir() and is_run_root(child)]
    return sorted(found, key=lambda item: item.label)


def get_run(project_root: Path | str, label: str) -> RunInfo | None:
    root = run_root(project_root, label)
    return _info(root.parent.parent, root) if is_run_root(root) else None


def create_run(project_root: Path | str, label: str) -> tuple[RunInfo, bool]:
    """Create ``runs/<label>/`` with the project's config frozen now; returns ``(run, created)``.

    An existing run is returned untouched (its frozen config is never silently
    replaced; see :func:`refreeze_run`). Runs cannot be nested: the project root
    must not itself be a run.
    """
    from lenspipe.project import utc_now

    parent = Path(project_root).expanduser().resolve()
    label = validate_label(label)
    if is_run_root(parent):
        raise ValueError(f"{parent} is already a run ({parent.name}); runs cannot be nested. Use its parent project.")
    inputs = parent / "inputs"
    if not inputs.is_dir():
        raise FileNotFoundError(f"{parent} has no inputs/ directory to share with run {label!r}.")
    root = parent / RUNS_DIRNAME / label
    if is_run_root(root):
        return _info(parent, root), False
    root.mkdir(parents=True, exist_ok=True)
    (root / CONFIG_FILENAME).write_text(_config_text(parent), encoding="utf-8")
    link = root / "inputs"
    if not link.exists() and not link.is_symlink():
        os.symlink(os.path.join(os.pardir, os.pardir, "inputs"), link, target_is_directory=True)
    (root / RUN_MARKER).write_text(
        json.dumps(
            {"label": label, "parent": str(parent), "created_utc": utc_now(), "lenspipe_version": __version__},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return _info(parent, root), True


def refreeze_run(project_root: Path | str, label: str) -> RunInfo:
    """Replace the run's frozen config with the project's current one (an explicit choice)."""
    run = get_run(project_root, label)
    if run is None:
        raise FileNotFoundError(f"run {label!r} does not exist under {project_root}")
    run.config_path.write_text(_config_text(run.parent), encoding="utf-8")
    return run


def product_counts(root: Path | str) -> dict[str, int]:
    """How many Stage 1, 2 and 3 products (and combined sets) a project or run holds."""
    from lenspipe.project import Layout

    layout = Layout.at(root)
    counts = {"stage1": 0, "stage2": 0, "stage3": 0, "combined": 0}
    for mode in layout.modes_present(1):
        counts["stage1"] += sum(1 for _ in layout.stage_dir(1, mode).glob("*/*.stage1.metadata.json"))
    for mode in layout.modes_present(2):
        counts["stage2"] += sum(1 for _ in layout.stage_dir(2, mode).glob("*/*.stage2.metadata.json"))
    for mode in layout.modes_present(3):
        tree = layout.stage_dir(3, mode)
        for meta in tree.glob("*/*/**/*.stage3.metadata.json"):
            if meta.relative_to(tree).parts[0] == "combined":
                continue
            counts["stage3"] += 1
        counts["combined"] += sum(1 for _ in tree.glob("combined/**/*.combined.stage3.metadata.json"))
    return counts
