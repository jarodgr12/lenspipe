# Changelog

Each entry says whether existing products need re-running. The `lenspipe`
version that made a product is recorded in its metadata JSON.

## 2.3.0 — 2026-10-27

**Re-run needed:** no, unless you set the new option. A `lenspipe.toml`
with the old `stage1.final_if_selfcal.channels` include form keeps
working: it is converted to the exclude form when read.

- Added: `stage2.exclude_channels`, per-spectral-window channel exclusions
  for Stage 2 in either mode, in the syntax Stage 3 already uses:
  `"spw3:0-3,60-63; spw7"` (CASA spw from 0, channels from 0 inside it;
  `spw7` the whole window; a plain number a DifMAP channel from 1). IF mode
  fits each window over its channels minus the symmetric edges and these
  exclusions, with gaps on one DifMAP `select`; a window left with nothing
  is not fitted and is listed in the metadata. Channel mode skips the named
  channels. The product tag gains `_x_<spec>` (`if64_edge3_x_spw7`), the
  Stage 2 metadata records `channel_exclusion`, and the resume fingerprint
  includes the spec. CLI `--exclude-channels`; Run page Stage 2 Advanced
  field with worked examples.
- Changed: the Stage 1 final-IF channel setting is now what to leave out,
  `stage1.final_if_selfcal.exclude_channels`, same syntax, instead of the
  2.1.1 include form (`"1:3-62"`, DifMAP numbering). Old configs are
  converted on load; CLI flag is `--final-if-exclude-channels`; the Run
  page field and examples follow. One exclusion syntax now serves Stage 1,
  Stage 2 and Stage 3.

## 2.2.6 — 2026-10-27

**Re-run needed:** no.

- Added: the flag report in the console. The Project page has a "Flags"
  panel: choose calibrated or raw files and optionally epochs, press
  "Check flags", and get one card per file with the fully flagged windows
  in red (IF and spw numbering), the partially flagged ones noted, and an
  "Every window" switch for the full per-IF table. Same report as
  `lenspipe flags`.

## 2.2.5 — 2026-10-27

**Re-run needed:** no.

- Added: `lenspipe flags`. Reads the weights of the calibrated UV-FITS
  files (or `--raw` inputs, or any `--file`) and reports per spectral
  window the unflagged fraction and whether it is fully flagged, in DifMAP
  and CASA numbering, with `--verbose` per-channel detail and `--json`.
  Exit status 3 when a window is fully flagged. Written for Stage 2 runs
  that report "no unflagged visibilities" for an IF: the data decides
  whether that window is really empty.

## 2.2.4 — 2026-10-27

**Re-run needed:** no.

- Fixed: after a successful install on a fresh Linux account, `lenspipe`
  was "command not found" in the user's shell. `uv tool install` puts the
  command in `~/.local/bin`; the installer had added that directory to its
  own PATH, so its final `lenspipe doctor` worked while the user's shell
  never saw it. The installer now checks the user's login shell, appends
  the PATH line to `~/.bashrc` / `~/.zshrc` when missing, and prints an
  IMPORTANT notice to open a new terminal (with the full-path fallback).
  `lenspipe doctor` gained a `path` check that warns when the installed
  command's directory is not on the PATH.

## 2.2.3 — 2026-10-26

**Re-run needed:** Stage 3, for the new combined figures.

- Added: the combined product now carries the scatter figures too.
  `<source>.<product>.average_spectrum_scatter` and
  `.average_flux_ratio_scatter` apply the per-epoch definitions to the
  all-visit average series: the average spectrum divided by its power-law
  fit, the average flux ratios by their weighted mean over channels, σ_w
  and σ_u in the margin. Numbers in `tables/<stem>.average_scatter.csv`,
  in the combined fits table (products `average_spectrum_scatter`,
  `average_flux_ratio_scatter`) and under `combined_fits.average_scatter`
  in the combined metadata.

## 2.2.2 — 2026-10-26

**Re-run needed:** Stage 3, if you want the new opacities applied.

