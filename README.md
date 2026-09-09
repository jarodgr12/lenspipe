# lenspipe

DifMAP spectral pipeline for multi-epoch spectra of lensed radio sources. It
turns calibrated UV-FITS visits into per-image spectra, flux ratios, and
publication plots, and adds a local web console so the whole workflow runs
from one window.

The pipeline has three stages, each reading the previous stage's products from
a standard project directory:

| Stage | Reads | Does | Writes |
|------:|-------|------|--------|
| 1 | `inputs/<source>.gmod`, `inputs/<source>.<epoch>.uvfits` | DifMAP self-calibration and model fitting | `stage1/<source>.<epoch>/` calibrated UV data, fitted model, maps |
| 2 | Stage 1 | Fits the frozen model per channel or per IF (flux only), sharded across DifMAP processes | `stage2/<source>.<epoch>/` spectrum CSV, quick-look PNGs |
| 3 | Stage 2 | Power-law fits, flux ratios, R_cusp, multi-visit summaries | `stage3/<source>.<epoch>/<product>/` and `stage3/combined/` |

A separate CASA module (`casa/`) calibrates raw EVLA data and exports the
Stage 1 inputs.

## Install

On the machine that has DifMAP (and CASA, if you calibrate there), one line:

```bash
curl -LsSf https://raw.githubusercontent.com/jarodgr12/lenspipe/master/scripts/install.sh | bash
```

If raw.githubusercontent.com is unavailable (it returns 503 now and then),
the same script through the GitHub API, or a clone, does the same job:

```bash
curl -LsSf -H 'Accept: application/vnd.github.raw' \
  https://api.github.com/repos/jarodgr12/lenspipe/contents/scripts/install.sh | bash
# or
git clone --depth 1 https://github.com/jarodgr12/lenspipe.git && bash lenspipe/scripts/install.sh --from https://github.com/jarodgr12/lenspipe.git
```

That installs `uv` if needed, installs `lenspipe` from this repository with
its own private Python into `~/.local/bin`, and runs `lenspipe doctor`, which
reports Python, DifMAP, CASA, fonts, cores and memory, and says how to fix
anything missing. System Python and CASA are not touched. It needs `git` and
internet access once; runs are offline afterwards.

From a checkout, `bash scripts/install.sh` installs that checkout instead, and
`--from <url> --ref v2.0.1` pins a release. Developers can use
`uv sync --all-extras` and `uv run lenspipe`.

### Updating

```bash
lenspipe update
```

That reinstalls from the source the tool was installed from, so an install
from the repository picks up the current `master`, and runs `doctor`.
`lenspipe update --check` shows the source without changing anything.
`CHANGELOG.md` states for every release whether existing products need to be
re-run; the version that produced a product is recorded in its metadata.

## Quick start

```bash
lenspipe init /data/MG0414 --inputs /archive/MG0414/uvfits   # links the .uvfits and .gmod files, detects DifMAP and CASA, writes lenspipe.toml
lenspipe ui /data/MG0414                                       # run stages, watch jobs, browse results in the browser
```

`--inputs` symlinks the files (add `--copy` to copy). An existing legacy
project already has the right layout, so `lenspipe init /data/MG0414` inside
it is enough. From the shell, the stages are:

```bash
lenspipe stage1 /data/MG0414
lenspipe stage2 /data/MG0414 --mode channel
lenspipe stage3 /data/MG0414 --product channel
lenspipe run /data/MG0414 --stages 1,2,3     # the same, in order
lenspipe inventory /data/MG0414
```

## Validating against a legacy run

```bash
bash scripts/validate.sh /data/MG0414              # all epochs; add --epoch A for a quick first pass
```

The legacy project is not modified. The script creates `/data/MG0414-v2test`
with inputs linked from the legacy `inputs/`, runs `doctor`, Stage 1, Stage 2
with one shard, Stage 2 with the default shard count, and Stage 3, and after
each step runs `lenspipe compare` against the legacy products. It records
wall times, the speed-up, DifMAP's peak memory and the `memory_multiple` that
implies, and writes everything to `validation-report.txt` in the work
project. It exits 0 only when every compared product is identical or
numerically equivalent.

The same check is available by hand: `lenspipe compare LEGACY NEW` reports
every Stage 1 model, calibrated file and residual RMS, every Stage 2
spectrum CSV and its metadata, and every Stage 3 table as identical,
equivalent (within tolerance), different (naming the columns), or missing,
and exits with status 4 if anything differs. `--stages`, `--epoch` and
`--product` narrow it.

### The master model

Stage 1 needs a labelled DifMAP model. Every data row is preceded by two
comment lines naming the lensed image (`GROUP`) and the component:

```
! GROUP A1
! COMPONENT A1a
0.312v 0.0v 0.0v 0.8v 1.0 0.0 1 0 0
! COMPONENT A1b
0.041v 1.9v 42.0v 0.0 1.0 0.0 0
! GROUP A2
...
```

The first group is the flux-ratio reference. Group names `A1`, `A2`, `B` (or
whatever `stage3.rcusp_images` names) enable the R_cusp product.

## Configuration

Every tunable lives in `<project>/lenspipe.toml`; `lenspipe init` writes the
file with all defaults and comments. Command-line flags override the file for
one run, and the effective values are recorded in each product's metadata.
`lenspipe describe <project>` prints the effective configuration.

