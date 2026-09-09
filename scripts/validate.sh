#!/usr/bin/env bash
# Validate lenspipe v2 against an existing legacy project and write a report.
#
#   bash scripts/validate.sh /data/MG0414                 # all epochs with legacy products
#   bash scripts/validate.sh /data/MG0414 --epoch A       # one epoch (quick first pass)
#   bash scripts/validate.sh /data/MG0414 --product channel --work /scratch/MG0414-v2
#
# The legacy project is never modified. A work project is created next to it
# (or at --work), its inputs linked from the legacy inputs/, and v2 is run on
# it: Stage 1, Stage 2 with one shard, Stage 2 with the default shard count,
# Stage 3. After each step `lenspipe compare` checks the products against the
# legacy ones. Timings and DifMAP peak memory go into the report.
#
# Report: <work>/validation-report.txt.  Exit status: 0 = everything matched.
set -uo pipefail
export LC_ALL=C   # stable number formatting in awk/printf

usage() { sed -n '2,15p' "$0"; exit 2; }

LEGACY=""; WORK=""; PRODUCT=""; EPOCH_ARGS=(); FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --work) WORK="$2"; shift 2 ;;
    --product) PRODUCT="$2"; shift 2 ;;
    --epoch) EPOCH_ARGS+=(--epoch "$2"); shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage ;;
    *) if [ -z "$LEGACY" ]; then LEGACY="$1"; shift; else echo "unexpected argument: $1"; usage; fi ;;
  esac
done
[ -n "$LEGACY" ] || usage
LEGACY="$(cd "$LEGACY" && pwd)" || { echo "legacy project not found: $LEGACY"; exit 2; }
[ -d "$LEGACY/inputs" ] || { echo "no inputs/ in $LEGACY"; exit 2; }
[ -d "$LEGACY/stage2" ] || { echo "no stage2/ in $LEGACY; nothing to validate against"; exit 2; }
command -v lenspipe >/dev/null 2>&1 || { echo "lenspipe is not on the PATH; run scripts/install.sh first"; exit 2; }

WORK="${WORK:-${LEGACY%/}-v2test}"
if [ -e "$WORK" ] && [ "$FORCE" -ne 1 ]; then
  echo "work project already exists: $WORK   (use --force to reuse it)"; exit 2
fi
mkdir -p "$WORK"
REPORT="$WORK/validation-report.txt"
: > "$REPORT"

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
rule() { say "----------------------------------------------------------------"; }
now() { date +%s; }

# --- which legacy product are we reproducing? ---------------------------------
if [ -z "$PRODUCT" ]; then
  PRODUCT="$(ls "$LEGACY"/stage2/*/*.stage2.manifest.json 2>/dev/null \
    | sed -E 's#.*/[^/]+\.[^./]+\.([^/]+)\.stage2\.manifest\.json$#\1#' | sort -u)"
  COUNT="$(printf '%s\n' "$PRODUCT" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [ "$COUNT" -ne 1 ]; then
    echo "legacy stage2 has $COUNT product tags:"; printf '  %s\n' $PRODUCT
    echo "choose one with --product"; exit 2
  fi
fi
STAGE2_FLAGS=()
case "$PRODUCT" in
  channel) STAGE2_FLAGS=(--mode channel) ;;
  channel_*) STAGE2_FLAGS=(--mode channel --channels "$(printf '%s' "${PRODUCT#channel_}" | tr '_' ',')") ;;
  if*_edge*) CPI="${PRODUCT#if}"; CPI="${CPI%%_edge*}"; EDGE="${PRODUCT##*_edge}"
             STAGE2_FLAGS=(--mode if --channels-per-if "$CPI" --exclude-edge-channels "$EDGE") ;;
  *) echo "unrecognised product tag: $PRODUCT"; exit 2 ;;
esac

# --- helpers -----------------------------------------------------------------
TIME_BIN=""
if [ -x /usr/bin/time ]; then
  if /usr/bin/time --version >/dev/null 2>&1; then TIME_BIN="gnu"; else TIME_BIN="bsd"; fi
fi
LAST_WALL=0; LAST_RSS_MB="n/a"
timed() {  # timed <label> <command...>  -> sets LAST_WALL (s) and LAST_RSS_MB (peak RSS of largest child)
  local label="$1"; shift
  local start rc tfile; start="$(now)"; tfile="$(mktemp)"
  say ">>> $label"
  say "    $*"
  if [ "$TIME_BIN" = "gnu" ]; then
    /usr/bin/time -v -o "$tfile" "$@" 2>&1 | tee -a "$REPORT"; rc=${PIPESTATUS[0]}
    LAST_RSS_MB="$(awk '/Maximum resident set size/ {printf "%.0f", $6/1024}' "$tfile")"
  elif [ "$TIME_BIN" = "bsd" ]; then
    /usr/bin/time -l "$@" 2> "$tfile" | tee -a "$REPORT"; rc=${PIPESTATUS[0]}
    grep -v 'maximum resident\|average\|real\|user\|sys\|page\|faults\|swaps\|block\|messages\|signals\|context\|instructions\|cycles\|peak' "$tfile" | tee -a "$REPORT" >/dev/null
    LAST_RSS_MB="$(awk '/maximum resident set size/ {printf "%.0f", $1/1048576}' "$tfile")"
  else
    "$@" 2>&1 | tee -a "$REPORT"; rc=${PIPESTATUS[0]}
  fi
  rm -f "$tfile"
  LAST_WALL=$(( $(now) - start ))
  say "    wall ${LAST_WALL}s  peak RSS ${LAST_RSS_MB} MB  exit $rc"
  return "$rc"
}
compare_step() {  # compare_step <stages>
  say ">>> compare stages $1"
  lenspipe compare "$LEGACY" "$WORK" --stages "$1" --product "$PRODUCT" ${EPOCH_ARGS[@]+"${EPOCH_ARGS[@]}"} 2>&1 | tee -a "$REPORT"
  return "${PIPESTATUS[0]}"
}

