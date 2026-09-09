#!/usr/bin/env bash
# Install (or update) lenspipe as a plain command and check the environment.
#
#   bash scripts/install.sh                                   # from this checkout
#   bash scripts/install.sh --from https://host/lenspipe.git --ref v2.0.1
#   bash scripts/install.sh --dry-run                         # show what would run
#
# Installs uv (a Python package manager) if missing, then installs lenspipe with
# its own private Python into ~/.local/bin, then runs `lenspipe doctor`.
# Re-running it updates an existing installation. System Python and CASA are
# never touched. Later updates: `lenspipe update`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"

SOURCE="$here"; REF=""; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) SOURCE="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1"; sed -n '2,12p' "$0"; exit 2 ;;
  esac
done

case "$SOURCE" in
  http://*|https://*|git@*|ssh://*|git+*)
    spec="${SOURCE#git+}"
    [ -n "$REF" ] && spec="${spec}@${REF}"
    from_arg="git+${spec}"
    ;;
  *)
    [ -f "$SOURCE/pyproject.toml" ] || { echo "not a lenspipe checkout: $SOURCE"; exit 2; }
    [ -n "$REF" ] && echo "note: --ref is ignored for a local checkout"
    from_arg="$SOURCE"
    ;;
esac

cmd=(uv tool install --force --python 3.12 --with emcee --from "$from_arg" lenspipe)
if [ "$DRY" -eq 1 ]; then
  printf 'would run:'; printf ' %q' "${cmd[@]}"; echo
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv ..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "Installing lenspipe from $from_arg ..."
"${cmd[@]}"

if ! command -v lenspipe >/dev/null 2>&1; then
  echo
  echo "lenspipe was installed to ~/.local/bin but that directory is not on your PATH."
  echo "Add this line to your shell profile and open a new terminal:"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
  exit 1
fi

echo
lenspipe --version
echo
lenspipe doctor || true
echo
echo "Next steps:"
echo "  lenspipe init /path/to/project --inputs /dir/with/uvfits/and/gmod"
echo "  lenspipe ui /path/to/project"
echo "  lenspipe update          # later, to pick up a new version"
