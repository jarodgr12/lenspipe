"""2.4.0: runs are named parameter sets with frozen configs and their own output trees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.project import inventory
from lenspipe.runs import create_run, get_run, list_runs, refreeze_run, run_root, validate_label
from lenspipe.ui.commands import RunRequest, build_steps

runner = CliRunner()


def test_create_run_freezes_the_config_and_shares_the_inputs(project: Path) -> None:
    (project / "lenspipe.toml").write_text("[stage2]\nunflag = true\n")
    run, created = create_run(project, "sched-A")
    assert created and run.root == project / "runs" / "sched-A" and run.label == "sched-A"
    assert (run.root / "lenspipe.toml").read_text() == "[stage2]\nunflag = true\n"
    assert (run.root / "inputs").is_symlink() and (run.root / "inputs" / "MG0414.A.uvfits").is_file()
    marker = json.loads((run.root / "run.json").read_text())
    assert marker["label"] == "sched-A" and marker["parent"] == str(project) and marker["created_utc"]
    assert [r.label for r in list_runs(project)] == ["sched-A"]
    assert run.config_differs is False and run.config_diff() == ""

    # Editing the project's config afterwards leaves the run's frozen copy alone and shows as a diff.
    (project / "lenspipe.toml").write_text("[stage2]\nunflag = false\n")
    again, created = create_run(project, "sched-A")
    assert not created and (again.root / "lenspipe.toml").read_text() == "[stage2]\nunflag = true\n"
    assert again.config_differs and "-unflag = true" in again.config_diff() and "+unflag = false" in again.config_diff()
    refreeze_run(project, "sched-A")
    assert get_run(project, "sched-A").config_differs is False
    assert get_run(project, "nope") is None

    # Labels name directories; runs cannot nest; a project without inputs has nothing to share.
    for bad in ("", " ", "../x", "a b", "-lead", "x" * 65):
        with pytest.raises(ValueError, match="run label"):
            validate_label(bad)
    assert validate_label(" ok_1.2-3 ") == "ok_1.2-3"
    with pytest.raises(ValueError, match="nested"):
        create_run(run.root, "inner")
    empty = project.parent / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="inputs"):
        create_run(empty, "x")
    assert run_root(project, "z") == project / "runs" / "z"


def test_stage_commands_with_run_work_in_the_run_tree_with_its_frozen_config(project: Path, fake_difmap: Path) -> None:
    (project / "lenspipe.toml").write_text(f'[project.difmap]\nexecutable = "{fake_difmap}"\n')
    result = runner.invoke(app, ["stage1", str(project), "--run", "sched-A", "--epoch", "A", "--workers", "1"])
    assert result.exit_code == 0, result.output
    assert "Created run sched-A" in result.output
    run_dir = project / "runs" / "sched-A"
    assert (run_dir / "stage1" / "full-band" / "MG0414.A" / "MG0414.A.stage1.metadata.json").is_file()
    assert not (project / "stage1").exists()  # the project's own tree is untouched

    # The project's config changes after the run was created: jobs for the run keep the frozen one.
    (project / "lenspipe.toml").write_text(f'[project.difmap]\nexecutable = "{fake_difmap}"\n[stage2]\nunflag = true\n')
    common = ["stage2", str(project), "--mode", "channel", "--shards", "1", "--epoch", "A"]
    frozen = runner.invoke(app, [*common, "--run", "sched-A", "--dry-run"])
    assert frozen.exit_code == 0, frozen.output
    assert "Using run sched-A" in frozen.output and "unflag *" not in frozen.output
    fresh = runner.invoke(app, ["stage1", str(project), "--run", "sched-B", "--epoch", "A", "--dry-run"])
    assert fresh.exit_code == 0, fresh.output
    assert "unflag = true" in (project / "runs" / "sched-B" / "lenspipe.toml").read_text()  # frozen as it is now

    result = runner.invoke(app, [*common, "--run", "sched-A"])
    assert result.exit_code == 0, result.output
    assert (run_dir / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.stage2.metadata.json").is_file()

    summary = inventory(project)
    assert [(r["label"], r["stage1"], r["stage2"], r["config_differs"]) for r in summary["runs"]] == [
        ("sched-A", 1, 1, True), ("sched-B", 0, 0, False),
    ]
    assert summary["is_run"] is False and inventory(run_dir)["is_run"] is True
    assert [r["epoch"] for r in inventory(run_dir)["epochs"]] == ["A", "B"]  # inputs seen through the link

    listing = runner.invoke(app, ["runs", str(project)])
    assert listing.exit_code == 0 and "sched-A" in listing.output and "differ from the project's" in listing.output
    diff = runner.invoke(app, ["runs", str(project), "--diff", "sched-A"])
    assert "+unflag = true" in diff.output
    assert "run  sched-A" in runner.invoke(app, ["inventory", str(project)]).output
    assert runner.invoke(app, ["runs", str(project), "--create", "bad label"]).exit_code == 2
    created = runner.invoke(app, ["runs", str(project), "--create", "sched-C"])
    assert created.exit_code == 0 and (project / "runs" / "sched-C" / "run.json").is_file()


def test_build_steps_adds_the_run_flag_to_every_stage(tmp_path: Path) -> None:
    steps, _ = build_steps(RunRequest(root=tmp_path, stages=[1, 2, 3], run="sched-A", epochs=["A"], stage2_mode="if"))
    assert [step.argv[:4] for step in steps] == [
        ["stage1", str(tmp_path), "--run", "sched-A"],
        ["stage2", str(tmp_path), "--run", "sched-A"],
        ["stage3", str(tmp_path), "--run", "sched-A"],
    ]
    assert [step.title for step in steps] == ["Stage 1 [sched-A] A", "Stage 2 [sched-A] A", "Stage 3 [sched-A] A"]
    plain, _ = build_steps(RunRequest(root=tmp_path, stages=[1]))
    assert plain[0].argv == ["stage1", str(tmp_path)] and plain[0].title == "Stage 1"


def test_console_resolves_run_roots(project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lenspipe.ui.state import Console

    monkeypatch.setattr("lenspipe.ui.state.RECENT_PATH", tmp_path / "recent.json")
    console = Console()
    console.open_root(project)
    assert console.runs() == [] and console.run_root("x") is None and console.run_root("") is None
    create_run(project, "x")
    assert console.run_root("x") == project / "runs" / "x" and console.run_root("bad label") is None
