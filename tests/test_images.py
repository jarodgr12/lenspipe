"""Clean/residual map figures: Stage 1 stamps the beam on the residual, Stage 3 draws cutouts."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from astropy.io import fits
from typer.testing import CliRunner

from lenspipe.cli import app
from lenspipe.config import LenspipeConfig
from lenspipe.progress import Reporter
from lenspipe.stage1 import copy_beam_keywords, run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.stage3.images import ImageSettings, load_map, parse_center
from lenspipe.ui.commands import RunRequest, build_steps

runner = CliRunner()


def _config(fake_difmap: Path, **images) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}}, stage2={"mode": "channel", "shards": 1}
    )
    return cfg.with_overrides(stage3={"figure_formats": ["png"], **({"images": {**cfg.stage3.images.model_dump(), **images}} if images else {})})


# ---------------------------------------------------------------------------
# Stage 1: beam keywords


def test_stage1_copies_the_clean_beam_onto_the_residual_map(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    results = run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1, epochs={"A"})
    assert results[0].ok, results[0].message
    paths = results[0].paths
    clean = fits.getheader(paths.clean_image)
    residual = fits.getheader(paths.residual_image)
    for key in ("BMAJ", "BMIN", "BPA"):
        assert residual[key] == clean[key]
    assert "NITER" not in residual  # only the beam is copied
    meta = json.loads(paths.stage_metadata.read_text())
    assert meta["clean_beam_deg"] == {"bmaj": clean["BMAJ"], "bmin": clean["BMIN"], "bpa": clean["BPA"]}


def test_copy_beam_keywords_returns_none_without_a_beam(tmp_path: Path) -> None:
    clean = tmp_path / "c.fits"
    residual = tmp_path / "r.fits"
    fits.PrimaryHDU(data=[[0.0]]).writeto(clean)
    fits.PrimaryHDU(data=[[0.0]]).writeto(residual)
    assert copy_beam_keywords(clean, residual) is None
    assert "BMAJ" not in fits.getheader(residual)


# ---------------------------------------------------------------------------
# Image settings


def test_parse_center_accepts_degrees_sexagesimal_and_default(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    paths = run_stage1(project, _config(fake_difmap), reporter=quiet, workers=1, epochs={"A"})[0].paths
    image = load_map(paths.clean_image)
    assert image.data.shape == (128, 128) and image.beam is not None
    centre = parse_center(None, image)
    assert abs(centre.ra.deg - 64.24) < 1e-3 and abs(centre.dec.deg - 5.57) < 1e-3
    degrees = parse_center("64.25, 5.58", image)
    assert abs(degrees.ra.deg - 64.25) < 1e-9 and abs(degrees.dec.deg - 5.58) < 1e-9
    sexagesimal = parse_center("04h16m57.6s +05d34m12s", image)
    assert abs(sexagesimal.ra.deg - 64.24) < 1e-6 and abs(sexagesimal.dec.deg - 5.57) < 1e-6


def test_image_settings_from_config_normalises_size() -> None:
    cfg = LenspipeConfig().with_overrides(stage3={"images": {"size_arcsec": 1.5}})
    assert ImageSettings.from_config(cfg.stage3.images).size_arcsec == (1.5, 1.5)
    cfg = LenspipeConfig().with_overrides(stage3={"images": {"size_arcsec": [2.0, 1.0], "vmin": None}})
    settings = ImageSettings.from_config(cfg.stage3.images)
    assert settings.size_arcsec == (2.0, 1.0) and settings.vmin is None
    with pytest.raises(ValueError):
        LenspipeConfig().with_overrides(stage3={"images": {"cmap": "not-a-colour-map"}})
    with pytest.raises(ValueError):
        LenspipeConfig().with_overrides(stage3={"images": {"size_arcsec": [1, 2, 3]}})


# ---------------------------------------------------------------------------
# Stage 3 figures


@pytest.fixture
def staged(project: Path, fake_difmap: Path) -> Path:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    return project


def test_stage3_writes_visit_and_all_epochs_map_figures(staged: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, center="64.24,5.57", size_arcsec=[1.2, 0.9], cmap="magma", pmax=99.0)
    assert run_stage3(staged, cfg, reporter=quiet, workers=1).ok
    visit = staged / "stage3" / "MG0414.A" / "channel"
    assert (visit / "plots" / "MG0414.A.channel.images.png").is_file()
    meta = json.loads(next(visit.glob("*.stage3.metadata.json")).read_text())
    assert meta["images"]["written"] is True and meta["images"]["settings"]["cmap"] == "magma"
    assert meta["images"]["settings"]["size_arcsec"] == [1.2, 0.9]
    combined = staged / "stage3" / "combined" / "MG0414" / "channel"
    assert (combined / "plots" / "MG0414.channel.images_all_epochs.png").is_file()
    cmeta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert cmeta["images"]["written"] is True and cmeta["images"]["epochs"] == ["A", "B"]


def test_stage3_map_figures_can_be_disabled_or_missing(staged: Path, fake_difmap: Path, capsys) -> None:
    quiet = Reporter(stream=io.StringIO())
    assert run_stage3(staged, _config(fake_difmap, enabled=False), reporter=quiet, workers=1).ok
    assert not list((staged / "stage3").rglob("*.images*"))

    (staged / "stage1" / "MG0414.B" / "MG0414.B.resid.fits").unlink()
    assert run_stage3(staged, _config(fake_difmap), reporter=quiet, workers=1, overwrite=True).ok
    assert (staged / "stage3" / "MG0414.A" / "channel" / "plots" / "MG0414.A.channel.images.png").is_file()
    assert not (staged / "stage3" / "MG0414.B" / "channel" / "plots" / "MG0414.B.channel.images.png").exists()
    combined = staged / "stage3" / "combined" / "MG0414" / "channel"
    cmeta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert cmeta["images"]["epochs"] == ["A"] and "B" in cmeta["images"]["reason"]
    assert "IMAGES SKIPPED: MG0414.B.channel" in capsys.readouterr().out


def test_stage3_cli_image_flags_reach_the_metadata(staged: Path, fake_difmap: Path) -> None:
    result = runner.invoke(app, [
        "stage3", str(staged), "--product", "channel", "--workers", "1", "--formats", "png",
        "--image-size", "1.0", "--image-cmap", "plasma", "--image-pmax", "98", "--image-center", "64.24,5.57",
    ])
    assert result.exit_code == 0, result.output
    meta = json.loads(next((staged / "stage3" / "MG0414.A" / "channel").glob("*.stage3.metadata.json")).read_text())
    assert meta["images"]["settings"] == {
        "enabled": True, "center": "64.24,5.57", "size_arcsec": [1.0, 1.0], "cmap": "plasma", "pmax": 98.0, "vmin": 0.0,
    }
    result = runner.invoke(app, ["stage3", str(staged), "--product", "channel", "--workers", "1", "--overwrite", "--no-images"])
    assert result.exit_code == 0, result.output
    assert not list((staged / "stage3").rglob("*.images*"))


def test_build_steps_passes_image_options(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[3], stage3_images=False, stage3_image_center="64.24,5.57",
        stage3_image_size="2,1.5", stage3_image_cmap="magma", stage3_image_pmax=99.0,
    )
    steps, _ = build_steps(request)
    assert steps[0].argv == [
        "stage3", str(tmp_path), "--no-images", "--image-center", "64.24,5.57", "--image-size", "2,1.5",
        "--image-cmap", "magma", "--image-pmax", "99",
    ]