- Added: `stage3.point_opacity` and `stage3.error_bar_opacity` (0 to 1,
  default 1), the opacity of the data markers and of the error bars on
  every Stage 3 figure, set independently, so error bars can be drawn
  faint under solid points or the reverse. CLI `--point-opacity`,
  `--error-bar-opacity`; two sliders on the Run page under Stage 3
  Advanced. Recorded in the per-visit `plot_style` metadata.

## 2.2.1 — 2026-10-26

**Re-run needed:** no.

- Fixed: a command run without a project path from a directory that is not
  a project (for example a CASA data directory) used that directory as the
  project and failed later with "Stage 2 directory not found for
  calibration mode full-band: <that dir>/stage2/full-band". The project
  root is now checked first: a directory with no `lenspipe.toml`, `inputs/`
  or stage tree is refused with a message naming it and how to pass the
  project path; a path inside a project (`.../stage2/full-band/X`) resolves
  to that project and says so; a path that does not exist is an error.
  `lenspipe init` still accepts a new directory, and `lenspipe doctor`
  reports on exactly the path it is given. The console's "Open" field
  resolves a sub-directory to its project the same way.

## 2.2.0 — 2026-10-25

**Re-run needed:** Stage 2 on macOS for any epoch that reported missing
channels (see below); Stage 3 to get the new per-epoch scatter figures.

- Fixed: random channels failing in Stage 2 on macOS ("tagged RMS
  measurements do not match the requested fits", DifMAP reporting
  "no unflagged visibilities" for channels that fit fine on another
  run). All shards of an epoch ran DifMAP in one directory. DifMAP writes
  its scratch copy of the input there under the fixed name `uvdata.scr`;
  a build without `_POSIX_C_SOURCE` (macOS clang leaves it undefined,
  Linux gcc defines it) creates that file with a check-then-create
  sequence, so processes starting together could share one scratch inode
  and corrupt each other's visibilities. Each DifMAP process now runs in
  its own `shard_NN/` sub-directory (also under `stage2.scratch_dir`),
  removed when the shard finishes. The fake DifMAP models the scratch
  file and fails on a shared directory, and Stage 2 is tested with four
  shards and two concurrent epochs. Not a memory problem: DifMAP's peak
  RSS in those runs was 50 MB.
- Added: per-epoch scatter. Each visit now gets `<prefix>.spectra_scatter`
  (each image's flux divided by that epoch's fitted power law, one panel
  per image) and `<prefix>.flux_ratio_scatter` (each ratio divided by its
  inverse-variance weighted mean over channels), with σ_w (sample std,
  ddof=1, of the normalised values) and σ_u (RMS about 1 of the values
  normalised by their plain mean) in the margin: the same two definitions
  the combined product uses across visits, here applied across channels.
  Numbers are in `fits/<prefix>.scatter.csv` (spectra also carry the
  slope-included scatter about the weighted mean) and in the `scatter`
  block of the epoch metadata.
- Added: Stage 3 channel exclusions can name spectral windows.
  `stage3.exclude_channels` and `exclude_epoch_channels` accept
  `spw<N>:<channels>` (CASA spectral window N from 0, channels from 0
  inside it), `spw<N>` for a whole window, or Stage 2 fit indices as
  before; items are separated by `;`, e.g. `"spw3:0-3,60-63; spw7; 1-4"`
  and `["E:spw14:0-63; spw15"]`. Window items are resolved per product
  from the Stage 2 metadata (channel products map to fit indices, IF
  products to the window's fit; a channel sub-selection on an IF product
  is refused). The metadata records the resolved fit indices and
  `spw N ch a-b` labels. Run page fields and CLI help updated;
  plain-index output tags are unchanged, spw specs get tags such as
  `exclude_spw3_0-3_60-63`.
- Added: `lenspipe stage3 --dry-run` (and the Run page dry run now
  includes Stage 3): lists each visit with its rows after exclusions, the
  resolved exclusions, and the per-visit and combined output directories;
  writes nothing.

## 2.1.4 — 2026-10-23

**Re-run needed:** no. A Stage 2 run that failed with "tagged RMS
measurements do not match the requested fits (requested N, found 0)"
can be finished with `lenspipe stage2 <project> --resume`; the kept
DifMAP output is re-parsed.

