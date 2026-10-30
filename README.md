# lenspipe

DifMAP spectral pipeline for multi-epoch spectra of lensed radio sources. It
turns calibrated UV-FITS visits into per-image spectra, flux ratios, and
publication plots, and adds a local web console so the whole workflow runs
from one window.

The pipeline has three stages, each reading the previous stage's products from
a standard project directory:

| Stage | Reads | Does | Writes |
|------:|-------|------|--------|
| 1 | `inputs/<source>.gmod`, `inputs/<source>.<epoch>.uvfits` | DifMAP self-calibration (full-band or per-IF) and model fitting, then a second imaging pass that puts every epoch on one pixel grid | `stage1/<mode>/<source>.<epoch>/` calibrated UV data, fitted model (plus one model per IF in per-IF mode), native maps `*.native.cln.fits` / `*.native.resid.fits`, and common-grid maps `*.cln.fits` / `*.resid.fits` (all carry the restoring beam keywords) |
| 2 | Stage 1 | Fits the frozen model per channel or per IF (flux only), sharded across DifMAP processes | `stage2/<mode>/<source>.<epoch>/` spectrum CSV, quick-look PNGs |
| 3 | Stage 2 (and the Stage 1 maps) | Power-law fits, flux ratios, R_cusp, clean/residual map cutouts, multi-visit summaries | `stage3/<mode>/<source>.<epoch>/<product>/` and `stage3/<mode>/combined/` |

`<mode>` is the Stage 1 calibration mode, `full-band` or `per-if`; a project can hold both side by side and Stage 2 and Stage 3 read the tree of the configured mode (`stage1.calibration_mode`, or `--calibration-mode` for one run). Projects written before 2.1 are moved into `full-band/` the first time any stage or the console looks at them.

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

If `~/.local/bin` is not on your PATH the installer adds it to your shell
profile (`~/.bashrc` or `~/.zshrc`) and says so; **open a new terminal** before
the first `lenspipe` command, otherwise the shell reports
`Command 'lenspipe' not found`. Until then `~/.local/bin/lenspipe` works by
full path, and `lenspipe doctor` has a `path` check that names the problem.

From a checkout, `bash scripts/install.sh` installs that checkout instead, and
`--from <url> --ref v2.0.1` pins a release. Developers can use
`uv sync --all-extras` and `uv run lenspipe`.

### Updating

```bash
lenspipe update
```