# --- report header -------------------------------------------------------------
rule
say "lenspipe validation report   $(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "legacy:   $LEGACY"
say "work:     $WORK"
say "product:  $PRODUCT   (stage2 flags: ${STAGE2_FLAGS[*]})"
say "epochs:   ${EPOCH_ARGS[*]:-all}"
say "host:     $(hostname)   $(uname -srm)"
say "version:  $(lenspipe --version)"
rule

FAILED=""; NFAIL=0
note_fail() { FAILED="${FAILED}    - $1\n"; NFAIL=$((NFAIL + 1)); say "!!! $1"; }

# --- set up work project -------------------------------------------------------
say ">>> init"
lenspipe init "$WORK" --inputs "$LEGACY/inputs" --force 2>&1 | tee -a "$REPORT" || { note_fail "init"; }
say ">>> doctor"
lenspipe doctor "$WORK" 2>&1 | tee -a "$REPORT" || note_fail "doctor reported a failure"
rule

# --- stage 1 -------------------------------------------------------------------
timed "stage 1" lenspipe stage1 "$WORK" --overwrite ${EPOCH_ARGS[@]+"${EPOCH_ARGS[@]}"} || note_fail "stage1 run"
compare_step 1 || note_fail "stage1 products differ from legacy"
rule

# --- stage 2, one shard (reference timing + per-process memory) -----------------
timed "stage 2, shards=1" lenspipe stage2 "$WORK" ${STAGE2_FLAGS[@]+"${STAGE2_FLAGS[@]}"} --shards 1 --workers 1 --overwrite ${EPOCH_ARGS[@]+"${EPOCH_ARGS[@]}"} \
  || note_fail "stage2 (1 shard) run"
WALL_1="$LAST_WALL"; RSS_1="$LAST_RSS_MB"
compare_step 2 || note_fail "stage2 (1 shard) products differ from legacy"
rule

# --- stage 2, default shards -----------------------------------------------------
timed "stage 2, default shards" lenspipe stage2 "$WORK" ${STAGE2_FLAGS[@]+"${STAGE2_FLAGS[@]}"} --workers 1 --overwrite ${EPOCH_ARGS[@]+"${EPOCH_ARGS[@]}"} \
  || note_fail "stage2 (default shards) run"
WALL_N="$LAST_WALL"
compare_step 2 || note_fail "stage2 (default shards) products differ from legacy"
rule

# --- stage 3 -------------------------------------------------------------------
timed "stage 3" lenspipe stage3 "$WORK" --product "$PRODUCT" --overwrite ${EPOCH_ARGS[@]+"${EPOCH_ARGS[@]}"} || note_fail "stage3 run"
compare_step 3 || note_fail "stage3 tables differ from legacy"
rule

# --- summary -------------------------------------------------------------------
LARGEST_BYTES="$(ls -lL "$LEGACY"/inputs/*.uvfits 2>/dev/null | awk '{ if ($5 > m) m = $5 } END { print m + 0 }')"
say "SUMMARY"
say "  stage 2 wall time, 1 shard:        ${WALL_1}s"
say "  stage 2 wall time, default shards: ${WALL_N}s"
if [ "${WALL_N:-0}" -gt 0 ] && [ "${WALL_1:-0}" -gt 0 ]; then
  say "  speed-up:                          $(awk -v a="$WALL_1" -v b="$WALL_N" 'BEGIN { printf "%.2fx", a / b }')"
fi
if [ "$RSS_1" != "n/a" ] && [ -n "$RSS_1" ] && [ "$LARGEST_BYTES" -lt 52428800 ]; then
  say "  peak RSS of the largest process:   ${RSS_1} MB (input too small to estimate memory_multiple)"
elif [ "$RSS_1" != "n/a" ] && [ -n "$RSS_1" ] && [ "$LARGEST_BYTES" -gt 0 ]; then
  MULT="$(awk -v r="$RSS_1" -v s="$LARGEST_BYTES" 'BEGIN { printf "%.1f", (r * 1048576) / s }')"
  say "  peak RSS, largest process (DifMAP): ${RSS_1} MB for a $(awk -v s="$LARGEST_BYTES" 'BEGIN { printf "%.0f", s / 1048576 }') MB input"
  say "  suggested stage2.memory_multiple:  ${MULT}   (set this in lenspipe.toml; default is 3.0)"
fi
if [ "$NFAIL" -eq 0 ]; then
  say "  RESULT: PASS - every compared product is identical or numerically equivalent"
  say "report: $REPORT"
  exit 0
fi
say "  RESULT: FAIL"
printf "%b" "$FAILED" | tee -a "$REPORT"
say "report: $REPORT"
exit 1