- Fixed: with `project.difmap.stream = "pty"` DifMAP sees a terminal and
  prints its `0>` prompt without a newline before reading each command,
  so the next output line lands in the log as `0>STAGE2_RMS 1 7.6e-05`
  (and `0>0>Log file ... closed` at the end). The log parsers required
  the marker at the start of the line and found nothing, so Stage 2
  reported every fit missing although DifMAP had completed them all.
  Every parser now strips leading `N>` prompts; this also covers the
  Stage 1 residual RMS, which silently fell back to a guess in pty mode.
  Regression test on the first shard of the real macOS log, and the fake
  DifMAP now prints prompts on a terminal so pty mode is exercised end
  to end.

## 2.1.3 — 2026-10-22

**Re-run needed:** Stage 3, to regenerate the per-visit map figures.

- Changed: the per-visit clean and residual map figures
  (`<prefix>.image_clean`, `<prefix>.image_residual`) are no longer A4
  pages with a caption. Each is just the map, its axes and colour bar on
  a plain white background, trimmed to the content. The combined
  all-visits pages are unchanged (A4, transparent, shared colour scale).

## 2.1.2 — 2026-10-20

**Re-run needed:** no.

- Fixed: the Run page's Stage 3 "Stage 2 product" select listed only the
  product implied by the current Stage 2 settings (typically `channel`),
  so an IF product from an earlier run could not be picked. It now always
  offers both the channel and the IF tag for the configured Stage 2
  settings, plus every product on disk under the selected calibration
  mode's `stage2/<mode>/` tree, and refreshes when the mode changes. A
  hand-typed tag is kept.

## 2.1.1 — 2026-10-14

**Re-run needed:** no, unless you set the new option.

- Added: `stage1.final_if_selfcal.channels`, a per-IF channel selection
  for the final IF-by-IF self-cal pass that can differ between IFs:
  `"1:3-62; 2:5-60,62; 7-12:1-64"` (IF or IF range, colon, channels
  within that IF, 1-based, Stage 2 syntax; unlisted IFs keep
  `edge_channels`). Each IF's picks go on one DifMAP `select` line, so a
  gap inside an IF is fine. CLI `--final-if-channels`, a field next to the
  "Final per-IF self-cal" switch on the Run page, and the Parameters page.
  The Stage 1 metadata lists the selected runs and channel count per IF.

## 2.1.0 — 2026-10-13

**Re-run needed:** Stage 1 for epochs you want on the common grid or in
per-IF mode; otherwise no. Existing outputs are kept: the first time any
stage or the console opens a project it moves `stage1/<prefix>`,
`stage2/<prefix>`, `stage3/<prefix>` and `stage3/combined` into
`.../full-band/`, rename only.

- Added: Stage 1 calibration modes, following the collaborator's Stage 1
  v1.2.2 script. `stage1.calibration_mode = "per-if"` runs the self-cal
  schedule once per IF: each IF selects its inner channels
  (`[stage1.per_if]`: `if_count`, `channels_per_if`, `edge_channels`),
  loads the master model with the spectral-index column fixed at 0, runs
  the schedule with amplitude self-cal steps issued as `gscale`, and saves
  that IF's model as `<prefix>.ifNNN.gmod` (labelled like the final model);
  then the full band is selected, the original master model reloaded and
  the final model fitted once without further self-cal. `full-band` is the
  previous behaviour. CLI `--calibration-mode`, `--if-count`,
  `--channels-per-if`, `--edge-channels`; Run page: a "Calibration mode"
  select with the three per-IF fields beside it.
- Added: a second Stage 1 imaging pass. The calibration pass now writes
  `*.native.cln.fits` / `*.native.resid.fits` on DifMAP's own cell; Stage 1
  then re-images every calibrated epoch of the mode on one grid
  (`stage1.cell_mas`, or the finest native cell) into `*.cln.fits` /
  `*.resid.fits`, so the Stage 3 map figures compare like with like.
  `--reimage-only` (and a Run page switch) repeats just that pass; adding an
  epoch with a finer native cell re-images the older ones. Metadata records
  `native_image_geometry`, `common_image_geometry`, both residual RMS
  values and `fits_image_size`.
