"""A Stage 1 self-cal step can be DifMAP's gscale (overall per-telescope amplitude correction)."""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest

from lenspipe.config import LenspipeConfig, SelfcalStep, render_toml
from lenspipe.difmap.scripts import stage1_commands
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1


def test_gscale_step_forms() -> None:
    from_list = SelfcalStep.model_validate(["gscale", True, 30])
    assert from_list.gscale is True and from_list.float_amplitude is True and from_list.modelfit_iterations == 30
    from_dict = SelfcalStep.model_validate({"gscale": True, "float_amplitude": False, "modelfit_iterations": 20})
    assert from_dict.difmap_command() == "gscale false"
    assert from_list.difmap_command() == "gscale true"
    assert SelfcalStep.model_validate([False, False, 0.5, 50]).difmap_command() == "selfcal false,false,0.5"
    assert SelfcalStep().gscale is False  # default steps are unchanged
    with pytest.raises(ValueError):
        SelfcalStep.model_validate(["gscale", True])


def test_stage1_script_emits_gscale_in_sequence(project: Path) -> None:
    cfg = LenspipeConfig().with_overrides(
        stage1={"selfcal": [[False, False, 3600, 50], ["gscale", False, 50], [True, True, 3600, 50]]}
    )
    text, _ = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
        config=cfg.stage1,
    )
    assert "selfcal false,false,3600\nmodelfit 50\n\ngscale false\nmodelfit 50\n\nselfcal true,true,3600\nmodelfit 50" in text


def test_gscale_schedule_survives_toml_round_trip() -> None:
    cfg = LenspipeConfig().with_overrides(stage1={"selfcal": [[False, False, 10, 5], ["gscale", True, 7]]})
    text = render_toml()  # defaults: documents the gscale form in the comments
    assert '["gscale", float_amplitude, modelfit_iterations]' in text
    import tomli_w

    reloaded = LenspipeConfig.model_validate(
        tomllib.loads(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True)))  # as the Parameters page saves
    )
    assert [step.gscale for step in reloaded.stage1.selfcal] == [False, True]
    assert reloaded.stage1.selfcal[1].difmap_command() == "gscale true"
    # The literal list form also parses from TOML.
    parsed = LenspipeConfig.model_validate(tomllib.loads('[stage1]\nselfcal = [[false, false, 3600, 50], ["gscale", false, 50]]\n'))
    assert parsed.stage1.selfcal[1].gscale is True


def test_stage1_runs_a_gscale_step_and_records_it(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage1={"selfcal": [[False, False, 3600, 50], ["gscale", False, 50]]},
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert results[0].ok, results[0].message
    log = results[0].paths.log_file.read_text()  # DifMAP's output, so the gscale response rather than the command
    assert "Performing overall amplitude self-cal" in log and log.count("Applying selfcal corrections") == 2
    meta = json.loads(results[0].paths.stage_metadata.read_text())
    assert [step["gscale"] for step in meta["config"]["selfcal"]] == [False, True]
