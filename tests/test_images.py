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


def _png_size(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


def test_stage3_writes_a4_pages_per_map_and_all_epochs_grids(staged: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, center="64.24,5.57", size_arcsec=[1.2, 0.9], cmap="magma", pmax=99.0, residual_pmax=97.0)
    assert run_stage3(staged, cfg, reporter=quiet, workers=1).ok
    plots = staged / "stage3" / "MG0414.A" / "channel" / "plots"
    clean, residual = plots / "MG0414.A.channel.image_clean.png", plots / "MG0414.A.channel.image_residual.png"
    assert clean.is_file() and residual.is_file()
    assert not (plots / "MG0414.A.channel.images.png").exists()  # the 2.0.14 side-by-side figure is gone
    assert _png_size(clean) == (1654, 2338) and _png_size(residual) == (1654, 2338)  # A4 portrait at 200 dpi
    meta = json.loads(next((plots.parent).glob("*.stage3.metadata.json")).read_text())
    settings = meta["images"]["settings"]
    assert meta["images"]["written"] is True and settings["cmap"] == "magma"
    assert settings["size_arcsec"] == [1.2, 0.9] and settings["pmax"] == 99.0 and settings["residual_pmax"] == 97.0
    assert set(meta["images"]["files"]) == {"clean", "residual"}

    combined = staged / "stage3" / "combined" / "MG0414" / "channel" / "plots"
    grid_clean = combined / "MG0414.channel.images_clean_all_epochs.png"
    grid_residual = combined / "MG0414.channel.images_residual_all_epochs.png"
    assert grid_clean.is_file() and grid_residual.is_file()
    assert _png_size(grid_clean) == (1654, 2338)  # A4 portrait at 200 dpi, two visits per row
    cmeta = json.loads(next(combined.parent.glob("*.combined.stage3.metadata.json")).read_text())
    assert cmeta["images"]["written"] is True and cmeta["images"]["epochs"] == ["A", "B"]
    assert cmeta["images"]["files"]["residual"] == ["MG0414.channel.images_residual_all_epochs"]
    assert meta["images"]["files"] == {
        "clean": "MG0414.A.channel.image_clean", "residual": "MG0414.A.channel.image_residual",
    }


def test_residual_and_clean_pmax_are_independent() -> None:
    cfg = LenspipeConfig().with_overrides(stage3={"images": {"pmax": 99.9, "residual_pmax": 95.0}})
    settings = ImageSettings.from_config(cfg.stage3.images)
    assert settings.pmax_for("clean") == 99.9 and settings.pmax_for("residual") == 95.0
    with pytest.raises(ValueError):
        LenspipeConfig().with_overrides(stage3={"images": {"residual_pmax": 0}})


def test_stage3_map_figures_can_be_disabled_or_missing(staged: Path, fake_difmap: Path, capsys) -> None:
    quiet = Reporter(stream=io.StringIO())
    assert run_stage3(staged, _config(fake_difmap, enabled=False), reporter=quiet, workers=1).ok
    assert not list((staged / "stage3").rglob("*.image*"))

    (staged / "stage1" / "MG0414.B" / "MG0414.B.resid.fits").unlink()
    assert run_stage3(staged, _config(fake_difmap), reporter=quiet, workers=1, overwrite=True).ok
    assert (staged / "stage3" / "MG0414.A" / "channel" / "plots" / "MG0414.A.channel.image_clean.png").is_file()
    assert not list((staged / "stage3" / "MG0414.B" / "channel" / "plots").glob("*.image_*"))
    combined = staged / "stage3" / "combined" / "MG0414" / "channel"
    cmeta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert cmeta["images"]["epochs"] == ["A"] and "B" in cmeta["images"]["reason"]
    assert "IMAGES SKIPPED: MG0414.B.channel" in capsys.readouterr().out


def test_stage3_cli_image_flags_reach_the_metadata(staged: Path, fake_difmap: Path) -> None:
    result = runner.invoke(app, [
        "stage3", str(staged), "--product", "channel", "--workers", "1", "--formats", "png",
        "--image-size", "1.0", "--image-cmap", "plasma", "--image-pmax", "98", "--image-residual-pmax", "96",
        "--image-center", "64.24,5.57",
    ])
    assert result.exit_code == 0, result.output
    meta = json.loads(next((staged / "stage3" / "MG0414.A" / "channel").glob("*.stage3.metadata.json")).read_text())
    settings = meta["images"]["settings"]
    assert {k: settings[k] for k in ("enabled", "center", "size_arcsec", "cmap", "pmax", "residual_pmax", "vmin")} == {
        "enabled": True, "center": "64.24,5.57", "size_arcsec": [1.0, 1.0], "cmap": "plasma",
        "pmax": 98.0, "residual_pmax": 96.0, "vmin": 0.0,
    }
    result = runner.invoke(app, ["stage3", str(staged), "--product", "channel", "--workers", "1", "--overwrite", "--no-images"])
    assert result.exit_code == 0, result.output
    assert not list((staged / "stage3").rglob("*.image*"))


def test_build_steps_passes_image_options(tmp_path: Path) -> None:
    request = RunRequest(
        root=tmp_path, stages=[3], stage3_images=False, stage3_image_center="64.24,5.57",
        stage3_image_size="2,1.5", stage3_image_cmap="magma", stage3_image_pmax=99.0, stage3_image_residual_pmax=95.5,
        stage3_image_reference_epoch="B",
    )
    steps, _ = build_steps(request)
    assert steps[0].argv == [
        "stage3", str(tmp_path), "--no-images", "--image-center", "64.24,5.57", "--image-size", "2,1.5",
        "--image-cmap", "magma", "--image-pmax", "99", "--image-residual-pmax", "95.5",
        "--image-reference-epoch", "B",
    ]


# ---------------------------------------------------------------------------
# All-epochs pages: shared colour scale from a reference visit, transparent, offset axes


def _alpha_at_corner(path: Path) -> int:
    from PIL import Image

    with Image.open(path) as image:
        return image.convert("RGBA").getpixel((2, 2))[3]


def test_all_epochs_pages_share_the_reference_visit_scale_and_are_transparent(staged: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, reference_epoch="B")
    assert run_stage3(staged, cfg, reporter=quiet, workers=1).ok
    combined = staged / "stage3" / "combined" / "MG0414" / "channel"
    cmeta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    files = cmeta["images"]["files"]
    assert files["reference_epoch"] == "B"
    assert set(files["shared_limits"]) == {"clean", "residual"}
    assert files["shared_limits"]["clean"][1] > files["shared_limits"]["clean"][0]
    assert cmeta["images"]["settings"]["reference_epoch"] == "B"
    assert cmeta["images"]["settings"]["transparent_all_epochs"] is True
    assert "arcsec" in cmeta["images"]["settings"]["axes"]

    page = combined / "plots" / "MG0414.channel.images_clean_all_epochs.png"
    visit_page = staged / "stage3" / "MG0414.A" / "channel" / "plots" / "MG0414.A.channel.image_clean.png"
    assert _alpha_at_corner(page) == 0  # transparent background
    assert _alpha_at_corner(visit_page) == 255  # per-visit pages stay opaque

    # An unknown reference falls back to the first visit rather than failing.
    assert run_stage3(staged, _config(fake_difmap, reference_epoch="nope"), reporter=quiet, workers=1, overwrite=True).ok
    cmeta = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert cmeta["images"]["files"]["reference_epoch"] == "A"


def test_residual_without_beam_keywords_borrows_the_clean_beam(staged: Path, fake_difmap: Path, monkeypatch) -> None:
    """Residual maps from Stage 1 runs before 2.0.14 carry no BMAJ/BMIN/BPA; every panel still shows a beam."""
    from matplotlib.patches import Ellipse

    from lenspipe.stage3 import images as images_module
    from lenspipe.stage3 import plotting

    residual = staged / "stage1" / "MG0414.B" / "MG0414.B.resid.fits"
    with fits.open(residual, mode="update") as hdul:
        for key in ("BMAJ", "BMIN", "BPA"):
            del hdul[0].header[key]
        hdul.flush()
    assert images_module.load_map(residual).beam is None

    captured: list = []
    real_save = plotting.save_figure
    monkeypatch.setattr(
        plotting, "save_figure",
        lambda fig, base, dpi=300, bbox_inches="tight", transparent=False: (
            captured.append((Path(base).name, fig)), real_save(fig, base, dpi=dpi, bbox_inches=bbox_inches, transparent=transparent)
        ),
    )
    settings = images_module.ImageSettings.from_config(_config(fake_difmap).stage3.images)
    entries = [(e, *images_module.stage1_images_for(staged, "MG0414", e)) for e in ("A", "B")]
    images_module.write_all_epochs_images(entries, staged / "x" / "MG0414.channel", settings, "MG0414")
    _, fig = next(item for item in captured if "images_residual_all_epochs" in item[0])
    panels = [ax for ax in fig.axes if ax.get_images()]
    assert len(panels) == 2
    assert all(any(isinstance(p, Ellipse) for p in ax.patches) for ax in panels)


def test_map_panels_use_offset_axes_in_arcsec_with_shared_limits(staged: Path, fake_difmap: Path, monkeypatch) -> None:
    from lenspipe.stage3 import images as images_module
    from lenspipe.stage3 import plotting

    captured: list = []
    real_save = plotting.save_figure

    def spy(fig, base_path, dpi=300, bbox_inches="tight", transparent=False):
        captured.append((Path(base_path).name, fig, transparent))
        real_save(fig, base_path, dpi=dpi, bbox_inches=bbox_inches, transparent=transparent)

    monkeypatch.setattr(plotting, "save_figure", spy)
    settings = images_module.ImageSettings.from_config(_config(fake_difmap, size_arcsec=1.0).stage3.images)
    entries = [(e, *images_module.stage1_images_for(staged, "MG0414", e)) for e in ("A", "B")]
    record = images_module.write_all_epochs_images(entries, staged / "x" / "MG0414.channel", settings, "MG0414")
    assert record["reference_epoch"] == "A"
    name, fig, transparent = next(item for item in captured if "images_clean_all_epochs" in item[0])
    assert transparent is True
    panels = [ax for ax in fig.axes if ax.get_images()]
    assert len(panels) == 2
    for ax in panels:
        image = ax.get_images()[0]
        assert image.get_clim() == tuple(record["shared_limits"]["clean"])  # same colour, same flux
        left, right, bottom, top = image.get_extent()
        assert left > right  # east (positive RA offset) on the left
        assert abs((left - right) - 1.0) < 0.05 and abs((top - bottom) - 1.0) < 0.05  # 1 arcsec cutout
    assert "RA [arcsec]" in panels[0].get_xlabel() and "Dec [arcsec]" in panels[0].get_ylabel()