- Changed: layout. Every stage writes under `stage<N>/<mode>/`, so full-band
  and per-IF results coexist; Stage 2 and Stage 3 read the configured
  mode's tree (`--calibration-mode` for one run; the Run page passes it to
  the whole chain). Inventory, the Project and Results pages, `lenspipe
  inventory` and provenance checks show the mode.
- Changed: the calibration pass writes `cal.uvf` and the model before
  imaging, as the v1.2.2 script does.
- Removed: `stage1.selfcal_per_if` from 2.0.25 (two days old). A TOML that
  still has it is read as `calibration_mode = "per-if"`.
- Tests: the v1.2.2 script is in `legacy/` and a golden test checks that
  per-IF mode reproduces its models, per-IF models and calibrated data.

## 2.0.25 — 2026-10-13

**Re-run needed:** no, unless you turn the new option on.

- Added: `stage1.selfcal_per_if` (CLI `--selfcal-per-if`, Run page switch
  "Self-cal schedule per IF"). Instead of running the self-cal schedule once
  on the full band, Stage 1 runs it once per IF: each IF selects its inner
  channels, re-reads the starting model and runs every step (selfcal,
  modelfit, gscale where set), so the gains are solved IF by IF against a
  model fitted to that IF. After the loop the full band is selected and the
  final model fitted once, so the Stage 1 model is still a full-band fit to
  the calibrated data. The IF layout comes from `[stage1.final_if_selfcal]`,
  which can stay disabled. Progress counts one unit per step per IF.

## 2.0.24 — 2026-10-12

**Re-run needed:** Stage 1, if a schedule uses gscale.

- Changed: the gscale option on a Stage 1 schedule step no longer replaces
  the step's self-cal. The step runs its selfcal and modelfit as before,
  then gscale (dofloat = float_amplitude) and a second modelfit, so the
  model is refitted on the amplitude-corrected data. In `lenspipe.toml` it
  is a fifth element on the entry, `[amplitude, float_amplitude,
  solint_min, modelfit_iterations, true]`; the 2.0.23 `["gscale", ...]`
  form is withdrawn. The Parameters page switch keeps its name.

## 2.0.23 — 2026-10-12

**Re-run needed:** no, unless you add gscale steps to the schedule.

- Added: a Stage 1 self-cal step can be DifMAP's `gscale`, which solves one
  overall amplitude correction per telescope. In `lenspipe.toml` write the
  step as `["gscale", float_amplitude, modelfit_iterations]` (or a table
  with `gscale = true`); it emits `gscale <dofloat>` followed by `modelfit`.
  On the Parameters page each schedule row has a `gscale` switch; when it is
  on, the row's amplitude and solint settings are ignored. Default
  schedules are unchanged.

## 2.0.22 — 2026-10-08

**Re-run needed:** Stage 3, for the new figure.

- Added: `normalised_rcusp_vs_mjd` in the combined product: each visit's
  R_cusp divided by the all-visit inverse-variance weighted mean, centred on
  1, with the weighted and unweighted scatter in per cent in the margin
  (same definitions as the normalised ratios and fluxes). The R_cusp table
  gains the normalised values, both means and both sigmas; `combined_fits`
  gains `normalised_rcusp` rows; the Results page shows the series with the
  sigmas in the legend.
- Fixed: the restoring beam was missing from some map panels. It is drawn
  from the FITS BMAJ/BMIN/BPA keywords, which residual maps written by Stage 1
  before 2.0.14 do not have. Stage 3 now takes the beam from the same visit's
  clean map when the residual lacks it, so every panel shows one without
  re-running Stage 1.

## 2.0.21 — 2026-10-08

**Re-run needed:** Stage 3, for the new map pages and the R_cusp scatter.

- Changed: map figures use offset axes, delta RA and delta Dec from the
  cutout centre in arcsec with east to the left, instead of absolute
  coordinates.
