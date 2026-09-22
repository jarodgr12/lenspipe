# Changelog

Each entry says whether existing products need re-running. The `lenspipe`
version that made a product is recorded in its metadata JSON.

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
