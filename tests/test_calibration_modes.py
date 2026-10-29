"""Stage 1 calibration modes, the common-grid imaging pass and the per-mode layout."""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest
from astropy.io import fits

from lenspipe.config import LenspipeConfig, SelfcalStep, Stage1Config, render_toml
from lenspipe.difmap.scripts import stage1_commands
from lenspipe.models import write_fixed_spectral_index_model
from lenspipe.progress import Reporter
from lenspipe.project import Layout, migrate_flat_layout
from lenspipe.stage1 import common_cell_mas, run_stage1, same_grid


def _cfg(fake_difmap: Path, **stage1) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap), "stream": "pipe"}})
    return cfg.with_overrides(stage1=stage1) if stage1 else cfg


def test_config_mode_and_per_if_layout() -> None:
    cfg = Stage1Config()
    assert cfg.calibration_mode == "full-band"
    assert cfg.per_if.channel_ranges()[:2] == [(1, 64), (65, 128)] and cfg.per_if.total_channels == 3072
    per_if = Stage1Config.model_validate({"calibration_mode": "per-if", "per_if": {"if_count": 2, "channels_per_if": 4, "edge_channels": 1}})
    assert per_if.per_if.channel_ranges() == [(2, 3), (6, 7)]
    with pytest.raises(ValueError):
        Stage1Config.model_validate({"per_if": {"channels_per_if": 4, "edge_channels": 2}})
    with pytest.raises(ValueError):
        Stage1Config.model_validate({"calibration_mode": "sideways"})
    # The 2.0.25 boolean is accepted and mapped onto the mode.
    assert Stage1Config.model_validate({"selfcal_per_if": True}).calibration_mode == "per-if"
    assert Stage1Config.model_validate({"selfcal_per_if": False}).calibration_mode == "full-band"
    parsed = LenspipeConfig.model_validate(tomllib.loads(render_toml()))
    assert parsed.stage1.calibration_mode == "full-band" and parsed.stage1.per_if.if_count == 48


def test_per_if_step_rendering_swaps_amplitude_selfcal_for_gscale() -> None:
    amplitude = SelfcalStep.model_validate([True, True, 3600, 50])
    assert amplitude.difmap_commands() == ["selfcal true,true,3600", "modelfit 50"]
    assert amplitude.difmap_commands(per_if=True) == ["gscale", "modelfit 50"]
    phase = SelfcalStep.model_validate([False, False, 0.5, 20, True])
    assert phase.difmap_commands(per_if=True) == ["selfcal false,false,0.5", "modelfit 20", "gscale false", "modelfit 20"]


