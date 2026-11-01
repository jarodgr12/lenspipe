"""2.4.2: every console job records the parameters it was queued with and the ones it used."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from lenspipe.jobs import JobManager, config_for_argv
from lenspipe.runs import create_run


def test_config_for_argv_resolves_project_run_and_explicit_config(project: Path) -> None:
    assert config_for_argv(project, ["stage1", str(project)]) == project / "lenspipe.toml"
    assert config_for_argv(project, ["stage2", str(project), "--run", "A"]) == project / "runs" / "A" / "lenspipe.toml"
    assert config_for_argv(project, ["stage3", str(project), "--config", "/x/other.toml"]) == Path("/x/other.toml")
    assert config_for_argv(project, ["stage1"]) == project / "lenspipe.toml"  # no path: the manager's project
    assert config_for_argv(project, ["--version"]) is None and config_for_argv(project, ["update"]) is None


def test_job_records_queued_config_and_the_effective_parameters_it_used(project: Path, fake_difmap: Path) -> None:
    (project / "lenspipe.toml").write_text(f'[project.difmap]\nexecutable = "{fake_difmap}"\n[stage2]\nunflag = true\n')
    manager = JobManager(project)
    job = manager.submit(
        ["stage2", str(project), "--mode", "channel", "--no-unflag", "--dry-run", "--epoch", "A"], title="t", stage="stage2"
    )
    # Queued with: a verbatim copy of the project's file at submit time, named in the record.
    assert job.queued_config_path.read_text() == (project / "lenspipe.toml").read_text()
    assert job.extra["config_source"] == str(project / "lenspipe.toml")
    assert job.parameters()[0].startswith("Config the job was queued with")
    (project / "lenspipe.toml").write_text("[stage2]\nunflag = false\n")  # later edits do not touch the record
    assert "unflag = true" in job.queued_config_path.read_text()

    # Used: the effective parameters written by the stage itself, with the command-line override applied.
    manager.wait(job.id, timeout_s=120)
    used = tomllib.loads(job.effective_config_path.read_text())
    assert used["stage2"]["unflag"] is False and used["stage2"]["mode"] == "channel"
    label, text = manager.get(job.id).parameters()
    assert label.startswith("Parameters this job used") and "unflag = false" in text

    # A run's job is queued against the run's frozen file.
    create_run(project, "A")
    run_job = manager.submit(["stage1", str(project), "--run", "A", "--dry-run"], title="t", stage="stage1")
    assert run_job.extra["config_source"] == str(project / "runs" / "A" / "lenspipe.toml")
    manager.wait(run_job.id, timeout_s=120)
    assert run_job.effective_config_path.is_file()

    # A project without a config file still gets a record: the defaults, with a note saying so.
    (project / "lenspipe.toml").unlink()
    bare = manager.submit(["stage1", str(project), "--dry-run"], title="t", stage="stage1")
    assert bare.queued_config_path.read_text().startswith("# No lenspipe.toml at ")
    manager.wait(bare.id, timeout_s=120)


@pytest.mark.parametrize("argv", [["--version"], ["update", "--check"]])
def test_non_stage_jobs_have_no_parameter_record(project: Path, argv: list[str]) -> None:
    manager = JobManager(project)
    job = manager.submit(argv, title="t", stage="other")
    assert job.parameters() is None and "config_source" not in job.extra
    manager.wait(job.id, timeout_s=120)


def test_a_waiting_job_runs_with_the_config_it_was_queued_with(project: Path, fake_difmap: Path, monkeypatch) -> None:
    """Queue a job, edit lenspipe.toml while it waits, let it start: it must use the old values."""
    from lenspipe.jobs import JobManager, JobRecord

    (project / "lenspipe.toml").write_text(f'[project.difmap]\nexecutable = "{fake_difmap}"\n[stage2]\nunflag = true\n')
    manager = JobManager(project)
    blocker = JobRecord(
        id="20260101T000000-stage1", title="busy", stage="stage1", argv=["stage1"], project_root=str(project),
        created_utc="2026-01-01T00:00:00+00:00", status="running", pid=424242, started_utc="2026-01-01T00:00:01+00:00",
    )
    blocker.save()
    monkeypatch.setattr("lenspipe.jobs._is_lenspipe_job", lambda pid: True)
    waiting = manager.submit(["stage1", str(project), "--dry-run", "--epoch", "A"], title="w", stage="stage1")
    assert manager.get(waiting.id).status == "queued"
    assert manager.launch_argv(waiting)[-2:] == ["--config", str(waiting.queued_config_path)]
    (project / "lenspipe.toml").write_text(f'[project.difmap]\nexecutable = "{fake_difmap}"\n[stage2]\nunflag = false\n')
    blocker.status = "completed"
    blocker.save()
    manager._finished.pop(blocker.id, None)
    manager.wait(waiting.id, timeout_s=120)
    assert manager.get(waiting.id).status == "completed"
    assert "unflag = true" in waiting.effective_config_path.read_text()  # not the edited file
