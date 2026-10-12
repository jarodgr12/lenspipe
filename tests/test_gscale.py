"""A Stage 1 self-cal step can end with DifMAP's gscale (overall per-telescope amplitude correction)."""

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


def test_gscale_step_forms_and_commands() -> None:
    plain = SelfcalStep.model_validate([False, False, 0.5, 50])
    assert plain.gscale is False
    assert plain.difmap_commands() == ["selfcal false,false,0.5", "modelfit 50"]

    with_gscale = SelfcalStep.model_validate([False, False, 3600, 30, True])
    assert with_gscale.gscale is True
    # The step keeps its selfcal; gscale and a second modelfit come at the end.
    assert with_gscale.difmap_commands() == [
        "selfcal false,false,3600", "modelfit 30", "gscale false", "modelfit 30",
    ]
    floating = SelfcalStep.model_validate({"amplitude": True, "float_amplitude": True, "solint_min": 1, "modelfit_iterations": 20, "gscale": True})
    assert floating.difmap_commands()[2] == "gscale true"
    assert SelfcalStep().gscale is False  # default steps are unchanged
    with pytest.raises(ValueError):
        SelfcalStep.model_validate(["gscale", True, 30])  # the 2.0.23 replace-form is gone
    with pytest.raises(ValueError):
        SelfcalStep.model_validate([False, False, 1])


def test_stage1_script_emits_gscale_at_the_end_of_its_step(project: Path) -> None:
    cfg = LenspipeConfig().with_overrides(
        stage1={"selfcal": [[False, False, 3600, 50], [False, False, 1, 50, True], [True, True, 3600, 50]]}
    )
    text, _ = stage1_commands(
        uvfits=Path("/i/x.uvfits"), starting_model=Path("/i/x.gmod"), residual_image=Path("/o/r.fits"),
        clean_image=Path("/o/c.fits"), calibrated_uvfits=Path("/o/x.cal.uvf"), final_model=Path("/o/x.gmod"),
        config=cfg.stage1,
    )
    assert (
        "selfcal false,false,3600\nmodelfit 50\n\n"
        "selfcal false,false,1\nmodelfit 50\ngscale false\nmodelfit 50\n\n"
        "selfcal true,true,3600\nmodelfit 50"
    ) in text
    assert text.count("gscale") == 1


def test_gscale_schedule_survives_toml_round_trip() -> None:
    cfg = LenspipeConfig().with_overrides(stage1={"selfcal": [[False, False, 10, 5], [True, True, 1, 7, True]]})
    assert "[amplitude, float_amplitude, solint_min, modelfit_iterations, true]" in render_toml()
    import tomli_w

    reloaded = LenspipeConfig.model_validate(
        tomllib.loads(tomli_w.dumps(cfg.model_dump(mode="json", exclude_none=True)))  # as the Parameters page saves
    )
    assert [step.gscale for step in reloaded.stage1.selfcal] == [False, True]
    assert reloaded.stage1.selfcal[1].difmap_commands()[2] == "gscale true"
    parsed = LenspipeConfig.model_validate(
        tomllib.loads('[stage1]\nselfcal = [[false, false, 3600, 50], [false, false, 1, 50, true]]\n')
    )
    assert parsed.stage1.selfcal[1].gscale is True and parsed.stage1.selfcal[1].solint_min == 1


def test_stage1_runs_a_step_with_gscale_and_records_it(project: Path, fake_difmap: Path) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}},
        stage1={"selfcal": [[False, False, 3600, 50], [False, False, 1, 50, True]]},
    )
    results = run_stage1(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert results[0].ok, results[0].message
    log = results[0].paths.log_file.read_text()  # DifMAP's output: two selfcals and one gscale answered
    assert "Performing overall amplitude self-cal" in log
    assert log.count("Applying selfcal corrections") == 3
    meta = json.loads(results[0].paths.stage_metadata.read_text())
    assert [step["gscale"] for step in meta["config"]["selfcal"]] == [False, True]
