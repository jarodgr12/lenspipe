"""DifMAP memory is measured per run and 'auto' sizing learns from it."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from lenspipe import memory_calibration as mc
from lenspipe.config import LenspipeConfig
from lenspipe.difmap.runner import run_difmap
from lenspipe.progress import Reporter
from lenspipe.project import decide_shards
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2

GIB = 1 << 30


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "lenspipe-home"
    monkeypatch.setenv("LENSPIPE_HOME", str(home))
    return home


def test_runner_reports_child_peak_memory(fake_difmap: Path, tmp_path: Path) -> None:
    result = run_difmap(str(fake_difmap), "quit\n", tmp_path / "peak.log")
    assert result.peak_rss_bytes is not None and result.peak_rss_bytes > 1_000_000  # a python child


def test_measurements_are_stored_only_for_meaningful_inputs(isolated_home: Path) -> None:
    assert mc.record_measurement(500 * 2**20, 10 * 2**20) is None  # 10 MiB input: ignored
    assert mc.load_calibration() is None
    first = mc.record_measurement(int(4.2 * GIB), 3 * GIB)
    assert first is not None and first.multiple == pytest.approx(1.4, abs=1e-3) and first.samples == 1
    # A smaller ratio later does not lower the stored one; a larger one raises it.
    second = mc.record_measurement(int(3.0 * GIB), 3 * GIB)
    assert second.multiple == pytest.approx(1.4, abs=1e-3) and second.samples == 2
    third = mc.record_measurement(int(6.0 * GIB), 3 * GIB)
    assert third.multiple == pytest.approx(2.0, abs=1e-3) and third.samples == 3
    stored = json.loads((isolated_home / "difmap_memory.json").read_text())
    assert stored["input_bytes"] == 3 * GIB and stored["host"]


def test_effective_multiple_sources() -> None:
    assert mc.effective_multiple(2.5) == (2.5, "configured")
    value, source = mc.effective_multiple("auto")
    assert value == mc.DEFAULT_MULTIPLE and "no measurement" in source
    mc.record_measurement(int(1.2 * GIB), 1 * GIB)
    value, source = mc.effective_multiple("auto")
    assert value == pytest.approx(1.2 * mc.SAFETY_FACTOR, abs=1e-3) and source.startswith("measured")


def test_auto_sizing_on_the_zenbook_before_and_after_measurement() -> None:
    """13th-gen i9 laptop: 20 logical CPUs, 32 GiB, 3 GiB visits, one epoch at a time."""
    kwargs = dict(fit_count=3072, input_bytes=3 * GIB, epoch_workers=1, memory_fraction=0.5,
                  total_memory_bytes=32 * GIB, cpu_count=20)
    before = decide_shards("auto", memory_multiple="auto", **kwargs)
    assert before.shards == 1  # the conservative default (x3) allows one 9 GiB process in 16 GiB
    mc.record_measurement(int(4.0 * GIB), 3 * GIB)  # a real run: DifMAP peaked at 4 GiB
    after = decide_shards("auto", memory_multiple="auto", **kwargs)
    assert after.memory_multiple == pytest.approx(4 / 3 * mc.SAFETY_FACTOR, abs=1e-3)
    assert after.shards == 3  # 16 GiB / 5 GiB per process
    assert "measured" in (after.memory_multiple_source or "")
    pinned = decide_shards("auto", memory_multiple=1.0, **kwargs)
    assert pinned.shards == 5 and pinned.memory_multiple_source == "configured"


def test_config_accepts_auto_or_positive_number() -> None:
    assert LenspipeConfig().stage2.memory_multiple == "auto"
    assert LenspipeConfig.model_validate({"stage2": {"memory_multiple": 1.5}}).stage2.memory_multiple == 1.5
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage2": {"memory_multiple": 0}})
    with pytest.raises(ValueError):
        LenspipeConfig.model_validate({"stage2": {"memory_multiple": "sometimes"}})


def test_stage2_records_peak_memory_in_metadata(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    run_stage1(project, cfg, reporter=quiet, workers=1)
    results = run_stage2(project, cfg.with_overrides(stage2={"mode": "channel", "shards": 2}), reporter=quiet, workers=1)
    assert all(r.ok for r in results)
    meta = json.loads(results[0].paths.metadata_file.read_text())
    execution = meta["execution"]
    assert execution["difmap_peak_rss_bytes"] and execution["difmap_peak_rss_bytes"] > 0
    assert execution["input_bytes"] > 0 and execution["measured_memory_multiple"] > 0
    assert execution["shard_decision"]["memory_multiple_source"]
    assert mc.load_calibration() is None  # test inputs are far too small to calibrate from
