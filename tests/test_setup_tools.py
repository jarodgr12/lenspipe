"""Doctor, init --inputs, and compare: the scientist-facing setup and validation path."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.compare import compare_projects
from lenspipe.config import LenspipeConfig, render_toml
from lenspipe.doctor import detect_difmap, run_checks
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from tests.conftest import REPO_ROOT

runner = CliRunner()


def _config(fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}})
    return cfg.with_overrides(stage2=stage2) if stage2 else cfg


# ---------------------------------------------------------------------------
# doctor


def test_doctor_reports_missing_difmap_as_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on the PATH
    checks = {c.name: c for c in run_checks(None, LenspipeConfig())}
    assert checks["python"].status == "ok"
    assert checks["difmap"].status == "fail" and "PATH" in (checks["difmap"].fix or "")
    assert checks["casa"].status == "warn"
    assert "cores" in checks["machine"].detail


def test_doctor_warns_when_executable_is_not_difmap(tmp_path: Path, monkeypatch) -> None:
    impostor = tmp_path / "difmap"
    impostor.write_text("#!/bin/sh\necho hello\n")
    impostor.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    checks = {c.name: c for c in run_checks(None, LenspipeConfig())}
    assert checks["difmap"].status == "warn" and "did not identify itself as DifMAP" in checks["difmap"].detail


def test_doctor_finds_configured_difmap_and_project_problems(project: Path, fake_difmap: Path, monkeypatch) -> None:
    # No difmap on the PATH, but keep python3 so the fake's shebang can run.
    monkeypatch.setenv("PATH", f"{project}{os.pathsep}{Path(sys.executable).parent}")
    cfg = _config(fake_difmap)
    checks = {c.name: c for c in run_checks(project, cfg)}
    assert checks["difmap"].status == "ok" and "2.5k" in checks["difmap"].detail
    assert checks["inputs"].status == "ok" and "2 UV-FITS" in checks["inputs"].detail
    assert checks["config"].status == "warn"  # no lenspipe.toml yet
    assert "shard" in checks["sharding"].detail

    (project / "inputs" / "MG0414.gmod").unlink()
    checks = {c.name: c for c in run_checks(project, cfg)}
    assert checks["inputs"].status == "fail" and "MG0414.gmod" in (checks["inputs"].fix or "")


def test_doctor_cli_exit_code(project: Path, fake_difmap_env) -> None:
    env_backup = dict(os.environ)
    os.environ.update(fake_difmap_env)
    try:
        result = runner.invoke(app, ["doctor", str(project)])
        assert result.exit_code == 0, result.output
        assert "OK    difmap" in result.output
        result = runner.invoke(app, ["doctor", str(project / "missing"), "--json"])
        assert result.exit_code == 1
        assert any(c["name"] == "project" and c["status"] == "fail" for c in json.loads(result.output))
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def test_detect_difmap_prefers_configured_path(fake_difmap: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert detect_difmap(str(fake_difmap)) == str(fake_difmap)
    assert detect_difmap("nonexistent-difmap") is None


# ---------------------------------------------------------------------------
# init --inputs


def test_init_links_inputs_and_writes_detected_config(project: Path, fake_difmap_env, tmp_path: Path) -> None:
    env_backup = dict(os.environ)
    os.environ.update(fake_difmap_env)
    try:
        new_root = tmp_path / "newproj"
        result = runner.invoke(app, ["init", str(new_root), "--inputs", str(project / "inputs")])
        assert result.exit_code == 0, result.output
        linked = sorted(p.name for p in (new_root / "inputs").iterdir())
        assert linked == ["MG0414.A.uvfits", "MG0414.B.uvfits", "MG0414.gmod"]
        assert all((new_root / "inputs" / name).is_symlink() for name in linked)
        text = (new_root / "lenspipe.toml").read_text()
        cfg = LenspipeConfig.model_validate(tomllib.loads(text))
        assert cfg.project.source == "MG0414"
        assert cfg.project.difmap.executable.endswith("difmap")
        assert "# Every value here is the default" in text  # comments preserved
        assert "Next: lenspipe doctor" in result.output

        copied_root = tmp_path / "copied"
        result = runner.invoke(app, ["init", str(copied_root), "--inputs", str(project / "inputs"), "--copy", "--no-detect"])
        assert result.exit_code == 0
        assert not (copied_root / "inputs" / "MG0414.gmod").is_symlink()
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def test_pipeline_runs_on_symlinked_inputs(project: Path, fake_difmap: Path, tmp_path: Path) -> None:
    """init --inputs links files from an archive; every stage must cope with that."""
    quiet = Reporter(stream=io.StringIO())
    linked = tmp_path / "linked"
    (linked / "inputs").mkdir(parents=True)
    for path in (project / "inputs").iterdir():
        (linked / "inputs" / path.name).symlink_to(path)
    cfg = _config(fake_difmap, mode="channel", shards=2)
    stage1 = run_stage1(linked, cfg, reporter=quiet, workers=1)
    assert all(r.ok for r in stage1), [r.message for r in stage1]
    meta = json.loads((linked / "stage1" / "full-band" / "MG0414.A" / "MG0414.A.stage1.metadata.json").read_text())
    assert meta["input_uvfits"] == "inputs/MG0414.A.uvfits"
    assert meta["input_uvfits_resolved"] == str((project / "inputs" / "MG0414.A.uvfits").resolve())
    stage2 = run_stage2(linked, cfg, reporter=quiet, workers=1)
    assert all(r.ok for r in stage2), [r.message for r in stage2]
    from lenspipe.stage3 import run_stage3

    assert run_stage3(linked, cfg.with_overrides(stage3={"figure_formats": ["png"]}), reporter=quiet, workers=1).ok
    assert (linked / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.spectrum.csv").is_file()


def test_render_toml_fills_values_and_stays_valid() -> None:
    text = render_toml(source="B1555", difmap_executable="/opt/difmap/difmap",
                       casa_interpreter="casa --nogui -c", casa_script="/x/calibration.py")
    cfg = LenspipeConfig.model_validate(tomllib.loads(text))
    assert cfg.project.source == "B1555"
    assert cfg.project.difmap.executable == "/opt/difmap/difmap"
    assert cfg.casa.interpreter == "casa --nogui -c" and cfg.casa.script == "/x/calibration.py"
    assert LenspipeConfig.model_validate(tomllib.loads(render_toml())) == LenspipeConfig()


# ---------------------------------------------------------------------------
# compare


def test_compare_identical_and_perturbed_projects(project: Path, fake_difmap: Path, tmp_path: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1)
    from lenspipe.stage3 import run_stage3

    assert run_stage3(project, _config(fake_difmap).with_overrides(stage3={"figure_formats": ["png"]}),
                      reporter=quiet, workers=1).ok
    twin = tmp_path / "twin"
    shutil.copytree(project, twin)

    diffs = compare_projects(project, twin)
    assert diffs and all(d.ok for d in diffs)
    assert {d.stage for d in diffs} == {1, 2, 3}

    # Perturb one flux value in the twin's Stage 2 CSV by 1 percent.
    csv = twin / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.spectrum.csv"
    lines = csv.read_text().splitlines()
    header = lines[0].split(",")
    column = header.index("A1_jy")
    cells = lines[1].split(",")
    cells[column] = f"{float(cells[column]) * 1.01:.12g}"
    lines[1] = ",".join(cells)
    csv.write_text("\n".join(lines) + "\n")

    diffs = compare_projects(project, twin)
    bad = [d for d in diffs if not d.ok]
    assert len(bad) == 1 and bad[0].item == "MG0414.A.channel.spectrum.csv"
    assert "A1_jy" in bad[0].columns and "relative difference 0.0099" in bad[0].detail

    result = runner.invoke(app, ["compare", str(project), str(twin)])
    assert result.exit_code == 4 and "different" in result.output
    result = runner.invoke(app, ["compare", str(project), str(twin), "--epoch", "B"])
    assert result.exit_code == 0, result.output


def test_compare_reports_missing_counterparts(project: Path, fake_difmap: Path, tmp_path: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    empty = tmp_path / "empty"
    empty.mkdir()
    diffs = compare_projects(project, empty)
    assert diffs and all(d.status == "missing" for d in diffs)


def test_doctor_path_check(monkeypatch, tmp_path: Path) -> None:
    import shutil as _shutil

    from lenspipe.doctor import _path_check

    # Not an installed command (uv run / pytest): the check is informational.
    monkeypatch.setattr("sys.argv", ["/somewhere/else/pytest"])
    assert _path_check().status == "ok"
    # Installed under ~/.local/bin but that directory is not on PATH: warn with the fix.
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "lenspipe"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.argv", [str(exe)])
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    check = _path_check()
    assert check.status == "warn" and str(bin_dir) in check.detail and "export PATH" in (check.fix or "")
    monkeypatch.setattr(_shutil, "which", lambda name: str(exe))
    assert _path_check().status == "ok"


def test_install_script_appends_path_line_to_a_profile_without_it(tmp_path: Path) -> None:
    """Simulate the Linux report: lenspipe lands in ~/.local/bin, the login shell has no PATH entry."""
    import os
    import subprocess

    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bashrc = home / ".bashrc"
    bashrc.write_text("# user rc\n")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    # uv: pretend the tool install succeeded by dropping an executable into ~/.local/bin
    (fake_bin / "uv").write_text(
        "#!/bin/sh\nprintf '#!/bin/sh\\necho lenspipe 9.9.9\\n' > \"$HOME/.local/bin/lenspipe\"; chmod +x \"$HOME/.local/bin/lenspipe\"\n"
    )
    (fake_bin / "uv").chmod(0o755)
    # a "login shell" whose PATH lacks ~/.local/bin, whatever the rc file says
    (fake_bin / "fakesh").write_text("#!/bin/sh\ncase \"$1\" in -lic) printf %s \"/usr/bin:/bin\";; *) exec /bin/sh \"$@\";; esac\n")
    (fake_bin / "fakesh").chmod(0o755)
    env = {**os.environ, "HOME": str(home), "SHELL": str(fake_bin / "fakesh"), "PATH": f"{fake_bin}:/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "install.sh"), "--from", str(REPO_ROOT)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'export PATH="$HOME/.local/bin:$PATH"' in bashrc.read_text()
    assert "IMPORTANT: Added ~/.local/bin to your PATH" in result.stdout
    assert "Open a NEW terminal" in result.stdout
    # Second run: line already there, still told to open a new terminal, not appended twice.
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "install.sh"), "--from", str(REPO_ROOT)],
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0 and bashrc.read_text().count(".local/bin") == 1
    assert "is set in" in result.stdout and "Open a NEW terminal" in result.stdout