- Changed: on the all-epochs pages every panel of a kind shares one colour
  scale, taken from the reference visit (`stage3.images.reference_epoch`,
  `--image-reference-epoch`, "Colour-scale reference visit" on the Run page;
  default the first visit), so the same colour means the same flux in every
  panel. The page title names the reference, and the combined metadata
  records it with the limits used. The pages are saved with transparent
  backgrounds (`transparent_all_epochs`, on by default); per-visit pages
  stay opaque.
- Added: R_cusp's visit-to-visit scatter (sample standard deviation, N - 1)
  and its error of the mean, in `combined_fits` and the figure title, next
  to the formal weighted-mean error, which only reflects the propagated
  reference-flux fit errors and is usually far smaller than the scatter.

## 2.0.20 — 2026-10-05

**Re-run needed:** Stage 3, for the new figure.

- Added: `fluxes_vs_mjd_per_image` in the combined product, one panel per
  image with the fitted reference-frequency flux (mJy) against MJD, the
  plain mean over visits as a dashed line, and the mean and the unweighted
  RMS scatter (sigma_u, per cent) annotated in the margin. The Results
  page's "Reference-frequency flux vs MJD" legend now carries the same mean
  and sigma_u per image.

## 2.0.19 — 2026-10-05

**Re-run needed:** Stage 3, to correct the unweighted sigma.

- Fixed: `sigma_unweighted` was evaluated on the raw values (fluxes in Jy),
  which left a factor of sqrt(mean) in it and made it far too small for
  faint images. It is now what the figure shows: the values divided by their
  plain mean, so the normalised mean is exactly 1, and the formula
  sqrt(sum((R_i - mean)^2) / (N * mean)) applied to that series, i.e. the
  population RMS of the normalised values. No error bars anywhere, and the
  same number in Jy or mJy. It differs from `sigma_weighted` only by using
  the plain mean instead of the weighted one and N instead of N - 1.

## 2.0.18 — 2026-10-05

**Re-run needed:** Stage 3, for the extra columns and the second sigma.

- Changed: the visit-to-visit scatter on the normalised flux-ratio and
  normalised reference-flux figures is now reported two ways. The existing
  value is kept as `sigma_weighted`: values divided by the inverse-variance
  weighted mean, then the sample standard deviation (N - 1). New alongside
  it is `sigma_unweighted`, the collaboration's formula on the raw values
  with the plain mean, sqrt(sum((R_i - mean)^2) / (N * mean)), in which the
  error bars play no part; flux series are evaluated in Jy. Both appear as
  percentages in the figure margins (sigma_w, sigma_u), in the Results page
  legends, in the normalised tables (`*_sigma_weighted_percent`,
  `*_sigma_unweighted_percent`, `*_unweighted_mean`) and as separate rows
  in `combined_fits`. The old `*_sigma_percent` column remains as an alias
  of the weighted value.

## 2.0.17 — 2026-10-05

**Re-run needed:** Stage 3, for the new figure layout.

- Changed: nothing explanatory is drawn inside the data area of a Stage 3
  figure any more. Legends sit beside the axes (top right, outside); the
  fit parameters on the annotated spectra and on the combined average
  spectrum and average flux-ratio figures are a caption block under the
  axes; the per-panel sigma on the normalised figures is in the right
  margin; the R_cusp value is in the title. Previously a "best" legend or a
  text block pinned inside the axes could sit on top of points once the
  spectra filled the 12 to 18 GHz frame.

## 2.0.16 — 2026-10-02

**Re-run needed:** Stage 3, to get the new figure and table.

- Added: the normalised-ratio scatter statistic for each image's measured
  flux over time. The combined product gains
  `normalised_reference_fluxes_vs_mjd` (figure and table): every visit's
  fitted reference-frequency flux per image, divided by the all-visit
  inverse-variance weighted mean, one panel per image with its sigma. The
  sigma is the sample standard deviation across visits (ddof=1) in per cent,
  every visit counting equally, exactly as on the normalised flux-ratio
  figure. Values also land in `combined_fits` (product
  `normalised_reference_flux`) and the Results page shows the series
  interactively with the sigma in the legend.

## 2.0.15 — 2026-09-30

**Re-run needed:** Stage 3, to replace the 2.0.14 map figures.