That reinstalls from the source the tool was installed from, so an install
from the repository picks up the current `master`, and runs `doctor`.
`lenspipe update --check` shows the source and the newest release without
changing anything. The console does the same from the Project page: a
"Version" card says whether a newer release exists, "Update" runs the update
as a job, and "Restart console" starts the console again on the new version
(a running console keeps the old code until it is restarted; jobs are
separate processes and keep going).
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
| `stage2.shards` | `auto` | DifMAP processes per epoch. `auto` takes cores - 1 (max 8) and then caps by memory and by free disk. Results are independent of the shard count. |
| `stage2.scratch_dir` | unset | Where DifMAP processes run. DifMAP streams its input into a hidden scratch file in its working directory (about the input size per process), so a run needs roughly shards x concurrent epochs x input size there. Unset means the Stage 2 work directory inside the project; point it at a large or fast disk otherwise. Every DifMAP process gets its own sub-directory there (`shard_NN/`), because DifMAP creates the scratch file by a fixed name and a macOS build does so with a check-then-create race. `lenspipe doctor` reports the requirement against the free space. |
| `stage2.memory_fraction`, `memory_multiple` | 0.5, `auto` | Memory budget for the shard cap: a fraction of physical RAM, shared across concurrent epochs, with each DifMAP estimated at `memory_multiple` times the UV-FITS size. `auto` learns the multiple from the peak memory of earlier runs on this machine (3.0 until then); a number pins it. |
| `run.epoch_workers` | 2 | Epochs processed concurrently in stages 1 and 2; the automatic shard count shares the spare cores between them |
| `run.nice` | 10 | Scheduling priority for jobs and DifMAP (0 normal, 19 lowest). Keeps the console responsive under load |
| `run.plot_workers` | 4 | Processes writing Stage 3 per-visit figures |
| `stage3.figure_formats` | `["pdf","png"]` | Formats written for every Stage 3 figure; `["png"]` roughly halves plot time |
| `stage2.plot_spectrum`, `stage2.plot_error_bars` | true, true | Quick-look PNGs after each epoch, and whether they carry residual-RMS error bars |
| `stage1.selfcal` gscale | off | A fifth element `true` on a schedule entry, `[amplitude, float_amplitude, solint_min, modelfit_iterations, true]`, runs DifMAP's `gscale` (one overall amplitude correction per telescope, `dofloat` = float_amplitude) and another `modelfit` at the end of that step, after its selfcal and modelfit. On the Parameters page tick the step's `gscale` switch |
| `stage1.calibration_mode` | `full-band` | `full-band` runs the self-cal schedule once on the whole band against the master model (the legacy v1.1 sequence). `per-if` (legacy v1.2.2) runs it once per IF: each IF selects its inner channels, loads the master model with the spectral-index column fixed at 0, runs the schedule with amplitude self-cal steps replaced by `gscale`, and saves `<prefix>.ifNNN.gmod`; then the full band is selected, the original master model reloaded and the final model fitted once with no further self-cal. The per-IF models are independent starting fits, not chained. CLI `--calibration-mode`; Run page select "Calibration mode" |
| `stage1.per_if.if_count`, `channels_per_if`, `edge_channels`, `exclude_channels` | 48, 64, 0, unset | Channel layout of the per-IF solves: IF *n* selects channels `(n-1)*channels_per_if + edge_channels + 1` to `n*channels_per_if - edge_channels` (1-based), minus any `exclude_channels` in the shared syntax (`"spw0-47:0-7; spw7"`, CASA numbering); gaps go on one DifMAP `select`, an IF left with nothing is skipped and gets no per-IF model. The final full-band fit still uses every channel. CLI `--if-count`, `--channels-per-if`, `--edge-channels`, `--per-if-exclude-channels`; Run page fields next to the mode |
| `stage1.final_if_selfcal.exclude_channels` | unset | Channels left out of the optional final IF-by-IF pass, on top of `edge_channels`, in the same form as the Stage 3 exclusions: `"spw0-47:0-7; spw3:60-63; spw7"` (CASA spectral window from 0, channels from 0 inside it; `spw0-47:...` applies to a run of windows; `spw7` drops the whole window; a plain number is a DifMAP channel from 1). Gaps inside an IF become one DifMAP `select` with several ranges; an IF left with nothing is skipped. Replaces the 2.1.1 include form `channels`, which is converted automatically when read. CLI `--final-if-exclude-channels`; Run page field beside the "Final per-IF self-cal" switch; recorded per IF in the Stage 1 metadata |
| `stage2.exclude_channels` | unset | Channels left out of the Stage 2 fits, same syntax. In IF mode each window is fitted over its channels minus the symmetric edges and these exclusions (gaps go on one `select`; a window left with nothing is not fitted); in channel mode the named channels are not fitted. The product tag gains `_x_<spec>` (e.g. `if64_edge3_x_spw7`) so results with different exclusions sit side by side. CLI `--exclude-channels`; Run page Stage 2 Advanced field with examples |
| `stage1.reimage` | `true` | Whether Stage 1 runs the second, common-grid imaging pass at all. Off, each epoch keeps only the native maps from its calibration pass (`*.native.cln.fits`, `*.native.resid.fits`), Stage 3 map figures use those, and the all-epochs pages are on each epoch's own cell. CLI `--reimage/--no-reimage`; Run page switch "Common-grid imaging pass". `--reimage-only` runs the pass regardless |
| `stage1.cell_mas` | unset | Cell of the second imaging pass. Stage 1 first images each epoch on DifMAP's native cell (`*.native.*.fits`), then re-images every calibrated epoch of the mode on one grid: this cell, or the finest native cell among them. `lenspipe stage1 --reimage-only` repeats only that pass (also a Run page switch); the metadata records both geometries and both residual RMS values |
| `stage2.unflag` | false | Run `unflag *` on the calibrated file before the channel fits. Off keeps the flags Stage 1 wrote (Stage 1 has its own `unflag`, on by default). Also `--unflag/--no-unflag` per run |
| `stage3.plot_error_bars` | true | Error bars on every Stage 3 figure; off plots the points alone |
| `stage3.point_opacity`, `error_bar_opacity` | 1.0, 1.0 | Opacity of the data markers and of the error bars on every Stage 3 figure, set separately (0 transparent to 1 opaque); e.g. faint bars under solid points. CLI `--point-opacity`, `--error-bar-opacity`; sliders on the Run page |
| `project.difmap.stream` | `pipe` | `pty` makes DifMAP line-buffer so progress arrives per fit |
| `stage3.rcusp_images` | `["A1","A2","B"]` | The three images of the cusp relation, in the order (A1, A2, B) with A2 the middle, opposite-parity image. The names must be GROUP labels of the master model, so a source whose images are called A, B, C, D needs e.g. `["A","B","C"]`. When they do not match, Stage 3 logs why R_cusp was skipped, `lenspipe doctor` warns, and the Results page shows the reason. `[]` disables it |
| fit reports | always | `fits/<prefix>.fits.txt` per visit and `tables/<stem>.combined_fits.txt` per combined product: every spectral and flux-ratio fit written out in full, with parameters, 1-sigma errors, the 2x2 covariance matrix (parameter order and units stated), correlation and goodness of fit; the Results page shows them as text panels |
| per-visit scatter figures | always | `<prefix>.spectra_scatter.<fmt>` and `<prefix>.flux_ratio_scatter.<fmt>` plus `fits/<prefix>.scatter.csv`: the channel-to-channel scatter of each image's flux about its power-law fit and of each flux ratio about its weighted mean, as σ_w and σ_u in per cent (the combined product's definitions, applied across channels within one visit) |
| `stage3.exclude_channels`, `exclude_epoch_channels` | unset | Channels left out of the Stage 3 fits. Items separated by `;`: `spw3:0-3,60-63` (CASA spectral window 3, numbered from 0, channels 0-3 and 60-63 inside it, from 0), `spw0-47:0-7` (the same channels in a run of windows), `spw7` or `spw7-9` (whole windows), or plain Stage 2 fit indices `1-4,61-64` (from 1). Per-epoch: `["E:spw14:0-63; spw15", "F:1-4"]`. Window items are resolved per product from the Stage 2 metadata; on an IF product only whole windows can be excluded. CLI `--exclude-channels`, `--exclude-epoch-channels` (repeatable); Run page "Exclude channels" fields. `lenspipe stage3 --dry-run` shows the resolved exclusions without writing anything |
| `stage3.images.*` | enabled, image centre, 2 arcsec, `viridis`, 99.5, 99.5, 0 | Clean and residual map cutouts drawn from the Stage 1 FITS images: `<prefix>.image_clean.<fmt>` and `<prefix>.image_residual.<fmt>` per visit (just the map, axes and colour bar on white, trimmed to the content), and `<source>.<product>.images_clean_all_epochs.<fmt>` plus `..._residual_all_epochs.<fmt>` in the combined product (portrait, two visits per row, six per page; `_p2`, `_p3` for more). Axes are offsets from the cutout centre in arcsec, east to the left. `center` is unset for the image centre, `"ra,dec"` in degrees, or sexagesimal; `size_arcsec` is a square or `[width, height]`; `cmap`, `pmax` (clean top percentile), `residual_pmax` and `vmin` set the colour scales. On the all-epochs pages every panel shares the colour scale of `reference_epoch` (default: the first visit) and the pages are saved with transparent backgrounds (`transparent_all_epochs`). Per run: `--image-center`, `--image-size`, `--image-cmap`, `--image-pmax`, `--image-residual-pmax`, `--image-reference-epoch`, `--no-images`, also on the Run page |
| `stage3.frequency_frame_ghz`, `frequency_ticks_ghz` | 11.7 to 18.3, 12..18 | Plot frame |
| `casa.*` | unset | Interpreter, script and observation file for `lenspipe calibrate` |

