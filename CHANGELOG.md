# Changelog

Each entry says whether existing products need re-running. The `lenspipe`
version that made a product is recorded in its metadata JSON.

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
