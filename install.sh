#!/usr/bin/env bash
# Q3TUI installer — installs the `q3tui` command with `uv tool` (the same way a developer install works here):
#
#   ./install.sh                 install (editable: `git pull` updates it) into ~/.local/bin
#   ./install.sh --no-editable   install a copy (an update needs ./install.sh again)
#   ./install.sh --uninstall     remove the command
#   ./install.sh --dry-run       print what would be done, change nothing
#
# Needs: bash, curl or an existing `uv` (the script can install uv for you), network access for the Python packages
# (or an internal PyPI mirror: UV_INDEX_URL=...). Python >= 3.11 is fetched by uv when the system one is older.
# Not installed by this script: Synopsys tools (VCS, Design Compiler — your site's modules) and Claude access.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EDITABLE=1
DRY=0
UNINSTALL=0
ASSUME_YES=0
PYTHON_REQ=">=3.11"

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-editable) EDITABLE=0 ;;
    --editable)    EDITABLE=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    --dry-run|-n)  DRY=1 ;;
    --yes|-y)      ASSUME_YES=1 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[31m ✖\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else "$@"; fi; }

# --- uv ---------------------------------------------------------------------------------------------------------------
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    ok "uv $(uv --version | awk '{print $2}')"
    return
  fi
  warn "uv is not installed (https://docs.astral.sh/uv/)"
  if [ "$ASSUME_YES" != 1 ] && [ "$DRY" != 1 ]; then
    read -r -p "    install uv now with the official installer (curl | sh)? [y/N] " a
    case "$a" in y|Y|yes) ;; *) die "uv is required: install it (e.g. 'pip install uv' or your site's package) and run again" ;; esac
  fi
  command -v curl >/dev/null 2>&1 || die "curl is needed to fetch the uv installer (or install uv yourself)"
  if [ "$DRY" = 1 ]; then
    printf '   [dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh\n'
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH: open a new shell and run again"
  fi
}

# --- uninstall --------------------------------------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  command -v uv >/dev/null 2>&1 || die "uv not found: nothing to uninstall with"
  say "removing the q3tui command"
  run uv tool uninstall q3tui
  [ "$DRY" = 1 ] || ok "removed (your ~/.q3tui settings and your projects are untouched)"
  exit 0
fi

# --- checks -----------------------------------------------------------------------------------------------------------
[ -f "$REPO/pyproject.toml" ] || die "run this script from a Q3TUI checkout (pyproject.toml not found in $REPO)"
say "installing Q3TUI from $REPO"
[ "$(uname -s)" = "Linux" ] || warn "tested on Linux only (EDA tools are Linux tools)"
ensure_uv

# --- install ----------------------------------------------------------------------------------------------------------
say "installing the 'q3tui' command (uv tool, Python $PYTHON_REQ)"
args=(tool install --force --python "$PYTHON_REQ")
if [ "$EDITABLE" = 1 ]; then args+=(--editable "$REPO"); else args+=("$REPO"); fi
run uv "${args[@]}"

BIN_DIR="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *) warn "$BIN_DIR is not on PATH — run 'uv tool update-shell' (or add 'export PATH=\"$BIN_DIR:\$PATH\"' to your shell profile) and open a new shell" ;;
esac

# --- user settings ----------------------------------------------------------------------------------------------------
CONF_DIR="${Q3TUI_HOME:-$HOME/.q3tui}"
if [ -f "$CONF_DIR/q3tui.yaml" ]; then
  ok "keeping your settings: $CONF_DIR/q3tui.yaml"
elif [ -f "$REPO/q3tui.example.yaml" ]; then
  say "writing a starting configuration: $CONF_DIR/q3tui.yaml (edit the model, tool modules, flow)"
  run mkdir -p "$CONF_DIR"
  run cp "$REPO/q3tui.example.yaml" "$CONF_DIR/q3tui.yaml"
fi

# --- what the script cannot do for you --------------------------------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code found: $(command -v claude) (Q3TUI uses the Claude Agent SDK: log in with 'claude' once, or set ANTHROPIC_API_KEY)"
else
  warn "Claude Code CLI not found — Q3TUI's LLM calls go through the Claude Agent SDK: install Claude Code and log in, or set ANTHROPIC_API_KEY"
fi
if command -v module >/dev/null 2>&1 || [ -f /etc/profile.d/modules.sh ]; then
  ok "environment modules available (Synopsys tools are loaded through tools.json 'modules')"
else
  warn "no 'module' command: Synopsys tools must be on PATH, or set 'setup_script' in tools.json"
fi

# --- verify -----------------------------------------------------------------------------------------------------------
if [ "$DRY" != 1 ]; then
  say "checking the install"
  export PATH="$BIN_DIR:$PATH"
  command -v q3tui >/dev/null 2>&1 || die "the q3tui command is not on PATH yet (see the PATH note above)"
  q3tui --version
  q3tui flow list
fi

cat <<EOF

$(printf '\033[1m')Next steps$(printf '\033[0m')
  mkdir my_block && cd my_block
  q3tui tools init        # tools.json: VCS lint / compile / run, Design Compiler — edit modules and the .db library
  q3tui tools check       # are the tools found?
  q3tui run --intent "…"  # or put your spec in spec/ ; \`q3tui\` alone opens the TUI (/run, ? for keys)
Docs: $REPO/docs/spec/README.md · flows: docs/spec/vlsit-flow.md · examples: $REPO/examples/
EOF