## The console

`lenspipe ui` starts a local web app (NiceGUI) on http://127.0.0.1:8080. It
opens the last project used, or the one given as an argument, and the Project
page switches between projects. Five pages: Project (inventory by epoch and stage),
Parameters (forms generated from the configuration schema, saved to
`lenspipe.toml`), Run (choose stages, epochs and options, preview the exact
command, submit), Jobs (queue, live progress and log tail, cancel, re-run,
resume an interrupted Stage 2) and Results (figure gallery and tables). The
Run page's "Advanced" groups hold the per-run overrides that the stage
commands accept: the Stage 1 final per-IF self-cal; Stage 2 edge channels,
modelfit iterations, unflag, kept models and quick-look toggles; Stage 3 fit
method, reference frequency, channel exclusions, annotations, error bars and
figure formats. A value only appears in the command when it differs from
`lenspipe.toml`. For Stage 3 products the Results page
also shows interactive versions of the spectra, flux ratios and combined
series; hovering a point gives its fit index (DifMAP numbering, from 1) and
its spectral window and channel (CASA numbering, both from 0).

### Runs: several parameter sets side by side

A run is a named parameter set with its own outputs: `runs/<label>/` inside
the project, holding a copy of `lenspipe.toml` frozen when the run is created,
an `inputs` link to the project's inputs, and its own `stage1/`, `stage2/` and
`stage3/` trees laid out exactly like the project's. That is what makes an
overnight queue safe: set the Parameters page up, type a label in the Run
page's "Run label" field and submit; change the parameters, type another
label, submit again. Each job reads its run's frozen file, not whatever
`lenspipe.toml` says when it finally starts, and writes under its own label,
so the morning's results can be compared instead of overwritten. The Run page
shows whether an existing run's frozen parameters still match the Parameters
page (with the diff) and can replace the frozen copy on request; the Project
page lists every run with its product counts; the Results page has a "Run"
select. Without a label, jobs behave as before: the project's own trees, and
`lenspipe.toml` read at job start.

