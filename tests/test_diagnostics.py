"""`lenspipe diagnostics`: one zip that names what went wrong, and a health log that outlives a crash."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.diagnostics import (
    HEALTH_FILENAME,
    _clip,
    collect,
    start_health_log,
    uvfits_size_check,
)

runner = CliRunner()


def _summary(target: Path) -> tuple[str, list[str]]:
    with zipfile.ZipFile(target) as archive:
        return archive.read("SUMMARY.txt").decode(), archive.namelist()


def _truncate(path: Path) -> None:
    """Cut a UV-FITS file inside its visibilities, header intact: what a hard crash leaves."""
    keep = uvfits_size_check(path)["expected_at_least_bytes"] - 500
    path.write_bytes(path.read_bytes()[:keep])


def test_intact_uvfits_passes_and_a_truncated_one_is_named(project: Path) -> None:
    good = project / "inputs" / "MG0414.A.uvfits"
    assert uvfits_size_check(good)["verdict"] == "ok"

    cut = project / "inputs" / "MG0414.B.uvfits"
    _truncate(cut)
    report = uvfits_size_check(cut)
    assert report["verdict"].startswith("TRUNCATED") and report["expected_at_least_bytes"] > report["size_bytes"]


def test_bundle_flags_the_broken_things_and_carries_the_evidence(project: Path) -> None:
    (project / "lenspipe.toml").write_text("[stage2]\nshards = 2\n")
    inputs = project / "inputs"
    _truncate(inputs / "MG0414.B.uvfits")
    (inputs / "MG0414.C.uvfits").symlink_to(project / "gone.uvfits")

    epoch = project / "stage1" / "full-band" / "MG0414.B"
    epoch.mkdir(parents=True)
    (epoch / "MG0414.B.stage1.difmap.log").write_text(
        "0>observe /data/MG0414.B.uvfits\nError reading UV FITS file: premature end of file\n0>select i\n"
    )
    job = project / ".jobs" / "20261124-000000-stage1"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": job.name, "status": "running", "pid": 2**22 + 12345}))
    (job / "log.txt").write_text("[stage1 MG0414.B] step 1: 0 s\n")

    summary, names = _summary(collect(project))

    assert "MG0414.B.uvfits: TRUNCATED" in summary
    assert "MG0414.C.uvfits: MISSING (broken link)" in summary
    assert "premature end of file" in summary  # the DifMAP error is quoted, not just the file attached
    assert "its process is gone" in summary
    for expected in (
        "doctor.json",
        "project/lenspipe.toml",
        "project/tree.txt",
        "project/uvfits-check.json",
        "logs/stage1/full-band/MG0414.B/MG0414.B.stage1.difmap.log",
        f"jobs/{job.name}/job.json",
        f"jobs/{job.name}/log.txt",
        "system/uname.txt",
        "system/health-now.json",
    ):
        assert expected in names, expected


def test_a_healthy_project_flags_no_data_or_log_problems(project: Path) -> None:
    summary, _ = _summary(collect(project))
    flagged = summary.split("file(s) in this archive")[0]
    assert "TRUNCATED" not in flagged and "MISSING" not in flagged and "first error lines" not in flagged


def test_long_logs_keep_their_start_and_end(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    log.write_text("FIRST LINE\n" + "x" * 5000 + "\nLAST LINE\n")
    clipped = _clip(log, head=100, tail=100).decode()
    assert clipped.startswith("FIRST LINE") and clipped.endswith("LAST LINE\n")
    assert "left out" in clipped and len(clipped) < 600


def test_health_log_writes_complete_lines_while_running(tmp_path: Path) -> None:
    stop = start_health_log(tmp_path / "job", interval=0.02)
    path = tmp_path / "job" / HEALTH_FILENAME
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (not path.is_file() or path.read_text().count("\n") < 3):
        time.sleep(0.02)
    stop.set()
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(samples) >= 3
    assert all("utc" in sample and "disk_free_gib" in sample for sample in samples)


def test_cli_writes_the_zip_and_says_which_file_to_send(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    result = runner.invoke(app, ["diagnostics", str(project), "--out", str(out)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    produced = list(out.glob("lenspipe-diagnostics-*.zip"))
    assert len(produced) == 1 and str(produced[0]) in result.output.replace("\n", "")

    monkeypatch.chdir(tmp_path)  # not a project: the machine part is still collected
    result = runner.invoke(app, ["diagnostics", "--out", str(out)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert len(list(out.glob("lenspipe-diagnostics-*.zip"))) >= 1


def test_stage_jobs_record_a_health_log(project: Path, fake_difmap: Path) -> None:
    from lenspipe.jobs import JobManager

    runner.invoke(app, ["init", str(project)], catch_exceptions=False)
    manager = JobManager(project)
    record = manager.submit(
        ["stage1", str(project), "--dry-run"], title="stage1", stage="stage1"
    )
    final = manager.wait(record.id, timeout_s=60)
    assert final.status == "completed", final.tail()
    lines = (final.directory / HEALTH_FILENAME).read_text().splitlines()
    assert lines and "utc" in json.loads(lines[0])