The defaults reproduce the legacy scripts exactly: the five-step self-cal
schedule, `mapsize 1024` with `uvw 0,-1,false`, 20 modelfit iterations per
channel with a 25 mas cell, RMS-weighted power-law fits at 15 GHz, and the
12 to 18 GHz plot frame.

Settings that did not exist before:

| Setting | Default | Meaning |
|---------|---------|---------|
| `stage2.shards` | `auto` | DifMAP processes per epoch. `auto` takes cores - 1 (max 8) and then caps by memory. Results are independent of the shard count. |
| `stage2.memory_fraction`, `memory_multiple` | 0.5, 3.0 | Memory budget for the shard cap: a fraction of physical RAM, shared across concurrent epochs, with each DifMAP estimated at `memory_multiple` times the UV-FITS size. The multiple is a placeholder until measured on a real epoch. |
| `run.epoch_workers` | 2 | Epochs processed concurrently in stages 1 and 2 |
| `run.plot_workers` | 4 | Processes writing Stage 3 per-visit figures |
| `stage3.figure_formats` | `["pdf","png"]` | Formats written for every Stage 3 figure; `["png"]` roughly halves plot time |
| `stage2.plot_spectrum`, `stage2.plot_error_bars` | true, true | Quick-look PNGs after each epoch, and whether they carry residual-RMS error bars |
| `stage3.plot_error_bars` | true | Error bars on every Stage 3 figure; off plots the points alone |
| `project.difmap.stream` | `pipe` | `pty` makes DifMAP line-buffer so progress arrives per fit |
| `stage3.rcusp_images` | `["A1","A2","B"]` | Group names for the cusp relation; `[]` disables it |
| `stage3.frequency_frame_ghz`, `frequency_ticks_ghz` | 11.7 to 18.3, 12..18 | Plot frame |
| `casa.*` | unset | Interpreter, script and observation file for `lenspipe calibrate` |

## The console

`lenspipe ui <project>` starts a local web app (NiceGUI) on
http://127.0.0.1:8080 with five pages: Project (inventory by epoch and stage),
Parameters (forms generated from the configuration schema, saved to
`lenspipe.toml`), Run (choose stages, epochs and options, preview the exact
command, submit), Jobs (queue, live progress and log tail, cancel, re-run) and
Results (figure gallery and tables).

Every console action is a job: a subprocess running `python -m lenspipe ...`
with its record under `<project>/.jobs/<id>/` (`job.json`, `log.txt`,
`progress.json`, `result.json`). `lenspipe jobs <project>` lists them from a
shell. When DifMAP lives on another machine, run the console there and reach
it through an SSH port forward:

```bash
ssh -L 8080:127.0.0.1:8080 workstation 'cd /data/MG0414 && lenspipe ui . --no-browser'
```

## Interrupted runs, log recovery and stale products

**Resume.** Stage 2 checkpoints per shard in a hidden work directory next to
its outputs. If a run is interrupted, re-running the same command reports the
interrupted run and stops; `lenspipe stage2 <project> --resume` continues it,
re-executing only the shards that did not finish. Resume refuses if the data,
model or Stage 2 settings changed in the meantime; `--overwrite` starts again.
The work directory is removed once the epoch completes.

**Log recovery.** `lenspipe stage2 <project> --recover-from-log --overwrite`
rebuilds the CSV, plots and metadata from an existing `*.stage2.difmap.log`
without running DifMAP. Logs from sharded runs are concatenated into that
file, so recovery works on them too.

**Verify.** Every product records the size, mtime and hash of the files it was
made from. `lenspipe verify <project>` compares them with the files as they
are now and reports each product as `fresh`, `stale` (its own inputs changed),
`upstream-stale` (an earlier stage is stale), `missing-input`, or `unknown`
(legacy products with no provenance). It exits with status 3 when anything is
stale. Hashes are only recomputed when size or mtime differ, so the check is
fast on large visits; `--hash` forces a full re-hash. The console's Project
page shows the same status as a badge on each product.

## CASA calibration

`casa/calibration.py` is a step-registry rewrite of the original
`scriptForCalibration.A.py`; observation-specific values come from a TOML file
such as `casa/observation.22A-388.1555.A.toml`. Its export step writes one
`<source>.<epoch>.uvfits` per epoch with the full channel layout Stage 1
expects. See `casa/README.md`. With `[casa]` set in `lenspipe.toml`,
`lenspipe calibrate <project> --steps 0-10` runs it as a job.

## Development

```bash
uv run pytest -q          # unit tests plus legacy-equivalence golden tests
uv run ruff check .
```

The golden tests run both the legacy scripts (kept under `legacy/`) and the
package against a fake DifMAP (`tests/fixtures/fake_difmap.py`) on a synthetic
project, and require identical Stage 1 models, byte-identical Stage 2 CSVs for
one and several shards, and numerically identical Stage 3 tables.

## Layout

```
src/lenspipe/
  config.py      pydantic schema for lenspipe.toml
  project.py     directory layout, naming, inventory
  models.py      .gmod parsing, GROUP/COMPONENT hierarchy, flux-only transform
  uvfits.py      frequencies and MJD from UV-FITS headers
  difmap/        command builders, streaming subprocess runner, log parser
  stage1.py  stage2.py  stage2_quicklook.py
  stage3/        io, fitting, combined, plotting, analysis (run_stage3)
  jobs.py        job records and runner used by the CLI and the console
  cli.py         typer command line
  ui/            NiceGUI console
casa/            CASA calibration module and observation config
legacy/          the original scripts, unchanged, used as the golden reference
tests/
```