- Changed: the map figures are now A4 pages, one per map. Per visit,
  `<prefix>.image_clean.<fmt>` and `<prefix>.image_residual.<fmt>` (portrait)
  replace the side-by-side `<prefix>.images.<fmt>`; in the combined product,
  `<source>.<product>.images_clean_all_epochs.<fmt>` and
  `..._residual_all_epochs.<fmt>` (portrait, two visits per row, six per
  page, `_p2` onwards for more) replace `images_all_epochs`.
- Added: `stage3.images.residual_pmax`, the residual map's own colour-scale
  percentile (`pmax` now applies to the clean map only). Also
  `--image-residual-pmax` per run and a "Residual pmax" field on the Run page.

## 2.0.14 — 2026-09-30

**Re-run needed:** Stage 1 only if you want BMAJ/BMIN/BPA in existing residual
maps (or add them by hand); Stage 3 to get the new map figures.

- Fixed: the Stage 1 residual map (`<epoch>.resid.fits`) had no BMAJ, BMIN or
  BPA. DifMAP writes the beam only into the restored map. Stage 1 now copies
  the three keywords from `<epoch>.cln.fits` onto the residual after DifMAP
  finishes and records them as `clean_beam_deg` in the Stage 1 metadata.
- Added: Stage 3 map figures from the Stage 1 FITS images. Each visit gets
  `<prefix>.images.<fmt>` with the clean and residual cutouts side by side
  (WCS axes, restoring beam, colour bar in Jy/beam); the combined product
  gets `<source>.<product>.images_all_epochs.<fmt>`, a grid of every visit.
  Settings under `[stage3.images]`: `center` (image centre, `ra,dec` in
  degrees, or sexagesimal), `size_arcsec` (square or `[width, height]`),
  `cmap`, `pmax`, `vmin`, `enabled`. Also as `--image-center`,
  `--image-size`, `--image-cmap`, `--image-pmax`, `--images/--no-images` per
  run, and in the Run page's Stage 3 advanced group. Missing maps skip the
  figure with an `IMAGES SKIPPED` line; they never fail the visit.
- Changed (tests only): the fake DifMAP now writes real FITS maps with WCS
  and, for the restored map, beam keywords, mirroring DifMAP 2.5q.

## 2.0.13 — 2026-09-29

**Re-run needed:** only Stage 3, and only if you want R_cusp on a source whose
images are not named A1, A2, B (set `stage3.rcusp_images` first).

- Fixed: R_cusp disappeared without a word when the configured image names
  did not match the master model's GROUP labels. The default names are
  MG0414's (A1, A2, B); on any other source the product was silently skipped
  and only `rcusp_available: false` recorded it. Stage 3 now warns in the job
  log before plotting and prints `R_CUSP SKIPPED` with the groups it found,
  the combined metadata records `rcusp_images`, `rcusp_reason` and `groups`,
  the Results page shows the reason under the interactive figures, and
  `lenspipe doctor` checks `stage3.rcusp_images` against every master model.
- Changed: when a visit's reference-flux fit is not finite, R_cusp is still
  produced for the other visits and the affected visits are named in a note.

## 2.0.12 — 2026-09-22

**Re-run needed:** no.

Reading the DifMAP 2.5q source showed that `observe` streams the whole input
into a hidden scratch file in the process's working directory, about the size
of the input, and that DifMAP's memory use is small (per-integration buffers
plus one IF of visibilities). Stage 2 shards are therefore bounded by disk
and I/O, not RAM.

- Added: the automatic shard count is also capped by free disk at DifMAP's
  working directory, keeping a 5 GiB reserve and sharing the space across
  concurrent epochs. An explicit shard count is still honoured, with a
  warning when the copies will not fit. The decision (`disk_cap`,
  `free_disk_bytes`, `scratch_bytes_per_process`) is recorded in the Stage 2
  metadata.
- Changed: `lenspipe doctor`'s disk check now states what the planned run
  needs for its scratch copies against the free space where DifMAP will run:
  FAIL when not even one process fits, WARN when the plan would fill the
  disk, with the remedies.
- Added: `stage2.scratch_dir` to run DifMAP, and therefore keep its scratch
  copies and `difmap.log_N` files, on another disk. A per-epoch subdirectory
  is created there and removed when the epoch completes.
