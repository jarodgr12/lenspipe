# Changelog

Each entry says whether existing products need re-running. The `lenspipe`
version that made a product is recorded in its metadata JSON.

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
