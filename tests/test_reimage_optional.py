"""2.4.1: the second, common-grid imaging pass of Stage 1 can be switched off."""

from __future__ import annotations

import io
import json
from pathlib import Path

from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage3.images import stage1_images_for
from lenspipe.ui.commands import RunRequest, build_steps

runner = CliRunner()


def test_stage1_without_reimage_keeps_only_native_maps(project: Path, fake_difmap: Path) -> None:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}}, stage1={"reimage": False})
    results = run_stage1(project, cfg, reporter=Reporter(stream=io.StringIO()), workers=1, epochs={"A"})
    assert [r.status for r in results] == ["completed"]  # no "reimaged" result
    d = project / "stage1" / "full-band" / "MG0414.A"
    assert (d / "MG0414.A.native.cln.fits").is_file() and not (d / "MG0414.A.cln.fits").exists()
    meta = json.loads((d / "MG0414.A.stage1.metadata.json").read_text())
    assert meta["common_image_geometry"] is None and meta["final_residual_rms_jy_per_beam"] is None
    # Stage 3 map figures fall back to the native maps.
    clean, residual = stage1_images_for(project, "MG0414", "A", "full-band")
    assert clean.name == "MG0414.A.native.cln.fits" and residual.name == "MG0414.A.native.resid.fits"

    # The pass can still be run afterwards on request, and then its maps take precedence.
    again = run_stage1(project, cfg, reporter=Reporter(stream=io.StringIO()), workers=1, reimage_only=True)
    assert [r.status for r in again] == ["reimaged"]
    assert stage1_images_for(project, "MG0414", "A", "full-band")[0].name == "MG0414.A.cln.fits"


def test_stage1_cli_and_run_page_flags(project: Path, fake_difmap: Path, tmp_path: Path) -> None:
    common = ["stage1", str(project), "--difmap", str(fake_difmap), "--epoch", "A", "--dry-run"]
    assert "common-grid imaging commands" in runner.invoke(app, common).output
    off = runner.invoke(app, [*common, "--no-reimage"])
    assert off.exit_code == 0 and "common-grid imaging commands" not in off.output
    (project / "lenspipe.toml").write_text("[stage1]\nreimage = false\n")
    assert "common-grid imaging commands" not in runner.invoke(app, common).output
    assert "common-grid imaging commands" in runner.invoke(app, [*common, "--reimage"]).output

    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_reimage=False))
    assert steps[0].argv == ["stage1", str(tmp_path), "--no-reimage"]
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1], stage1_reimage=True))
    assert steps[0].argv == ["stage1", str(tmp_path), "--reimage"]
    assert build_steps(RunRequest(root=tmp_path, stages=[1]))[0][0].argv == ["stage1", str(tmp_path)]