- Changed: Stage 2's `unflag` now sends `unflag *, true`, which covers all
  channels regardless of the current selection (the bare form applies to the
  selected channels only). Stage 1 keeps the bare form for legacy parity.

## 2.0.11 — 2026-09-22

**Re-run needed:** no.

- Fixed: the spw and channel in the interactive figures' hover text were
  numbered from 1 like DifMAP IFs. They now follow CASA numbering, both from
  0, so "spw 11 ch 36" is what you would put in a CASA `spw` selection. The
  fit index next to it is unchanged (DifMAP's, from 1).

## 2.0.10 — 2026-09-21

**Re-run needed:** no. Stage 2 products made with the default `unflag = false`
are unchanged.

- Added: `stage2.unflag` (config) and `--unflag/--no-unflag` (per run). On,
  Stage 2 runs `unflag *` on the calibrated file before the channel fits;
  off, the legacy behaviour, fits with the flags Stage 1 wrote. The setting is
  part of the resume fingerprint and recorded in the Stage 2 metadata.
- Added: "Advanced" groups on the console's Run page for the per-run options
  the commands already accepted. Stage 1: final per-IF self-cal. Stage 2:
  edge channels, modelfit iterations, unflag, keep models, quick-look plots,
  error bars. Stage 3: fit method, reference frequency, channel exclusions
  (global and per epoch), annotations, error bars, figure formats. A value is
  passed only when it differs from `lenspipe.toml`, so the command preview
  stays short.
- Added: a Resume button on failed or cancelled Stage 2 jobs in the Jobs
  page. It resubmits the job's own command with `--resume` and without
  `--overwrite`, continuing from the shard checkpoints.

## 2.0.9 — 2026-09-14

**Re-run needed:** no.

- Fixed: figures and PDFs on the Results page came back blank or 404 after
  switching to another project directory from the console. The previous
  project's file route stayed registered ahead of the new one; it is now
  replaced. Restarting `lenspipe ui` inside the project was the workaround.
- Fixed: a console stopped while it was writing a preview could leave a
  truncated thumbnail that was then served blank for as long as the figure
  existed. Thumbnails are written atomically and empty cache files are
  regenerated.

## 2.0.8 — 2026-09-14

**Re-run needed:** no (Stage 2 metadata gains a `spectral_windows` field on
the next run; older products fall back to the IF width or 64 channels).

- Added: interactive Stage 3 figures on the console's Results page. Per-visit
  spectra with their power-law fits and flux ratios with their weighted means,
  and for the combined set the averaged spectra, averaged ratios and the
  versus-MJD series. Hovering a point shows the image, frequency, value and
  error, the fit index, and the spectral window and channel it came from.
  The static PDF/PNG figures are unchanged.
- Added: Stage 2 metadata records the spectral-window layout (`n_spw`,
  `channels_per_spw`) read from the UV-FITS header.

## 2.0.7 — 2026-09-10

**Re-run needed:** no.

- Fixed: `doctor` warned that a working DifMAP "printed no banner". The real
  banner says "difference mapping program - version 2.5q" without the word
  "difmap", and DifMAP can exit on `quit` without flushing it. The probe now
  reads the version from DifMAP's own session log and accepts DifMAP's
  "Quitting program" as identification.
- Fixed: every DifMAP run left a `difmap.log_N` in whatever directory the
  command was run from. Stage 1 and Stage 2 now run DifMAP inside their work
  directories and remove the duplicate.
- Changed: `doctor` names the input file behind a missing master model (a
  name like `1555.L.2.uvfits` means source `1555.L`, epoch `2`) and only
  fails when no file can run; it also flags `memory_multiple = 3.0` left in
  configs written by 2.0.1 and suggests `"auto"`.

## 2.0.6 — 2026-09-10

**Re-run needed:** no.

- Fixed: the Results page could time out. It built its product list on the
  event loop, and that listing re-hashed any multi-gigabyte input whose size
  or mtime had changed; it also pushed every full-resolution figure and every
  table at once. Listings now never read file contents (a touched input shows
  as "stale" until `lenspipe verify` confirms it), the page loads in a worker
  thread, figures are cached thumbnails linking to the full image, and tables
  load when their panel is opened.

## 2.0.5 — 2026-09-10

**Re-run needed:** no.

- Changed: `stage2.memory_multiple` defaults to `"auto"`. Every Stage 2 run
  records DifMAP's peak memory (largest process, from the OS) against the
  input size in `~/.lenspipe/difmap_memory.json`; `auto` sizes shards from the
  largest ratio measured on this machine with a 25 % margin, and from the old
  conservative 3.0 until a measurement exists. Inputs under 200 MB are not
  used for calibration. The peak, input size and ratio are also written to the
  Stage 2 metadata, and `doctor` shows which multiple is in effect.

## 2.0.4 — 2026-09-10

**Re-run needed:** no.

- Changed: jobs and every DifMAP process now run at lower scheduling priority
  (`run.nice`, default 10) so the console and the desktop stay responsive
  while fits saturate the CPUs. Throughput is essentially unchanged.
- Changed: the automatic shard count shares the spare cores between the epochs
  running at once instead of giving each epoch cores minus one, so the machine
  is no longer oversubscribed with the default two epoch workers.
- Changed: the console reads job files off the event loop and tolerates
  20 seconds without a heartbeat before showing "Connection lost" (was 3).

## 2.0.3 — 2026-09-10

**Re-run needed:** no.

- Added: `lenspipe stop [project] [--port N] [--with-jobs] [--list]` ends
  running consoles, including ones whose terminal has since closed and ones
  started by earlier versions (found by a process scan). Jobs keep running
  unless `--with-jobs` is given, because they are separate processes by
  design. `lenspipe ui` now refuses to start a second console on a port that
  already has one and points at `stop`.
- Changed: `lenspipe ui` no longer needs a project argument. Without one it
  opens the last project used (else the current directory); projects are
  switched on the Project page, and `stop` follows the switch.

## 2.0.2 — 2026-09-09

**Re-run needed:** no for DifMAP products (stages 1 to 3 are unchanged).
Affects CASA calibration of future observations only.

- Changed: in CASA step 7 the target's phase solve now appends to `phase.cal`
  like the two calibrators, instead of `append=False`. The legacy setting
  replaced the table with the target's solutions immediately before the
  amplitude solves, `fluxscale` and the flux-calibrator `applycal` needed the
  calibrators' phase solutions, leaving those steps working on flagged data.
  Agreed as a copy-and-paste slip in the original; the target's entries in
  `phase.cal` are not used for the final target calibration either way.

## 2.0.1 — 2026-09-09

**Re-run needed:** no. Products from 2.0.0 are unchanged; epochs that failed
with "RMS marker encountered without a preceding final Flux/Stdev table" can be
completed with `lenspipe stage2 <project> --epoch X --resume` without refitting.

- Fixed: DifMAP's "large map pixels excluded" warning is written to stderr and,
  in a merged log, could split a stdout line at any character. It broke Stage 2
  parsing when it split the table rule or a number (seen on 1555 epoch B with
  DifMAP 2.5q). Stderr is now captured separately (`! [stderr]` lines) and
  existing merged logs are repaired before parsing.
- Added: `lenspipe update` reinstalls from the source the tool was installed
  from (a checkout that was just replaced, or a git remote) and runs `doctor`.
- Added: `scripts/install.sh --from <git-url> --ref <tag>` for installing
  straight from a repository.
- Added: GitHub Actions workflow running lint and tests on Linux.
- Added: plot toggles. `stage2.plot_spectrum` and `stage2.plot_error_bars`
  (the former script constants) and `stage3.plot_error_bars`, also as
  `--plots/--no-plots` and `--error-bars/--no-error-bars`.

## 2.0.0 — 2026-09-09

First packaged release, replacing the standalone scripts (kept under
`legacy/`). Same self-calibration, fitting and plotting behaviour, proven by
golden tests; adds sharded and resumable Stage 2, a configuration file, a
unified CLI, provenance checks, the web console, installer and validation
scripts, and the CASA calibration rewrite.