def test_per_if_script_structure() -> None:
    cfg = Stage1Config.model_validate(
        {"calibration_mode": "per-if", "per_if": {"if_count": 2, "channels_per_if": 4, "edge_channels": 1},
         "selfcal": [[False, False, 3600, 50], [True, True, 3600, 50], [False, False, 0.5, 30]]}
    )
    text, _ = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/nr.fits"),
        clean_image=Path("/o/nc.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
        config=cfg, fixed_index_model=Path("/o/x.fixed.gmod"), per_if_models=[Path("/o/x.if001.gmod"), Path("/o/x.if002.gmod")],
    )
    one_if = (
        "rmodel /o/x.fixed.gmod\nselfcal false,false,3600\nmodelfit 50\ngscale\nmodelfit 50\n"
        "selfcal false,false,0.5\nmodelfit 30\n"
    )
    assert "! IF 1: channels 2-3\nselect i,2,3\n" + one_if + "wmodel /o/x.if001.gmod\n" in text
    assert "! IF 2: channels 6-7\nselect i,6,7\n" + one_if + "wmodel /o/x.if002.gmod\n" in text
    tail = text.split("wmodel /o/x.if002.gmod\n")[1]
    assert tail.startswith("! Full band")
    assert "select i,1,8\nrmodel /i/x.gmod\nmodelfit 30\n" in tail  # original master model, last step's iterations
    assert tail.count("selfcal") == 0 and tail.count("gscale") == 0  # no self-cal after the final fit
    assert "wobs /o/x.cal.uvf\nwmodel /o/x.gmod\n" in tail and "mapsize 1024\n" in text
    with pytest.raises(ValueError):
        stage1_commands(
            uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/nr.fits"),
            clean_image=Path("/o/nc.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
            config=cfg,
        )


def test_fixed_spectral_index_model_pins_only_the_ninth_column(tmp_path: Path) -> None:
    master = tmp_path / "m.gmod"
    master.write_text("! GROUP A\n! COMPONENT a\n0.3v 0.0v 0.0v 0.8v 1.0 0.0 1 0 0.7v\n! COMPONENT b\n0.1v 1.9v 42.0v 0.0 1.0 0.0 0\n")
    out = tmp_path / "fixed.gmod"
    assert write_fixed_spectral_index_model(master, out) == 1
    assert out.read_text() == "! GROUP A\n! COMPONENT a\n0.3v 0.0v 0.0v 0.8v 1.0 0.0 1 0 0\n! COMPONENT b\n0.1v 1.9v 42.0v 0.0 1.0 0.0 0\n"


def test_full_band_run_images_twice_on_one_grid(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    results = run_stage1(project, _cfg(fake_difmap), reporter=quiet, workers=2)
    statuses = sorted((r.paths.epoch, r.status) for r in results)
    assert statuses == [("A", "completed"), ("A", "reimaged"), ("B", "completed"), ("B", "reimaged")]
    tree = Layout.at(project).stage_dir(1, "full-band")
    for epoch in ("A", "B"):
        d = tree / f"MG0414.{epoch}"
        for suffix in ("cal.uvf", "gmod", "native.cln.fits", "native.resid.fits", "cln.fits", "resid.fits",
                       "stage1.difmap.log", "stage1.reimage.difmap.log"):
            assert (d / f"MG0414.{epoch}.{suffix}").is_file(), suffix
        meta = json.loads((d / f"MG0414.{epoch}.stage1.metadata.json").read_text())
        assert meta["calibration_mode"] == "full-band" and meta["per_if"] is None
        assert meta["native_residual_rms_jy_per_beam"] is not None and meta["final_residual_rms_jy_per_beam"] is not None
        assert meta["common_image_geometry"]["naxis1"] == 128 and meta["common_cell_mas"] == pytest.approx(25.0)
        assert meta["clean_beam_deg"] and meta["native_clean_beam_deg"]
        assert "BMAJ" in fits.getheader(d / f"MG0414.{epoch}.resid.fits")  # beam copied on the final residual too
        manifest = json.loads((d / f"MG0414.{epoch}.stage1.manifest.json").read_text())
        assert f"MG0414.{epoch}.cln.fits" in manifest["products"] and f"MG0414.{epoch}.native.cln.fits" in manifest["products"]
    a = json.loads((tree / "MG0414.A" / "MG0414.A.stage1.metadata.json").read_text())["common_image_geometry"]
    b = json.loads((tree / "MG0414.B" / "MG0414.B.stage1.metadata.json").read_text())["common_image_geometry"]
    assert same_grid(a, b)
    assert not (project / "stage1" / "MG0414.A").exists()  # nothing written to the flat location


def test_configured_cell_wins_and_reimage_only_redoes_maps(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    results = run_stage1(project, _cfg(fake_difmap, cell_mas=10.0), reporter=quiet, workers=1, epochs={"A"})
    assert [r.status for r in results] == ["completed", "reimaged"]
    d = Layout.at(project).stage_dir(1, "full-band") / "MG0414.A"
    assert abs(fits.getheader(d / "MG0414.A.cln.fits")["CDELT2"]) * 3.6e6 == pytest.approx(10.0)
    assert abs(fits.getheader(d / "MG0414.A.native.cln.fits")["CDELT2"]) * 3.6e6 == pytest.approx(25.0)
    before = (d / "MG0414.A.cal.uvf").stat().st_mtime_ns
    again = run_stage1(project, _cfg(fake_difmap, cell_mas=12.0), reporter=quiet, workers=1, reimage_only=True)
    assert [r.status for r in again] == ["reimaged"]  # only A is calibrated; nothing else to image
    assert (d / "MG0414.A.cal.uvf").stat().st_mtime_ns == before  # calibration untouched
    assert abs(fits.getheader(d / "MG0414.A.cln.fits")["CDELT2"]) * 3.6e6 == pytest.approx(12.0)
    meta = json.loads((d / "MG0414.A.stage1.metadata.json").read_text())
    assert meta["common_cell_mas"] == 12.0 and meta["calibration_mode"] == "full-band"


def test_common_cell_rules() -> None:
    g = lambda cell, ctype="RA---SIN", sign=-1: {  # noqa: E731
        "ctype1": ctype, "ctype2": "DEC--SIN", "cdelt1_deg": sign * cell / 3.6e6, "cdelt2_deg": cell / 3.6e6, "cell_mas": cell,
    }
    assert common_cell_mas([g(25), g(10), g(30)]) == 10
    assert common_cell_mas([g(25), g(10)], requested=7.5) == 7.5
    with pytest.raises(ValueError):
        common_cell_mas([g(25), g(10, ctype="RA---TAN")])
    with pytest.raises(ValueError):
        common_cell_mas([g(25), g(10, sign=+1)])
    with pytest.raises(ValueError):
        common_cell_mas([])


def test_modes_coexist_and_flat_layout_is_migrated(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    # A pre-2.1 project: flat stage1/<prefix> directories.
    flat = project / "stage1" / "MG0414.Z"
    flat.mkdir(parents=True)
    (flat / "MG0414.Z.stage1.metadata.json").write_text("{}")
    (project / "stage2" / "MG0414.Z").mkdir(parents=True)
    (project / "stage2" / "MG0414.Z" / "MG0414.Z.channel.stage2.metadata.json").write_text("{}")
    (project / "stage3" / "combined" / "MG0414").mkdir(parents=True)
    notes = migrate_flat_layout(project)
    assert len(notes) == 3 and all("moved" in n for n in notes)
    assert (project / "stage1" / "full-band" / "MG0414.Z" / "MG0414.Z.stage1.metadata.json").is_file()
    assert (project / "stage2" / "full-band" / "MG0414.Z").is_dir()
    assert (project / "stage3" / "full-band" / "combined" / "MG0414").is_dir()
    assert migrate_flat_layout(project) == []  # idempotent

    run_stage1(project, _cfg(fake_difmap), reporter=quiet, workers=1, epochs={"A"})
    per_if = _cfg(fake_difmap, calibration_mode="per-if", per_if={"if_count": 2, "channels_per_if": 4, "edge_channels": 0})
    results = run_stage1(project, per_if, reporter=quiet, workers=1, epochs={"A"})
    assert all(r.ok for r in results), [r.message for r in results]
    layout = Layout.at(project)
    assert (layout.stage_dir(1, "full-band") / "MG0414.A" / "MG0414.A.cal.uvf").is_file()
    d = layout.stage_dir(1, "per-if") / "MG0414.A"
    assert (d / "MG0414.A.cal.uvf").is_file() and (d / "MG0414.A.if001.gmod").is_file() and (d / "MG0414.A.if002.gmod").is_file()
    assert "! COMPONENT A1a" in (d / "MG0414.A.if002.gmod").read_text()
    meta = json.loads((d / "MG0414.A.stage1.metadata.json").read_text())
    assert meta["calibration_mode"] == "per-if"
    assert meta["per_if"]["selected_ranges_one_based"] == [
        {"if_number": 1, "first_channel": 1, "last_channel": 4, "channels": [[1, 4]], "n_channels": 4},
        {"if_number": 2, "first_channel": 5, "last_channel": 8, "channels": [[5, 8]], "n_channels": 4},
    ]
    assert meta["per_if"]["per_if_models"] == ["MG0414.A.if001.gmod", "MG0414.A.if002.gmod"]
    assert layout.modes_present(1) == ["full-band", "per-if"]
    log = (d / "MG0414.A.stage1.difmap.log").read_text()
    assert log.count("Performing overall amplitude self-cal") == 2  # one gscale per IF (the amplitude step)


def test_dry_run_prints_both_passes(project: Path, fake_difmap: Path) -> None:
    stream = io.StringIO()
    results = run_stage1(project, _cfg(fake_difmap), reporter=Reporter(stream=stream), dry_run=True, epochs={"A"})
    assert [r.status for r in results] == ["dry_run"]
    text = stream.getvalue()
    assert "mapsize 1024\n" in text and "mapsize 1024,0.1" in text and "finest native cell" in text
    assert not (project / "stage1").exists()


def test_per_if_mode_honours_exclude_channels_and_skips_empty_ifs(project: Path, fake_difmap: Path) -> None:
    from lenspipe.ui.commands import RunRequest, build_steps

    cfg = Stage1Config.model_validate({
        "calibration_mode": "per-if",
        "per_if": {"if_count": 3, "channels_per_if": 4, "edge_channels": 0, "exclude_channels": "spw1:1; spw2"},
    })
    kept, skipped = cfg.per_if.selections()
    assert [s.if_number for s in kept] == [1, 2] and skipped == [3]
    text, _ = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/nr.fits"),
        clean_image=Path("/o/nc.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
        config=cfg, fixed_index_model=Path("/o/x.fixed.gmod"),
        per_if_models=[Path("/o/x.if001.gmod"), Path("/o/x.if002.gmod"), Path("/o/x.if003.gmod")],
    )
    assert "! IF(s) skipped (no channels left after exclusions): 3" in text
    assert "! IF 2: channels 5 7-8\nselect i,5,5,7,8\n" in text and "wmodel /o/x.if002.gmod" in text
    assert "wmodel /o/x.if003.gmod" not in text
    assert "select i,1,12\nrmodel /i/x.gmod\n" in text  # final full-band fit untouched
    with pytest.raises(ValueError, match="outside 0-2"):
        Stage1Config.model_validate({"per_if": {"if_count": 3, "channels_per_if": 4, "exclude_channels": "spw3"}})

    quiet = Reporter(stream=io.StringIO())
    run_cfg = _cfg(fake_difmap, calibration_mode="per-if",
                   per_if={"if_count": 2, "channels_per_if": 4, "edge_channels": 0, "exclude_channels": "spw0:1; spw1"})
    results = run_stage1(project, run_cfg, reporter=quiet, workers=1, epochs={"A"})
    assert all(r.ok for r in results), [r.message for r in results]
    d = Layout.at(project).stage_dir(1, "per-if") / "MG0414.A"
    assert (d / "MG0414.A.if001.gmod").is_file() and not (d / "MG0414.A.if002.gmod").exists()
    meta = json.loads((d / "MG0414.A.stage1.metadata.json").read_text())["per_if"]
    assert meta["exclude_channels_spec"] == "spw0:1; spw1" and meta["skipped_ifs"] == [2]
    assert meta["selected_ranges_one_based"] == [
        {"if_number": 1, "first_channel": 1, "last_channel": 4, "channels": [[1, 1], [3, 4]], "n_channels": 3}
    ]
    steps, _ = build_steps(RunRequest(root=project, stages=[1], calibration_mode="per-if", stage1_per_if_exclude="spw0-47:0-7"))
    assert steps[0].argv[-2:] == ["--per-if-exclude-channels", "spw0-47:0-7"]