From a shell: `--run <label>` on `stage1`, `stage2`, `stage3` and `run`
creates the run on first use and works inside it; `lenspipe runs <project>`
lists runs, `--create <label>` freezes the current parameters into a new one,
`--diff <label>` shows how the project's parameters have drifted from a run's,
`--refreeze <label>` replaces the frozen copy. A run root is an ordinary
project root, so `lenspipe stage2 <project>/runs/<label>` and
`lenspipe compare` between two runs work too. Runs cannot be nested.

Stop the console with `lenspipe stop <project>` (or `lenspipe stop --port 8080`),
which works even if the terminal that started it has gone. Running jobs are
left alone unless you add `--with-jobs`; `lenspipe stop --list` shows what is
running.

Every console action is a job: a subprocess running `python -m lenspipe ...`
with its record under `<project>/.jobs/<id>/` (`job.json`, `log.txt`,
`progress.json`, `result.json`, plus `lenspipe.toml`, a copy of the config
the job was queued against, and `effective_config.toml`, the complete
parameters the stage used after every command-line override, written when
the job starts). The Jobs page shows that parameter set under each job. `lenspipe jobs <project>` lists them from a
shell. When DifMAP lives on another machine, run the console there and reach
it through an SSH port forward:

```bash
ssh -L 8080:127.0.0.1:8080 workstation 'cd /data/MG0414 && lenspipe ui . --no-browser'
```

## Interrupted runs, log recovery and stale products

**Resume.** Stage 2 checkpoints per shard in a hidden work directory next to
its outputs (DifMAP also runs there unless `stage2.scratch_dir` says
otherwise). If a run is interrupted, re-running the same command reports the
interrupted run and stops; `lenspipe stage2 <project> --resume` continues it,
re-executing only the shards that did not finish. Resume refuses if the data,
model or Stage 2 settings changed in the meantime; `--overwrite` starts again.
The work directory is removed once the epoch completes. In the console, a
failed or cancelled Stage 2 job shows a Resume button on the Jobs page that
submits the same command with `--resume`.

**Log recovery.** `lenspipe stage2 <project> --recover-from-log --overwrite`
rebuilds the CSV, plots and metadata from an existing `*.stage2.difmap.log`
without running DifMAP. Logs from sharded runs are concatenated into that
file, so recovery works on them too.

**Flags.** The Project page's "Flags" panel, or `lenspipe flags <project>`, reads the weights of every calibrated
UV-FITS file (or `--raw` for `inputs/`, `--file` for any file) and reports,
per spectral window, the unflagged fraction and whether the window is
fully flagged, in both DifMAP (IF from 1) and CASA (spw from 0) numbering.
Weights at or below zero are flags, which is exactly what DifMAP sees, so a
Stage 2 fit that reported "no unflagged visibilities" for an IF can be
checked against the data. `--verbose` lists every window and any fully
flagged channels inside it; `--json` is machine-readable; exit status 3
means at least one window is fully flagged. A multi-gigabyte visit is one
sequential read.

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
  runs.py        runs: frozen parameter sets with their own output trees (runs/<label>/)
  jobs.py        job records and runner used by the CLI and the console
  cli.py         typer command line
  ui/            NiceGUI console
casa/            CASA calibration module and observation config
legacy/          the original scripts, unchanged, used as the golden reference
tests/
```
