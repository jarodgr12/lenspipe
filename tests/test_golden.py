"""Golden tests: the package reproduces the legacy scripts' products end to end.

Both pipelines drive the same fake DifMAP, so any difference in the CSV, model
or metadata products is a behaviour change in the Python, not in DifMAP.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from tests.conftest import LEGACY_DIR, LEGACY_STAGE1, LEGACY_STAGE2, LEGACY_STAGE3

VOLATILE_KEYS = {"created_utc", "stage1_version", "stage2_version", "stage3_version", "version"}


def _run(argv: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [sys.executable, *argv], env=env, cwd=str(cwd) if cwd else None,
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, f"{argv}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    return completed


def _config(fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig()
    cfg = cfg.with_overrides(project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}})
    if stage2:
        cfg = cfg.with_overrides(stage2=stage2)
    return cfg


def _strip(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in VOLATILE_KEYS}


def _copy_project(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


@pytest.fixture
def quiet() -> Reporter:
    import io

    return Reporter(stream=io.StringIO())


def test_stage1_products_match_legacy(project: Path, fake_difmap: Path, fake_difmap_env, tmp_path: Path, quiet) -> None:
    legacy_root = _copy_project(project, tmp_path / "legacy")
    new_root = _copy_project(project, tmp_path / "new")

    _run([str(LEGACY_STAGE1), str(legacy_root), "--difmap", str(fake_difmap)], fake_difmap_env)
    results = run_stage1(new_root, _config(fake_difmap), reporter=quiet, workers=2)
    assert all(r.ok for r in results), [r.message for r in results]

    for epoch in ("A", "B"):
        prefix = f"MG0414.{epoch}"
        legacy_dir = legacy_root / "stage1" / prefix
        new_dir = new_root / "stage1" / prefix
        assert (legacy_dir / f"{prefix}.gmod").read_text() == (new_dir / f"{prefix}.gmod").read_text()
        assert (legacy_dir / f"{prefix}.cal.uvf").read_bytes() == (new_dir / f"{prefix}.cal.uvf").read_bytes()
        legacy_model = json.loads((legacy_dir / f"{prefix}.model.json").read_text())
        new_model = json.loads((new_dir / f"{prefix}.model.json").read_text())
        assert _strip(legacy_model) == _strip(new_model)
        legacy_meta = json.loads((legacy_dir / f"{prefix}.stage1.metadata.json").read_text())
        new_meta = json.loads((new_dir / f"{prefix}.stage1.metadata.json").read_text())
        assert new_meta["final_residual_rms_jy_per_beam"] == legacy_meta["final_residual_rms_jy_per_beam"]
        assert new_meta["final_if_selfcal"] == legacy_meta["final_if_selfcal"]
        assert new_meta["difmap"]["version"] == "2.5k"
        assert new_meta["config"]["selfcal"][3]["amplitude"] is True


@pytest.mark.parametrize(
    "legacy_args, stage2_overrides",
    [
        (["--mode", "channel"], {"mode": "channel"}),
        (["--mode", "channel", "--channels", "1-3,6"], {"mode": "channel", "channels": "1-3,6"}),
        (["--mode", "if", "--channels-per-if", "4"], {"mode": "if", "channels_per_if": 4}),
        (
            ["--mode", "if", "--channels-per-if", "4", "--exclude-edge-channels", "1"],
            {"mode": "if", "channels_per_if": 4, "exclude_edge_channels": 1},
        ),
    ],
)
def test_stage2_products_match_legacy_and_are_shard_invariant(
    project: Path, fake_difmap: Path, fake_difmap_env, tmp_path: Path, quiet,
    legacy_args: list[str], stage2_overrides: dict,
) -> None:
    # Stage 1 once (new code, already shown equivalent), then fan out.
    base = project
    run_stage1(base, _config(fake_difmap), reporter=quiet, workers=1)
    legacy_root = _copy_project(base, tmp_path / "legacy")
    single_root = _copy_project(base, tmp_path / "single")
    sharded_root = _copy_project(base, tmp_path / "sharded")

    _run([str(LEGACY_STAGE2), str(legacy_root), "--difmap", str(fake_difmap), *legacy_args], fake_difmap_env)
    single = run_stage2(single_root, _config(fake_difmap, shards=1, **stage2_overrides), reporter=quiet)
    sharded = run_stage2(sharded_root, _config(fake_difmap, shards=3, **stage2_overrides), reporter=quiet, workers=2)
    assert all(r.ok for r in single), [r.message for r in single]
    assert all(r.ok for r in sharded), [r.message for r in sharded]

    product = single[0].paths.product_tag
    for epoch in ("A", "B"):
        prefix = f"MG0414.{epoch}"
        name = f"{prefix}.{product}"
        legacy_csv = (legacy_root / "stage2" / prefix / f"{name}.spectrum.csv").read_text()
        single_csv = (single_root / "stage2" / prefix / f"{name}.spectrum.csv").read_text()
        sharded_csv = (sharded_root / "stage2" / prefix / f"{name}.spectrum.csv").read_text()
        assert single_csv == legacy_csv
        assert sharded_csv == legacy_csv

        legacy_model = (legacy_root / "stage2" / prefix / f"{prefix}.stage2.mod").read_text()
        assert (single_root / "stage2" / prefix / f"{prefix}.stage2.mod").read_text() == legacy_model

        legacy_meta = json.loads((legacy_root / "stage2" / prefix / f"{name}.stage2.metadata.json").read_text())
        new_meta = json.loads((sharded_root / "stage2" / prefix / f"{name}.stage2.metadata.json").read_text())
        for key in ("mode", "channels_per_if", "excluded_edge_channels_per_side", "fitted_channels_per_if",
                    "total_channels", "n_fits", "groups", "components", "mjd", "n_groups", "n_components"):
            assert new_meta[key] == legacy_meta[key], key
        assert new_meta["execution"]["shards"] == min(3, new_meta["n_fits"])

        legacy_manifest = json.loads((legacy_root / "stage2" / prefix / f"{name}.stage2.manifest.json").read_text())
        new_manifest = json.loads((sharded_root / "stage2" / prefix / f"{name}.stage2.manifest.json").read_text())
        assert new_manifest == legacy_manifest

    # The concatenated shard log recovers to the same CSV without running DifMAP.
    recovered = run_stage2(
        sharded_root, _config(fake_difmap, shards=3, **stage2_overrides),
        reporter=quiet, recover_from_log=True, overwrite=True,
    )
    assert all(r.ok and r.status == "recovered" for r in recovered)
    for epoch in ("A", "B"):
        prefix = f"MG0414.{epoch}"
        name = f"{prefix}.{product}"
        assert (sharded_root / "stage2" / prefix / f"{name}.spectrum.csv").read_text() == (
            legacy_root / "stage2" / prefix / f"{name}.spectrum.csv"
        ).read_text()


def _csv_files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*.csv"))}


def test_stage3_products_match_legacy(project: Path, fake_difmap: Path, fake_difmap_env, tmp_path: Path, quiet) -> None:
    from lenspipe.stage3 import run_stage3

    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap, mode="channel", shards=2), reporter=quiet, workers=1)
    legacy_root = _copy_project(project, tmp_path / "legacy")
    new_root = _copy_project(project, tmp_path / "new")

    _run([str(LEGACY_STAGE3), str(legacy_root), "--product", "channel"], fake_difmap_env, cwd=LEGACY_DIR)
    summary = run_stage3(new_root, _config(fake_difmap), products={"channel"}, reporter=quiet, workers=2)
    assert summary.ok, summary

    legacy_csv = _csv_files(legacy_root / "stage3")
    new_csv = _csv_files(new_root / "stage3")
    assert set(legacy_csv) == set(new_csv)
    assert len(new_csv) >= 12
    for name, legacy_path in legacy_csv.items():
        left = pd.read_csv(legacy_path)
        right = pd.read_csv(new_csv[name])
        assert_frame_equal(left, right, check_exact=False, rtol=1e-10, atol=1e-14, obj=name)

    # Same figure set apart from the RMS diagnostics: the legacy script saved those with
    # Path.with_suffix(), which drops the ".rms_..." name segment, so its per-visit RMS figure
    # was written as "<prefix>.png" and the four combined RMS figures overwrote one another as
    # "<stem>.png". The package writes them under their intended names.
    def figure_names(root: Path) -> set[str]:
        return {
            str(p.relative_to(root)) for p in (root / "stage3").rglob("*.png")
            if "rms_" not in p.name and p.name not in {
                "MG0414.A.channel.png", "MG0414.B.channel.png", "MG0414.channel.png",
            }
        }

    assert figure_names(legacy_root) == figure_names(new_root)
    new_pngs = sorted(str(p.relative_to(new_root)) for p in (new_root / "stage3").rglob("*.png"))
    assert any("rcusp_vs_mjd" in p for p in new_pngs)
    rms_figures = sorted(Path(p).name for p in new_pngs if "rms_" in Path(p).name)
    assert rms_figures == [
        "MG0414.A.channel.rms_vs_channel.png",
        "MG0414.B.channel.rms_vs_channel.png",
        "MG0414.channel.median_rms_vs_channel.png",
        "MG0414.channel.relative_rms_vs_channel_all_epochs.png",
        "MG0414.channel.rms_visit_robust_score_vs_channel.png",
        "MG0414.channel.rms_vs_channel_all_epochs.png",
    ]
    assert (legacy_root / "stage3" / "combined" / "MG0414" / "channel" / "plots" / "MG0414.channel.png").exists()

    combined_meta = next((new_root / "stage3" / "combined").rglob("*.combined.stage3.metadata.json"))
    payload = json.loads(combined_meta.read_text())
    assert payload["rcusp_available"] is True
    assert payload["layout"] == {"columns": 4, "rows": "adaptive"}


def test_stage3_rcusp_can_be_disabled_and_frame_is_configurable(project: Path, fake_difmap: Path, quiet) -> None:
    from lenspipe.stage3 import run_stage3

    run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1)
    run_stage2(project, _config(fake_difmap, mode="if", channels_per_if=2), reporter=quiet, workers=1)
    cfg = _config(fake_difmap).with_overrides(
        stage3={"rcusp_images": [], "frequency_frame_ghz": [14.9, 15.1], "frequency_ticks_ghz": [15.0]}
    )
    summary = run_stage3(project, cfg, reporter=quiet, workers=1)
    assert summary.ok
    combined_meta = next((project / "stage3" / "combined").rglob("*.combined.stage3.metadata.json"))
    payload = json.loads(combined_meta.read_text())
    assert payload["rcusp_available"] is False
    assert payload["frequency_limits_ghz"] == [14.9, 15.1]
    assert not list((project / "stage3").rglob("*rcusp*"))
