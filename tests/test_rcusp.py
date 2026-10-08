"""R_cusp must never vanish silently: when the configured image names do not match the model's
groups, Stage 3 says so in its log and metadata, the doctor warns, and the Results page shows why."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from lenspipe.config import LenspipeConfig
from lenspipe.doctor import run_checks
from lenspipe.progress import Reporter
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import run_stage2
from lenspipe.stage3 import run_stage3
from lenspipe.stage3.analysis import rcusp_status
from lenspipe.ui.interactive import combined_notes


def _config(fake_difmap: Path, **stage3) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(
        project={"difmap": {"executable": str(fake_difmap)}}, stage2={"mode": "channel", "shards": 1}
    )
    return cfg.with_overrides(stage3={"figure_formats": ["png"], **stage3})


def _rename_groups_abcd(project: Path) -> None:
    """Relabel the fixture model's groups A1, A2, B, C as A, B, C, D (a different source's naming)."""
    gmod = project / "inputs" / "MG0414.gmod"
    text = gmod.read_text()
    # Rename from the last group backwards so a new name never collides with an old one.
    for old, new in (("C", "D"), ("B", "C"), ("A2", "B"), ("A1", "A")):
        text = re.sub(rf"^! GROUP {old}$", f"! GROUP {new}", text, flags=re.M)
        text = re.sub(rf"^! COMPONENT {old}([a-z])$", rf"! COMPONENT {new}\1", text, flags=re.M)
    gmod.write_text(text)


def test_rcusp_status_explains_missing_groups() -> None:
    class Fit:
        def __init__(self, s, e):
            self.s_ref_jy, self.s_ref_error_jy = s, e

    class Analysis:
        def __init__(self, epoch, fits):
            self.spectral_fits = fits
            self.dataset = type("D", (), {"epoch": epoch})()

    good = {"A": Fit(1.0, 0.1), "B": Fit(0.5, 0.1), "C": Fit(0.2, 0.1)}
    status = rcusp_status([Analysis("A", good)], ["A1", "A2", "B"])
    assert status["available"] is False
    assert "A1" in status["reason"] and "A2" in status["reason"]
    assert status["groups"] == ["A", "B", "C"]
    assert "stage3.rcusp_images" in status["reason"]

    assert rcusp_status([Analysis("A", good)], [])["reason"].startswith("disabled")
    assert rcusp_status([Analysis("A", good)], ["A", "B", "C"]) == {
        "available": True, "reason": None, "groups": ["A", "B", "C"], "images": ["A", "B", "C"], "note": None,
    }

    nan_fit = {"A": Fit(float("nan"), 0.1), "B": Fit(0.5, 0.1), "C": Fit(0.2, 0.1)}
    partial = rcusp_status([Analysis("A", good), Analysis("B", nan_fit)], ["A", "B", "C"])
    assert partial["available"] is True and "B" in partial["note"]
    none = rcusp_status([Analysis("B", nan_fit)], ["A", "B", "C"])
    assert none["available"] is False and "not finite" in none["reason"]


def test_stage3_reports_why_rcusp_was_skipped(project: Path, fake_difmap: Path, capsys) -> None:
    _rename_groups_abcd(project)
    quiet_stream = io.StringIO()
    quiet = Reporter(stream=quiet_stream)
    cfg = _config(fake_difmap)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok

    combined = project / "stage3" / "combined" / "MG0414" / "channel"
    payload = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert payload["rcusp_available"] is False
    assert payload["groups"] == ["A", "B", "C", "D"]
    assert payload["rcusp_images"] == ["A1", "A2", "B"]
    assert "A1" in payload["rcusp_reason"]
    assert not list(combined.rglob("*rcusp*"))

    printed = capsys.readouterr().out
    assert "R_CUSP SKIPPED" in printed and "A, B, C, D" in printed
    assert "R_cusp" in quiet_stream.getvalue() and "rcusp_images" in quiet_stream.getvalue()
    assert any("R_cusp not computed" in note for note in combined_notes(combined))


def test_stage3_rcusp_works_with_renamed_groups_once_configured(project: Path, fake_difmap: Path) -> None:
    _rename_groups_abcd(project)
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, rcusp_images=["A", "B", "C"])
    run_stage1(project, cfg, reporter=quiet, workers=1)
    run_stage2(project, cfg, reporter=quiet, workers=1)
    assert run_stage3(project, cfg, reporter=quiet, workers=1).ok
    combined = project / "stage3" / "combined" / "MG0414" / "channel"
    payload = json.loads(next(combined.glob("*.combined.stage3.metadata.json")).read_text())
    assert payload["rcusp_available"] is True and payload["rcusp_reason"] is None
    assert (combined / "tables" / "MG0414.channel.rcusp_vs_mjd.csv").is_file()
    assert combined_notes(combined) == []

    fits_payload = json.loads((combined / "tables" / "MG0414.channel.combined_fits.json").read_text())
    rcusp = fits_payload["rcusp"]
    per_visit = pd.read_csv(combined / "tables" / "MG0414.channel.rcusp_vs_mjd.csv")
    values = per_visit["rcusp_a1_a2_b"].to_numpy()
    errors = per_visit["rcusp_a1_a2_b_error"].to_numpy()
    weights = 1.0 / errors**2
    assert np.isclose(rcusp["value"], np.sum(weights * values) / np.sum(weights))
    assert np.isclose(rcusp["error"], 1.0 / np.sqrt(np.sum(weights)))  # formal: from the propagated errors only
    assert np.isclose(rcusp["visit_scatter"], np.std(values, ddof=1))
    assert np.isclose(rcusp["visit_scatter_error_of_mean"], np.std(values, ddof=1) / np.sqrt(values.size))
    rows = pd.read_csv(combined / "tables" / "MG0414.channel.combined_fits.csv")
    assert sorted(rows[rows["product"] == "rcusp"]["parameter"]) == ["constant", "visit_scatter"]


def test_doctor_checks_rcusp_images_against_the_model(project: Path, fake_difmap: Path) -> None:
    checks = {c.name: c for c in run_checks(project, _config(fake_difmap))}
    assert checks["rcusp"].status == "ok" and "A1, A2, B" in checks["rcusp"].detail

    checks = {c.name: c for c in run_checks(project, _config(fake_difmap, rcusp_images=["A", "B", "C"]))}
    assert checks["rcusp"].status == "warn"
    assert "A1, A2, B, C" in checks["rcusp"].detail and "stage3.rcusp_images" in (checks["rcusp"].fix or "")

    checks = {c.name: c for c in run_checks(project, _config(fake_difmap, rcusp_images=[]))}
    assert checks["rcusp"].status == "ok" and "disabled" in checks["rcusp"].detail
