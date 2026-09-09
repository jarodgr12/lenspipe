"""Model parsing, labelling and the flux-only transform match the legacy scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from lenspipe import models
from tests.conftest import FIXTURES

MASTER = FIXTURES / "MG0414.gmod"


def test_parse_master_model_hierarchy() -> None:
    hierarchy = models.parse_master_model(MASTER)
    assert hierarchy.groups == {
        "A1": ["A1a", "A1b"],
        "A2": ["A2a"],
        "B": ["Ba", "Bb"],
        "C": ["Ca"],
    }
    assert [c.row_index for c in hierarchy.components] == list(range(6))
    assert hierarchy.group_indices == [("A1", [0, 1]), ("A2", [2]), ("B", [3, 4]), ("C", [5])]


def test_parse_matches_legacy(legacy_stage1) -> None:
    ours = models.parse_master_model(MASTER)
    theirs = legacy_stage1.parse_master_model(MASTER)
    assert [(c.group, c.name, c.row_index) for c in ours.components] == [
        (c.group, c.name, c.row_index) for c in theirs.components
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ("! COMPONENT x\n1 0 0 0 1 0 0\n", "before any GROUP"),
        ("! GROUP A\n1 0 0 0 1 0 0\n", "must follow GROUP and COMPONENT"),
        ("! GROUP A\n! COMPONENT a\n! COMPONENT b\n1 0 0\n", "has no model row"),
        ("! GROUP A\n! COMPONENT a\n1 0 0\n! GROUP A\n! COMPONENT b\n1 0 0\n", "duplicate GROUP"),
        ("! GROUP A\n! GROUP B\n! COMPONENT b\n1 0 0\n", "without components"),
    ],
)
def test_parse_rejects_bad_hierarchy(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "bad.gmod"
    path.write_text(text)
    with pytest.raises(models.ModelFormatError, match=message):
        models.parse_master_model(path)


def test_label_fitted_model_roundtrip(tmp_path: Path, legacy_stage1) -> None:
    hierarchy = models.parse_master_model(MASTER)
    difmap_written = tmp_path / "fitted.gmod"
    rows = [line for line in MASTER.read_text().splitlines() if models.is_model_data_line(line)]
    difmap_written.write_text("! Flux (Jy) Radius (mas) ...\n" + "\n".join(rows) + "\n")
    legacy_copy = tmp_path / "fitted_legacy.gmod"
    legacy_copy.write_text(difmap_written.read_text())

    models.label_fitted_model(difmap_written, hierarchy)
    legacy_stage1.label_fitted_model(legacy_copy, legacy_stage1.parse_master_model(MASTER))

    assert difmap_written.read_text() == legacy_copy.read_text()
    assert models.parse_master_model(difmap_written).groups == hierarchy.groups


def test_label_fitted_model_row_count_mismatch(tmp_path: Path) -> None:
    hierarchy = models.parse_master_model(MASTER)
    path = tmp_path / "short.gmod"
    path.write_text("1 0 0 0 1 0 0\n")
    with pytest.raises(models.ModelFormatError, match="contains 1 rows"):
        models.label_fitted_model(path, hierarchy)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("0.312v 0.0v 0.0v 0.8v 1.0 0.0 1 0 0\n", "0.312v 0.0 0.0 0.8 1.0 0.0 1 0 0\n"),
        ("0.041 1.9v 42.0v 0.0 1.0 0.0 0\n", "0.041v 1.9 42.0 0.0 1.0 0.0 0\n"),
        ("  1.5V  2.0V 3.0 \r\n", "  1.5v  2.0 3.0 \r\n"),
        ("0.1 0.2 0.3 0.4 0.5 0.6 1 15e9 -0.7v\n", "0.1v 0.2 0.3 0.4 0.5 0.6 1 15e9 0\n"),
        ("! comment line\n", "! comment line\n"),
    ],
)
def test_transform_model_line(line: str, expected: str, legacy_stage2) -> None:
    ours, _ = models.transform_model_line(line)
    theirs, _ = legacy_stage2.transform_model_line(line)
    assert ours == expected
    assert theirs == expected


def test_transform_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="Non-numeric field 2"):
        models.transform_model_line("0.1 abc 0.3\n")


def test_write_flux_only_model_matches_legacy(tmp_path: Path, legacy_stage2) -> None:
    ours = tmp_path / "ours.mod"
    theirs = tmp_path / "theirs.mod"
    counts = models.write_flux_only_model(MASTER, ours)
    legacy_counts = legacy_stage2.create_stage2_model(MASTER, theirs, overwrite=True)
    assert counts == legacy_counts
    assert counts[0] == 6
    assert ours.read_text() == theirs.read_text()
    assert models.model_component_fluxes(ours) == pytest.approx(
        [0.312, 0.041, 0.288, 0.101, 0.012, 0.045]
    )


def test_safe_label() -> None:
    assert models.safe_label(" A1 / core ") == "A1_core"
    with pytest.raises(ValueError):
        models.safe_label("***")
