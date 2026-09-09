# CASA calibration (22A-388, B1555+375, Ku band)

`calibration.py` is the modernised form of `legacy/scriptForCalibration.A.py`
for CASA 6.5.7. Observation-specific values live in a TOML file
(`observation.22A-388.1555.A.toml`); the calibration logic (tables, fields,
channel ranges, solints, interpolation) is unchanged from the legacy script,
with two deliberate exceptions: the final export step (see below), and the
target's phase solve in step 7, which now appends to `phase.cal` instead of
replacing it (the legacy `append=False` discarded the calibrators' phase
solutions before the amplitude solves and `fluxscale` used them; see
`CHANGELOG.md` 2.0.2).

## Running

Under the CASA shell (tasks are shell globals; run from the directory that
holds the MS):

```bash
casa --nologger --log2term -c /path/to/casa/calibration.py \
    --config /path/to/casa/observation.22A-388.1555.A.toml
```

Under a plain Python 3.11+ with the `casatasks`/`casatools` wheels installed:

```bash
python3 casa/calibration.py --config casa/observation.22A-388.1555.A.toml
```

Step 1 (`plotweather`, `gencal antpos`) needs an internet connection.

## Selecting steps

```bash
# list steps (works without CASA)
python3 casa/calibration.py --list-steps --config casa/observation.22A-388.1555.A.toml

# run a subset; comma lists and ranges are accepted
python3 casa/calibration.py --config ... --steps 2,3,4
python3 casa/calibration.py --config ... --steps 0-3,10
```

Without `--steps` all steps run in order. Every run writes
`<mssplit>.calibration.json` (config used, steps run with timestamps and
status, CASA and Python versions); it is updated after each step, so a
failed run still records what completed.

## Step 10: what is exported and why

Step 10 now writes **one** calibrated target file per epoch:

1. `split` field 2 (target) from `<mssplit>`, all 48 spws, `width=1`
   (no channel averaging), `datacolumn="corrected"` -> `<source>.<epoch>.ms`
2. `statwt` once (`minsamp=8`, `datacolumn="data"`, `flagbackup=False`)
3. `exportuvfits` with `multisource=True`, `combinespw=True`,
   `padwithflags=True`, `writestation=True` -> `<source>.<epoch>.uvfits`

DifMAP Stage 1 (`run_difmap_stage1`) requires
`<project>/inputs/<source>.<epoch>.uvfits` with all 48 IFs x 64 channels; its
IF-by-IF self-calibration loop uses `channels_per_if=64` and trims edge
channels itself. The legacy three-file, 4-channel-averaged export cannot feed
that layout. The old export remains available for comparison with
`legacy_split = true` in `[export]`: three files `<source>.<epoch>.{1,2,3}.ms`
/ `.uvfits` (spws 0~15, 16~31, 32~47, `width=4`), with the legacy variable
name bugs fixed and `statwt` run once per file.

## Legacy fixes

- Stray `m` before key 5 in the step-title dict (syntax error) removed.
- Step 0 used the undefined `mymsfile`; now uses the configured `msfile`.
- Step 10 referenced commented-out `mstarget2/3`, `uvtarget2/3` and ran
  `statwt` three times on target 1.

## Step mapping

| Legacy | New | Title | Tasks |
|-------:|----:|-------|-------|
| 0 | 0 | Set the variables and initial split | `mstransform`, `listobs` |
| 1 | 1 | A priori opacity, gain curve, antenna positions, requantizer | `plotweather`, `gencal` x4 |
| 2 | 2 | Flag bad data (from `[[flags]]`) | `flagdata` |
| 3 | 3 | Flux calibrator model | `setjy` |
| 4 | 4 | Short phase correction (`intphase.cal`) | `gaincal` |
| 5 | 5 | Delay correction (`delays.cal`) | `gaincal` K |
| 6 | 6 | Bandpass (`bpass.cal`) | `bandpass` |
| 7 | 7 | Gain phase/amp (`phase.cal`, `amp.cal`) | `gaincal` x6 |
| 8 | 8 | Flux scale + second chain on the phase calibrator (`*2.cal`) | `fluxscale`, `setjy`, `gaincal`, `bandpass` |
| 9 | 9 | Apply tables per field | `applycal` x3 |
| 10 | 10 | Split target and export UVFITS for DifMAP Stage 1 | `split`, `statwt`, `exportuvfits` |

Field ids used by steps 3-10 (after the step 0 split): 0 = 3C286 (flux),
1 = phase calibrator, 2 = target.
