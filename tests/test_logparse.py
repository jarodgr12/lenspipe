"""DifMAP log parsing agrees with the legacy Stage 2 parser, including split rows."""

from __future__ import annotations

import pytest

from lenspipe.difmap import logparse
from tests.conftest import FIXTURES

SAMPLE = (FIXTURES / "stage2_sample.log").read_text()


def test_tagged_values() -> None:
    assert logparse.tagged_values(SAMPLE, "STAGE2_RMS") == {
        1: 0.0010234,
        2: 1.0301e-03,
        3: 0.0010111,
    }


def test_tagged_values_matches_legacy(legacy_stage2) -> None:
    assert logparse.tagged_values(SAMPLE, "STAGE2_RMS") == legacy_stage2.tagged_rms_values(SAMPLE)


def test_tagged_values_duplicate_raises() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        logparse.tagged_values(SAMPLE + "STAGE2_RMS 3 0.5\n", "STAGE2_RMS")


def test_measurements_including_split_row() -> None:
    measurements = logparse.modelfit_flux_measurements_by_fit(SAMPLE, expected_component_count=3)
    assert sorted(measurements) == [1, 2, 3]
    assert measurements[2] == [(0.3099, 0.0030), (0.0410, 0.00081), (0.2880, 0.0028)]


def test_measurements_match_legacy(legacy_stage2) -> None:
    ours = logparse.modelfit_flux_measurements_by_fit(SAMPLE, 3)
    theirs = legacy_stage2.modelfit_flux_measurements_by_fit(SAMPLE, 3)
    assert ours == theirs


def test_measurements_wrong_component_count() -> None:
    with pytest.raises(ValueError, match="contains 3 component rows; hierarchy defines 4"):
        logparse.modelfit_flux_measurements_by_fit(SAMPLE, expected_component_count=4)


def test_marker_without_table_raises() -> None:
    text = "STAGE2_RMS 7 0.001\n"
    with pytest.raises(ValueError, match="without a preceding"):
        logparse.modelfit_flux_measurements_by_fit(text, 1)


HEADER_SPLIT = (FIXTURES / "stage2_header_split.log").read_text()


def test_real_difmap_log_with_warning_splitting_the_table_rule() -> None:
    """Excerpt from a real 2.5q run (1555 epoch B, fit 3033): the stderr warning landed
    between the '#' and the dashes of the table rule. Legacy parsing failed here."""
    measurements = logparse.modelfit_flux_measurements_by_fit(HEADER_SPLIT, expected_component_count=5)
    assert sorted(measurements) == [3032, 3033]
    assert measurements[3033][0] == (2.9191e-02, 1.2e-05)
    assert measurements[3033][4] == (2.7474e-03, 7.4e-06)
    assert logparse.tagged_values(HEADER_SPLIT, "STAGE2_RMS") == {3032: 0.000534027, 3033: 0.000533389}


@pytest.mark.parametrize(
    "split_at",
    [
        "#|------------------  --------",       # the rule (fit 3033 in the real log)
        " 1.3073e-02 7.3|e-06  -3.6809e-01",  # inside an exponent (fit 3045)
        " 1.3073e-02 7.3e-06  -3.68|09e-01",  # inside a mantissa
        "STAGE2_RMS 3045 0.00054|5831",        # inside the marker value
        "Iteration 20: Reduced Chi-|squared",  # somewhere harmless
    ],
)
def test_repair_restores_a_line_split_by_the_warning(split_at: str) -> None:
    whole = split_at.replace("|", "")
    left, right = split_at.split("|")
    merged = (
        f"{left}Your choice of large map pixels excluded 0.27% of the data.\n"
        f" The y-axis pixel size should ideally be below 24.96 milli-arcsec\n"
        f"{right}\n"
    )
    assert logparse.repair_merged_warnings(merged) == whole + "\n"


def test_repair_handles_warning_at_line_boundaries_and_leaves_clean_logs_alone() -> None:
    assert logparse.repair_merged_warnings(SAMPLE.replace("Your choice", "Nope")) == SAMPLE.replace("Your choice", "Nope")
    at_start = "row one\nYour choice of large map pixels excluded 1% of the data.\n The y-axis pixel size should ideally be below 25 mas\nrow two\n"
    assert logparse.repair_merged_warnings(at_start) == "row one\nrow two\n"
    assert logparse.repair_merged_warnings("tail Your choice of large map pixels excluded 1%") == "tail "


def test_legacy_parser_fails_on_the_split_rule(legacy_stage2) -> None:
    with pytest.raises(ValueError, match="Fit 3033: RMS marker encountered without a preceding"):
        legacy_stage2.modelfit_flux_measurements_by_fit(HEADER_SPLIT, 5)


def test_bare_dash_lines_elsewhere_do_not_start_a_table() -> None:
    text = (
        " IF  Channel    Frequency\n"
        " ------------------------------------------------------------- (Hz)\n"
        "------------------  -------------------\n"
        "1.0 0.1 0 0 0 0 point 0 0 0\n"
        "STAGE2_RMS 1 0.001\n"
    )
    with pytest.raises(ValueError, match="without a preceding"):
        logparse.modelfit_flux_measurements_by_fit(text, 1)


def test_stderr_prefixed_lines_are_ignored() -> None:
    text = SAMPLE.replace(
        "#-------------------------------------------------------------------------------\n 0.3102",
        "#-------------------------------------------------------------------------------\n"
        "! [stderr] Your choice of large map pixels excluded 1% of the data.\n 0.3102",
    )
    assert sorted(logparse.modelfit_flux_measurements_by_fit(text, 3)) == [1, 2, 3]


def test_stage1_scalar_and_fallback() -> None:
    text = "junk\nSTAGE1_RMS 0.00042\nWriting map\n"
    assert logparse.tagged_scalar(text, "STAGE1_RMS") == 0.00042
    assert logparse.tagged_scalar("nothing", "STAGE1_RMS") is None
    assert logparse.extract_last_numeric("a\n1.5e-3\nb\n") == 1.5e-3
    assert logparse.extract_last_numeric("a\nb\n") is None
