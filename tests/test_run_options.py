"""2.0.10: Stage 2 unflag, per-run advanced options on the Run page, and resume from the Jobs page."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig, Stage2Config
from lenspipe.difmap.scripts import stage2_commands
from lenspipe.jobs import JobRecord
from lenspipe.stage2_shards import plan_fingerprint
from lenspipe.ui.commands import RunRequest, build_steps, can_resume, resume_step

runner = CliRunner()


# ---------------------------------------------------------------------------
# Stage 2 unflag


def _commands(config: Stage2Config) -> str:
    return stage2_commands(
        calibrated_uvfits=Path("/p/stage1/X.A/X.A.cal.uvf"),
        stage2_model=Path("/p/stage2/X.A/X.A.channel.mod"),
        fit_ranges=[(1, 1, 1), (2, 2, 2)],
        work_directory=Path("/p/work"),
        mode="channel",
        config=config,
    )


def test_stage2_unflag_is_off_by_default_and_inserts_one_command_when_on() -> None:
    assert Stage2Config().unflag is False
    assert "unflag" not in _commands(Stage2Config(mode="channel"))
    text = _commands(Stage2Config(mode="channel", unflag=True))
    assert text.startswith("observe /p/stage1/X.A/X.A.cal.uvf\nselect i\nunflag *\n")
    assert text.count("unflag *") == 1  # once after observe, not once per fit


def test_stage2_unflag_changes_the_resume_fingerprint(project: Path, tmp_path: Path) -> None:
    data = tmp_path / "data.uvf"
    data.write_bytes(b"x")
    model = tmp_path / "m.mod"
    model.write_text("! model\n")
    ranges = [(1, 1, 1)]
    on = plan_fingerprint(data, model, Stage2Config(mode="channel", unflag=True), ranges)
    off = plan_fingerprint(data, model, Stage2Config(mode="channel", unflag=False), ranges)
    assert on != off


def test_stage2_cli_unflag_flag(project: Path, fake_difmap: Path) -> None:
    stage1 = runner.invoke(app, ["stage1", str(project), "--difmap", str(fake_difmap), "--epoch", "A", "--workers", "1"])
    assert stage1.exit_code == 0, stage1.output
    common = ["stage2", str(project), "--difmap", str(fake_difmap), "--mode", "channel", "--dry-run", "--epoch", "A"]
    assert "unflag *" not in runner.invoke(app, common).output
    with_flag = runner.invoke(app, [*common, "--unflag"])
    assert with_flag.exit_code == 0, with_flag.output
    assert "unflag *" in with_flag.output
    (project / "lenspipe.toml").write_text("[stage2]\nunflag = true\n")
    assert "unflag *" in runner.invoke(app, common).output
    assert "unflag *" not in runner.invoke(app, [*common, "--no-unflag"]).output


def test_stage2_unflag_in_config_roundtrip() -> None:
    cfg = LenspipeConfig().with_overrides(stage2={"unflag": True})
    assert cfg.stage2.unflag is True
    assert LenspipeConfig.model_validate(cfg.dump()).stage2.unflag is True


# ---------------------------------------------------------------------------
# Run page: advanced per-run options become the matching CLI flags


def test_build_steps_passes_advanced_options_through(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[1, 2, 3],
        stage1_final_if_selfcal=True,
        stage2_mode="if", stage2_channels_per_if=64, stage2_exclude_edge_channels=2,
        stage2_modelfit_iterations=5, stage2_keep_models=True, stage2_plots=False,
        stage2_error_bars=False, stage2_unflag=True,
        stage3_product="if64_edge2", stage3_fit_method="emcee", stage3_reference_frequency=14.5,
        stage3_exclude_channels="1-4,61-64", stage3_exclude_epoch_channels=["E:897-960", "F:1-4"],
        stage3_annotations=False, stage3_error_bars=False, stage3_formats="png",
    )
    steps, _ = build_steps(request)
    s1, s2, s3 = (step.argv for step in steps)
    assert s1 == ["stage1", str(tmp_path), "--final-if-selfcal"]
    assert s2 == [
        "stage2", str(tmp_path), "--mode", "if", "--channels-per-if", "64",
        "--exclude-edge-channels", "2", "--modelfit-iterations", "5", "--unflag",
        "--keep-models", "--no-plots", "--no-error-bars",
    ]
    assert s3 == [
        "stage3", str(tmp_path), "--product", "if64_edge2", "--fit-method", "emcee",
        "--reference-frequency", "14.5", "--exclude-channels", "1-4,61-64",
        "--exclude-epoch-channels", "E:897-960", "--exclude-epoch-channels", "F:1-4",
        "--no-annotations", "--no-error-bars", "--formats", "png",
    ]


def test_build_steps_emits_negative_forms_and_nothing_when_unset(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[1, 2, 3], stage1_final_if_selfcal=False, stage2_mode="channel",
        stage2_keep_models=False, stage2_plots=True, stage2_error_bars=True, stage2_unflag=False,
        stage3_annotations=True, stage3_error_bars=True,
    )
    steps, _ = build_steps(request)
    s1, s2, s3 = (step.argv for step in steps)
    assert s1[-1] == "--no-final-if-selfcal"
    assert s2 == ["stage2", str(tmp_path), "--mode", "channel", "--no-unflag", "--no-keep-models", "--plots", "--error-bars"]
    assert s3 == ["stage3", str(tmp_path), "--annotations", "--error-bars"]
    plain, _ = build_steps(RunRequest(root=tmp_path, stages=[1, 2, 3]))
    assert [step.argv for step in plain] == [
        ["stage1", str(tmp_path)], ["stage2", str(tmp_path)], ["stage3", str(tmp_path)],
    ]


def test_build_steps_edge_channels_only_in_if_mode(tmp_path: Path) -> None:
    request = RunRequest(root=tmp_path, stages=[2], stage2_mode="channel", stage2_exclude_edge_channels=2)
    steps, _ = build_steps(request)
    assert "--exclude-edge-channels" not in steps[0].argv


# ---------------------------------------------------------------------------
# Jobs page: resume an interrupted Stage 2


def _record(stage: str, argv: list[str], status: str) -> JobRecord:
    return JobRecord(id="j1", title=f"{stage} A", stage=stage, argv=argv, project_root="/p",
                     created_utc="2026-09-21T00:00:00Z", status=status)


@pytest.mark.parametrize(
    "stage, argv, status, expected",
    [
        ("stage2", ["stage2", "/p", "--mode", "channel"], "failed", True),
        ("stage2", ["stage2", "/p"], "cancelled", True),
        ("stage2", ["stage2", "/p"], "completed", False),
        ("stage2", ["stage2", "/p"], "running", False),
        ("stage2", ["stage2", "/p", "--dry-run"], "failed", False),
        ("stage2", ["stage2", "/p", "--recover-from-log"], "failed", False),
        ("stage1", ["stage1", "/p"], "failed", False),
        ("stage3", ["stage3", "/p"], "failed", False),
    ],
)
def test_can_resume(stage: str, argv: list[str], status: str, expected: bool) -> None:
    assert can_resume(_record(stage, argv, status)) is expected


def test_resume_step_keeps_the_settings_and_drops_overwrite() -> None:
    record = _record(
        "stage2", ["stage2", "/p", "--epoch", "A", "--mode", "channel", "--shards", "4", "--overwrite", "--workers", "2"],
        "failed",
    )
    step = resume_step(record)
    assert step.stage == "stage2"
    assert step.argv == ["stage2", "/p", "--epoch", "A", "--mode", "channel", "--shards", "4", "--workers", "2", "--resume"]
    assert step.title == "stage2 A (resume)"
    # Resuming a resumed job does not stack flags.
    assert resume_step(_record("stage2", step.argv, "failed")).argv == step.argv
