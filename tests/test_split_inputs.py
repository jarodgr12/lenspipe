"""2.4.19: stage2.split_inputs gives every DifMAP process only the spectral windows it fits.

DifMAP copies its whole input to scratch at start-up, so with N shards the calibrated
file was copied N times per epoch. The per-shard subset must carry exactly the data of
its IFs, at the frequencies they had, and must not change any Stage 2 number.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

import lenspipe.stage2 as stage2_module
from lenspipe.config import LenspipeConfig, Stage2Config
from lenspipe.difmap.scripts import stage2_commands
from lenspipe.progress import Reporter
from lenspipe.project import SCRATCH_PER_PROCESS, decide_shards
from lenspipe.stage1 import run_stage1
from lenspipe.stage2 import SHARD_INPUT_NAME, run_stage2
from lenspipe.uvfits import get_uvfits_frequencies
from lenspipe.uvfits_subset import if_span, read_layout, write_subset
from tests.conftest import write_synthetic_uvfits

GIB = 1 << 30


# ---------------------------------------------------------------------------
# The subset file


def test_subset_holds_exactly_the_chosen_ifs_and_the_parallel_hands(tmp_path: Path) -> None:
    source, subset_path = tmp_path / "full.uvf", tmp_path / "part.uvf"
    write_synthetic_uvfits(source, n_if=6, n_chan=4, n_pol=4, seed=3)
    info = write_subset(source, subset_path, 2, 4)
    assert (info.if_first, info.if_last, info.n_pol, info.channel_offset) == (2, 4, 2, 8)
    assert info.size_bytes == subset_path.stat().st_size < source.stat().st_size

    with fits.open(source) as full, fits.open(subset_path) as part:
        # Random-groups data axes: (group, dec, ra, IF, channel, polarisation, re/im/weight).
        np.testing.assert_array_equal(part[0].data.data, full[0].data.data[:, :, :, 2:5, :, :2, :])
        assert part[0].data.data.dtype == np.dtype(">f4")
        for index in range(len(full[0].data.parnames)):
            np.testing.assert_array_equal(part[0].data.par(index), full[0].data.par(index))
        assert part[0].header["CRVAL4"] == full[0].header["CRVAL4"]  # the frequency reference is kept
        np.testing.assert_array_equal(part["AIPS FQ"].data["IF FREQ"][0], full["AIPS FQ"].data["IF FREQ"][0][2:5])
        assert part["AIPS FQ"].data["CH WIDTH"].shape == (1, 3) and part["AIPS FQ"].header["NO_IF"] == 3
        assert any("LENSPIPE SUBSET IF 3-5 OF 6 CHANOFF 8" in str(line) for line in part[0].header["HISTORY"])
    # Every channel keeps the frequency it had in the whole file.
    assert get_uvfits_frequencies(subset_path) == get_uvfits_frequencies(source)[8:20]


def test_subset_of_a_single_polarisation_file_keeps_its_one_plane(tmp_path: Path) -> None:
    source, subset_path = tmp_path / "full.uvf", tmp_path / "part.uvf"
    write_synthetic_uvfits(source, n_if=3, n_chan=4, seed=5)
    layout, reason = read_layout(source)
    assert reason is None and layout is not None
    assert (layout.n_if, layout.channels_per_if, layout.n_pol, layout.cross_hands) == (3, 4, 1, False)
    assert layout.polarisation_fraction == 1.0
    info = write_subset(source, subset_path, 0, 0)
    with fits.open(source) as full, fits.open(subset_path) as part:
        assert info.n_pol == 1
        np.testing.assert_array_equal(part[0].data.data, full[0].data.data[:, :, :, 0:1])


def test_tables_other_than_fq_are_copied_unchanged(tmp_path: Path) -> None:
    source, subset_path = tmp_path / "full.uvf", tmp_path / "part.uvf"
    write_synthetic_uvfits(source, n_if=2, n_chan=4, seed=7)
    antennas = fits.BinTableHDU.from_columns(
        [fits.Column(name="ANNAME", format="8A", array=np.array(["EA01", "EA02", "EA03"])),
         fits.Column(name="STABXYZ", format="3D", array=np.arange(9.0).reshape(3, 3))],
        name="AIPS AN",
    )
    antennas.header["ARRNAM"] = "VLA"
    with fits.open(source) as hdul:
        fits.HDUList([hdul[0], hdul[1], antennas]).writeto(tmp_path / "with_an.uvf")
    write_subset(tmp_path / "with_an.uvf", subset_path, 1, 1)
    with fits.open(tmp_path / "with_an.uvf") as full, fits.open(subset_path) as part:
        assert [hdu.name for hdu in part] == [hdu.name for hdu in full]
        assert part["AIPS AN"].header == full["AIPS AN"].header
        assert part["AIPS AN"].data.tobytes() == full["AIPS AN"].data.tobytes()


def test_a_file_that_is_not_32_bit_random_groups_is_refused(tmp_path: Path) -> None:
    image = tmp_path / "image.fits"
    fits.PrimaryHDU(np.zeros((4, 4), dtype=">f4")).writeto(image)
    layout, reason = read_layout(image)
    assert layout is None and reason and "random-groups" in reason
    with pytest.raises(ValueError):
        write_subset(image, tmp_path / "out.uvf", 0, 0)
    assert not (tmp_path / "out.uvf").exists()


def test_if_span_covers_every_window_a_shard_touches() -> None:
    assert if_span([(1, 1, 1), (2, 2, 2)], channels_per_if=64) == (0, 0)
    assert if_span([(64, 64, 64), (65, 65, 65)], channels_per_if=64) == (0, 1)  # straddles a boundary
    assert if_span([(3, 131, 190)], channels_per_if=64) == (2, 2)  # an IF-mode fit: (fit index, first, last)


# ---------------------------------------------------------------------------
# The DifMAP script


def test_channel_numbers_are_relative_to_the_subset_and_names_are_not() -> None:
    kwargs = dict(
        stage2_model=Path("/p/m.mod"), work_directory=Path("/p/work"), mode="channel",
        config=Stage2Config(mode="channel"), fit_ranges=[(129, 129, 129), (130, 130, 130)],
    )
    whole = stage2_commands(calibrated_uvfits=Path("/p/c.uvf"), **kwargs)
    part = stage2_commands(calibrated_uvfits=Path("/w/input.uvf"), channel_offset=128, **kwargs)
    assert "select i,129,129\n" in whole and "select i,1,1\n" in part and "select i,2,2\n" in part
    assert "observe /w/input.uvf\n" in part
    # Fit indices and model names keep the numbering of the whole file.
    assert 'print "STAGE2_RMS", 129, imstat(rms)' in part and "/p/work/channel_00129.mod" in part
    assert stage2_commands(calibrated_uvfits=Path("/p/c.uvf"), channel_offset=0, **kwargs) == whole


def test_non_contiguous_fits_are_shifted_run_by_run() -> None:
    text = stage2_commands(
        calibrated_uvfits=Path("/w/input.uvf"), stage2_model=Path("/p/m.mod"), work_directory=Path("/p/work"),
        mode="if", config=Stage2Config(mode="if"), fit_ranges=[(3, 131, 190)],
        segments={3: ((131, 150), (160, 190))}, channel_offset=128,
    )
    assert "select i,3,22,32,62\n" in text


# ---------------------------------------------------------------------------
# The shard decision


def test_split_inputs_are_not_capped_by_disk_per_process() -> None:
    kwargs = dict(input_bytes=10 * GIB, cpu_count=24, total_memory_bytes=512 * GIB, memory_multiple=0.1)
    # 40 GiB free held 3 whole-file scratch copies; split, the whole epoch needs about 2.1 x 10 GiB.
    whole = decide_shards("auto", 3072, epoch_workers=1, free_disk_bytes=40 * GIB, **kwargs)
    split = decide_shards("auto", 3072, epoch_workers=1, free_disk_bytes=40 * GIB, split_input_bytes=10 * GIB, **kwargs)
    assert whole.shards == whole.disk_cap == 3 and not whole.split_inputs
    assert split.shards == split.cpu_cap == 8 and split.split_inputs and not split.disk_short
    assert split.scratch_bytes_per_process == int(10 * GIB * (1 + SCRATCH_PER_PROCESS)) // 8
    assert split.to_dict()["split_inputs"] is True and "split_inputs=yes" in split.describe()
    # Cross hands dropped: half the bytes, half the need.
    halved = decide_shards("auto", 3072, epoch_workers=1, free_disk_bytes=17 * GIB, split_input_bytes=5 * GIB, **kwargs)
    assert not halved.disk_short and halved.shards == 8


def test_split_inputs_that_do_not_fit_are_reported_not_hidden() -> None:
    decision = decide_shards(
        "auto", 3072, input_bytes=10 * GIB, cpu_count=24, total_memory_bytes=512 * GIB, memory_multiple=0.1,
        epoch_workers=2, free_disk_bytes=40 * GIB, split_input_bytes=10 * GIB,
    )
    # Two epochs at once share (40 - 5 reserve) GiB: 17.5 each, under the 21 GiB an epoch needs.
    assert decision.disk_short and decision.disk_cap == 0 and decision.shards >= 1


# ---------------------------------------------------------------------------
# Stage 2 end to end (fake DifMAP)


def _config(fake_difmap: Path, **stage2) -> LenspipeConfig:
    cfg = LenspipeConfig().with_overrides(project={"difmap": {"executable": str(fake_difmap)}})
    return cfg.with_overrides(stage2=stage2)


def _spectrum(project: Path) -> list[dict[str, str]]:
    path = project / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.spectrum.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_stage2_gives_each_process_its_own_windows_and_the_same_spectrum(
    project: Path, fake_difmap: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LENSPIPE_HOME", str(tmp_path / "home"))
    quiet = Reporter(stream=io.StringIO())
    whole_cfg = _config(fake_difmap, mode="channel", shards=2, split_inputs=False)
    run_stage1(project, whole_cfg, reporter=quiet, workers=1)
    assert all(r.ok for r in run_stage2(project, whole_cfg, reporter=quiet, workers=1, epochs={"A"}))
    whole_rows = _spectrum(project)
    # From here on a whole-file memory measurement exists; a split run must leave it alone.
    recorded: list[tuple[int | None, int]] = []
    monkeypatch.setattr(stage2_module, "load_calibration", lambda: object())
    monkeypatch.setattr(stage2_module, "record_measurement", lambda peak, size: recorded.append((peak, size)))
    calibrated = next((project / "stage1" / "full-band" / "MG0414.A").glob("*.cal.uvf"))

    observed: list[tuple[Path, int, bool]] = []
    real_run = stage2_module.run_difmap

    def spy(executable, commands, *args, **kwargs):
        target = Path(next(line for line in commands.splitlines() if line.startswith("observe ")).split(" ", 1)[1])
        with fits.open(target) as hdul:
            observed.append((target, int(hdul[0].header["NAXIS5"]), target.parent == Path(kwargs["cwd"])))
        return real_run(executable, commands, *args, **kwargs)

    monkeypatch.setattr(stage2_module, "run_difmap", spy)
    log = io.StringIO()
    split_cfg = _config(fake_difmap, mode="channel", shards=2)  # split_inputs is the default
    results = run_stage2(project, split_cfg, reporter=Reporter(stream=log), workers=1, epochs={"A"}, overwrite=True)
    assert results and all(r.ok for r in results), [r.message for r in results]

    # The synthetic visit has 2 IFs of 4 channels: two shards, one IF each, in the process's own directory.
    assert len(observed) == 2 and all(target.name == SHARD_INPUT_NAME and n_if == 1 and own for target, n_if, own in observed)
    assert len({target for target, _, _ in observed}) == 2
    assert not any(target.exists() for target, _, _ in observed)  # removed with the process directory
    assert "split inputs: each DifMAP process reads only the spectral windows it fits" in log.getvalue()

    assert _spectrum(project) == whole_rows  # same fluxes, errors, RMS and frequencies, row for row
    metadata = json.loads(
        (project / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.stage2.metadata.json").read_text()
    )
    execution = metadata["execution"]
    assert execution["shard_decision"]["split_inputs"] is True
    assert len(execution["shard_input_bytes"]) == 2  # one per process (these toy files are a single FITS block either way)
    assert execution["input_bytes"] == calibrated.stat().st_size
    # A process that loaded a share of the file over-states the memory per byte of a whole file,
    # so it must not replace a figure measured on one.
    assert recorded == []


def test_a_split_run_supplies_the_memory_figure_when_none_was_measured(
    project: Path, fake_difmap: Path, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LENSPIPE_HOME", str(tmp_path / "home"))
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, mode="channel", shards=2)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    recorded: list[tuple[int | None, int]] = []
    monkeypatch.setattr(stage2_module, "record_measurement", lambda peak, size: recorded.append((peak, size)))
    assert all(r.ok for r in run_stage2(project, cfg, reporter=quiet, workers=1, epochs={"A"}))
    # Without this a machine that only ever runs split would keep the 3.0 guess and its tiny shard cap.
    # The figure is taken against the file a process loaded, not against the whole calibrated file.
    metadata = json.loads(
        (project / "stage2" / "full-band" / "MG0414.A" / "MG0414.A.channel.stage2.metadata.json").read_text()
    )
    assert len(recorded) == 1 and recorded[0][0] and recorded[0][1] == max(metadata["execution"]["shard_input_bytes"])


def test_a_single_shard_of_a_file_without_cross_hands_reads_the_calibrated_file_itself(
    project: Path, fake_difmap: Path, monkeypatch
) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, mode="channel", shards=1)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    targets: list[str] = []
    real_run = stage2_module.run_difmap

    def spy(executable, commands, *args, **kwargs):
        targets.append(next(line for line in commands.splitlines() if line.startswith("observe ")))
        return real_run(executable, commands, *args, **kwargs)

    monkeypatch.setattr(stage2_module, "run_difmap", spy)
    assert all(r.ok for r in run_stage2(project, cfg, reporter=quiet, workers=1, epochs={"A"}))
    assert len(targets) == 1 and targets[0].endswith(".cal.uvf")  # nothing to leave out, so no copy is made


def test_a_failed_shard_does_not_leave_its_input_file_behind(project: Path, fake_difmap: Path, monkeypatch) -> None:
    quiet = Reporter(stream=io.StringIO())
    cfg = _config(fake_difmap, mode="channel", shards=2)
    run_stage1(project, cfg, reporter=quiet, workers=1)
    monkeypatch.setenv("FAKE_DIFMAP_FAIL_ON_CHANNEL", "6")  # a channel of the second shard
    results = run_stage2(project, cfg, reporter=quiet, workers=1, epochs={"A"})
    assert results and not results[0].ok
    work = project / "stage2" / "full-band" / "MG0414.A" / ".MG0414.A.channel.work"
    assert work.is_dir() and not list(work.rglob(SHARD_INPUT_NAME))


# ---------------------------------------------------------------------------
# Against a real DifMAP (opt-in: LENSPIPE_TEST_DIFMAP=/path/to/difmap)


@pytest.mark.skipif(not os.environ.get("LENSPIPE_TEST_DIFMAP"), reason="set LENSPIPE_TEST_DIFMAP to a real difmap")
def test_real_difmap_fits_are_identical_from_subset_files(tmp_path: Path) -> None:
    from tests.fixtures.synthetic_visit import MODEL_TEXT, write_visit

    difmap = os.environ["LENSPIPE_TEST_DIFMAP"]
    n_if, n_chan = 6, 8

    def run(cwd: Path, script: str) -> str:
        cwd.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([difmap], input=script, text=True, capture_output=True, cwd=cwd, check=False)
        return done.stdout + done.stderr

    raw, calibrated = tmp_path / "raw.uvf", tmp_path / "visit.cal.uvf"
    write_visit(raw, n_ant=10, n_int=30, n_if=n_if, n_chan=n_chan, flag_fraction=0.05)
    run(tmp_path / "wobs", f"observe {raw}\nselect i\nwobs {calibrated}\nquit\n")  # a DifMAP-written file, as Stage 1 makes
    assert calibrated.is_file()
    model = tmp_path / "stage2.mod"
    model.write_text(MODEL_TEXT, encoding="utf-8")
    config = Stage2Config(mode="channel")
    fit_ranges = [(c, c, c) for c in range(1, n_if * n_chan + 1)]

    def fits_of(label: str, uvfits: Path, block: list[tuple[int, int, int]], offset: int) -> dict[int, tuple[str, str]]:
        work = tmp_path / label
        work.mkdir(exist_ok=True)
        text = run(work / "cwd", stage2_commands(
            calibrated_uvfits=uvfits, stage2_model=model, fit_ranges=block, work_directory=work,
            mode="channel", config=config, channel_offset=offset,
        ))
        rms = {int(line.split()[1]): line.split()[2] for line in text.splitlines() if line.startswith("STAGE2_RMS")}
        found = {}
        for fit_index, _, _ in block:
            rows = (work / f"channel_{fit_index:05d}.mod").read_text().splitlines()
            found[fit_index] = ("\n".join(row for row in rows if not row.startswith("!")), rms[fit_index])
        return found

    whole = fits_of("whole", calibrated, fit_ranges, 0)
    assert len(whole) == n_if * n_chan
    for shards in (2, 3, 6):
        split: dict[int, tuple[str, str]] = {}
        per = len(fit_ranges) // shards
        for index in range(shards):
            block = fit_ranges[index * per : (index + 1) * per]
            first, last = if_span(block, n_chan)
            info = write_subset(calibrated, tmp_path / f"part_{shards}_{index}.uvf", first, last)
            assert info.n_pol == 2
            split.update(fits_of(f"split_{shards}_{index}", info.path, block, info.channel_offset))
        assert split == whole, f"{shards} shards: fitted models or RMS differ from the whole-file fits"


# ---------------------------------------------------------------------------
# Reachable from the command line and the console


def test_the_switch_reaches_the_command_line_from_the_run_page() -> None:
    from typer.testing import CliRunner

    from lenspipe.cli import app
    from lenspipe.ui.commands import RunRequest, build_steps

    assert "--no-split-inputs" in CliRunner().invoke(app, ["stage2", "--help"]).output.replace("\n", " ")
    assert Stage2Config().split_inputs is True

    def stage2_argv(value: bool | None) -> list[str]:
        request = RunRequest(root=Path("/p"), stages=[2], stage2_split_inputs=value)
        steps, _ = build_steps(request)
        return steps[0].argv

    assert "--no-split-inputs" in stage2_argv(False) and "--split-inputs" in stage2_argv(True)
    assert not any("split-inputs" in item for item in stage2_argv(None))  # unchanged: the config decides
