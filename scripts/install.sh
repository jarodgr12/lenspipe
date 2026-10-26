#!/usr/bin/env bash
# Install (or update) lenspipe as a plain command and check the environment.
#
#   One line, no checkout needed (tracks master; later updates: `lenspipe update`):
#     curl -LsSf https://raw.githubusercontent.com/jarodgr12/lenspipe/master/scripts/install.sh | bash
#   From a checkout:            bash scripts/install.sh
#   A specific release:         bash scripts/install.sh --from https://github.com/jarodgr12/lenspipe.git --ref v2.0.1
#   Show what would run:        bash scripts/install.sh --dry-run
#
# Installs uv (a Python package manager) if missing, then installs lenspipe with
# its own private Python into ~/.local/bin, then runs `lenspipe doctor`.
# Re-running it updates an existing installation. System Python and CASA are
# never touched.
set -euo pipefail

DEFAULT_REPO="https://github.com/jarodgr12/lenspipe.git"
script_path="${BASH_SOURCE[0]:-}"
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  here="$(cd "$(dirname "$script_path")/.." && pwd)"
else
  here=""   # piped from curl: no checkout, install from the repository
fi
export PATH="$HOME/.local/bin:$PATH"

SOURCE="${here:-$DEFAULT_REPO}"; REF=""; DRY=0
usage() { if [ -n "$script_path" ] && [ -f "$script_path" ]; then sed -n '2,13p' "$script_path"; else echo "usage: install.sh [--from URL|DIR] [--ref TAG] [--dry-run]"; fi; }
while [ $# -gt 0 ]; do
  case "$1" in
    --from) SOURCE="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1"; usage; exit 2 ;;
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
  echo "lenspipe was installed to ~/.local/bin but that directory is not on this script's PATH."
  echo "Add this line to your shell profile and open a new terminal:"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
  exit 1
fi

# This script put ~/.local/bin on its own PATH (line 23), so `lenspipe` works here even
# when the user's shell cannot see it. Decide from the shell's own view: a login shell
# started fresh ("Command 'lenspipe' not found" was the report that led to this).
bin_dir="$HOME/.local/bin"
login_shell="$(basename "${SHELL:-bash}")"
on_user_path=0
case ":$(${SHELL:-bash} -lic 'printf %s "$PATH"' 2>/dev/null):" in
  *":$bin_dir:"*) on_user_path=1 ;;
esac
if [ "$on_user_path" -eq 1 ]; then
  path_note=""
else
  case "$login_shell" in
    zsh)  profile="$HOME/.zshrc" ;;
    fish) profile="" ;;
    *)    profile="$HOME/.bashrc" ;;
  esac
  line='export PATH="$HOME/.local/bin:$PATH"'
  if [ -n "$profile" ] && ! grep -qsF '.local/bin' "$profile"; then
    printf '\n# added by the lenspipe installer: uv tools live here\n%s\n' "$line" >> "$profile"
    path_note="Added ~/.local/bin to your PATH in $profile. Open a NEW terminal (or run: source $profile) before using lenspipe."
  elif [ -n "$profile" ]; then
    path_note="~/.local/bin is set in $profile but not active in this terminal. Open a NEW terminal (or run: source $profile) before using lenspipe."
  else
    path_note="Put ~/.local/bin on your PATH (fish: fish_add_path ~/.local/bin, or run: uv tool update-shell) and open a new terminal before using lenspipe."
  fi
fi

echo
lenspipe --version
echo
lenspipe doctor || true
echo
if [ -n "$path_note" ]; then
  echo "IMPORTANT: $path_note"
  echo "  Until then: $bin_dir/lenspipe works by full path."
  echo
fi
echo "Next steps:"
echo "  lenspipe init /path/to/project --inputs /dir/with/uvfits/and/gmod"
echo "  lenspipe ui /path/to/project"
echo "  lenspipe update          # later, to pick up a new version"
