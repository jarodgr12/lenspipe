"""scripts/validate.sh runs end to end against a project with legacy-style products."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "validate.sh"


@pytest.fixture
def reference_project(project: Path, fake_difmap: Path) -> Path:
    """A project whose products play the role of the legacy reference."""
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg.with_overrides(stage2={"mode": "channel", "shards": 1}), reporter=quiet, workers=1)
    assert run_stage3(project, cfg.with_overrides(stage3={"figure_formats": ["png"]}), reporter=quiet, workers=1).ok
    return project


def _env(fake_difmap_env: dict[str, str]) -> dict[str, str]:
    env = dict(fake_difmap_env)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"  # lenspipe console script
    return env


def test_validate_script_passes_on_matching_products(reference_project: Path, fake_difmap_env, tmp_path: Path) -> None:
    work = tmp_path / "work"
    completed = subprocess.run(
        ["bash", str(SCRIPT), str(reference_project), "--work", str(work)],
        env=_env(fake_difmap_env), text=True, capture_output=True, check=False,
    )
    report = (work / "validation-report.txt").read_text() if (work / "validation-report.txt").exists() else ""
    assert completed.returncode == 0, completed.stdout[-3000:] + completed.stderr[-2000:]
    assert "RESULT: PASS" in report
    assert "product:  channel" in report
    assert "stage 2 wall time, 1 shard" in report
    assert report.count("compare stages") == 4
    assert (work / "inputs" / "MG0414.A.uvfits").is_symlink()


def test_validate_script_fails_when_products_differ(reference_project: Path, fake_difmap_env, tmp_path: Path) -> None:
    # Corrupt one legacy Stage 2 value so the comparison must fail.
    csv = reference_project / "stage2" / "MG0414.B" / "MG0414.B.channel.spectrum.csv"
    lines = csv.read_text().splitlines()
    cells = lines[2].split(",")
    cells[lines[0].split(",").index("B_jy")] = "0.5"
    lines[2] = ",".join(cells)
    csv.write_text("\n".join(lines) + "\n")

    work = tmp_path / "work"
    completed = subprocess.run(
        ["bash", str(SCRIPT), str(reference_project), "--work", str(work), "--epoch", "B"],
        env=_env(fake_difmap_env), text=True, capture_output=True, check=False,
    )
    report = (work / "validation-report.txt").read_text()
    assert completed.returncode == 1
    assert "RESULT: FAIL" in report and "stage2 (1 shard) products differ" in report
    assert "B_jy" in report


def test_validate_script_refuses_bad_arguments(tmp_path: Path, fake_difmap_env) -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "nowhere")], env=_env(fake_difmap_env),
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 2
