#!/usr/bin/env bash
# Idempotent install script. Re-running it just refreshes the deployed code
# and reloads launchd; runtime state (env, runs.jsonl, prompt.txt) is preserved.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Args ---------------------------------------------------------------------
LINK_MODE=0
for arg in "$@"; do
  case "$arg" in
    --link) LINK_MODE=1 ;;
    -h|--help)
      cat <<USAGE
usage: install.sh [--link]

  --link    Symlink the deployed scripts back to this repo instead of copying.
            Edits inside the repo become live immediately (after a launchctl
            kickstart for the dashboard). Best when this repo is the single
            source of truth on a machine you actively develop on.

Configuration (lowest to highest precedence):
  1. Defaults shown below
  2. .env at the repo root (gitignored — put your real paths here)
  3. Environment variables passed on the command line

  DOMAIN="..."                         description of the library's domain
                                       (used as claude context — be specific)
  LIBRARY=~/source-library             library root
  INBOX=~/source-library-inbox         watched folder
  REFBOOK=                             optional, path to a reference-book repo;
                                       when set, each successful intake also
                                       writes a relevance-triage report to
                                       \$REFBOOK/triage/ (stage 2)
  LABEL_PREFIX=com.user                plist label prefix
  CLAUDE_BIN=\$(command -v claude)      claude CLI binary
  CATEGORY_ORDER=                      optional, comma-separated category sort
  OPENALEX_EMAIL=                      optional, opts the weekly preprint
                                       check into OpenAlex's polite pool
  DASHBOARD_EXTRA_ORIGINS=             optional, comma-separated list of extra
                                       browser origins permitted to POST to
                                       the dashboard (CSRF allowlist)
  DASHBOARD_PORT=7341                  localhost port the dashboard binds to
  PREPRINT_REFRESH_DAYS=7              re-check preprint cache entries older
                                       than this many days
USAGE
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# --- Local config (.env at repo root, gitignored) -----------------------------
# Sourced before defaults are applied so vars set there are picked up. The
# documented precedence is: command-line env vars > .env > defaults. Plain
# `source` would let .env clobber the caller's environment, so snapshot any
# already-set values first and restore them after sourcing.
CONFIG_VARS=(DOMAIN LIBRARY INBOX REFBOOK LABEL_PREFIX CLAUDE_BIN CATEGORY_ORDER
             OPENALEX_EMAIL DASHBOARD_EXTRA_ORIGINS DASHBOARD_PORT
             PREPRINT_REFRESH_DAYS)
if [[ -f "$REPO_ROOT/.env" ]]; then
  _pre_vals=()
  for _v in "${CONFIG_VARS[@]}"; do
    if [[ -n "${!_v+x}" ]]; then
      _pre_vals+=("$_v=${!_v}")
    fi
  done
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
  if (( ${#_pre_vals[@]} > 0 )); then
    for _kv in "${_pre_vals[@]}"; do
      export "${_kv?}"
    done
  fi
fi

# --- Configurable via env vars; sensible defaults otherwise -------------------
LIBRARY="${LIBRARY:-$HOME/source-library}"
INBOX="${INBOX:-$HOME/source-library-inbox}"
REFBOOK="${REFBOOK:-}"                 # empty = stage-2 triage disabled
LABEL_PREFIX="${LABEL_PREFIX:-com.user}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
CATEGORY_ORDER="${CATEGORY_ORDER:-}"
DOMAIN="${DOMAIN:-A general-purpose personal research library.}"
OPENALEX_EMAIL="${OPENALEX_EMAIL:-}"
DASHBOARD_EXTRA_ORIGINS="${DASHBOARD_EXTRA_ORIGINS:-}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7341}"
PREPRINT_REFRESH_DAYS="${PREPRINT_REFRESH_DAYS:-7}"

# Expand a leading "~/" (or a bare "~") since parameter expansion doesn't.
expand_tilde() {
  if [[ "$1" == "~" ]]; then
    printf '%s' "$HOME"
  else
    printf '%s' "${1/#\~\//$HOME/}"
  fi
}
LIBRARY="$(expand_tilde "$LIBRARY")"
INBOX="$(expand_tilde "$INBOX")"
[[ -n "$REFBOOK" ]] && REFBOOK="$(expand_tilde "$REFBOOK")"

CONFIG="$HOME/.config/claude-source-intake"
SCRIPTS_DIR="$HOME/Library/Scripts"
PLISTS_DIR="$HOME/Library/LaunchAgents"

WORKER_LABEL="${LABEL_PREFIX}.claude-source-intake"
DASHBOARD_LABEL="${LABEL_PREFIX}.claude-source-intake-ui"
PREPRINT_LABEL="${LABEL_PREFIX}.claude-source-intake-preprint-check"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

# --- Preflight ----------------------------------------------------------------
[[ "$(uname -s)" == "Darwin" ]] || fail "macOS only (this uses launchd)."
command -v python3 >/dev/null || fail "python3 not found in PATH."
[[ -n "$CLAUDE_BIN" && -x "$CLAUDE_BIN" ]] || fail "claude CLI not found. Install Claude Code first, or set CLAUDE_BIN=/path/to/claude."
if [[ -n "$REFBOOK" && ! -d "$REFBOOK" ]]; then
  warn "REFBOOK is set but $REFBOOK is not a directory; the worker will skip triage until it exists."
fi

say "claude:    $CLAUDE_BIN"
say "library:   $LIBRARY"
say "inbox:     $INBOX"
say "refbook:   ${REFBOOK:-<disabled>}"
say "labels:    ${LABEL_PREFIX}.*"
say "domain:    $DOMAIN"
[[ -n "$DASHBOARD_EXTRA_ORIGINS" ]] && say "origins:   $DASHBOARD_EXTRA_ORIGINS"
say "mode:      $([[ $LINK_MODE == 1 ]] && echo 'symlink (single source of truth)' || echo 'copy')"

# --- Directories --------------------------------------------------------------
say "Creating directories..."
mkdir -p "$INBOX/.staged" "$INBOX/_failed" "$INBOX/_duplicate" "$INBOX/_promoted" \
         "$CONFIG" "$SCRIPTS_DIR" "$PLISTS_DIR"

# --- Python venv with deps ----------------------------------------------------
if [[ ! -e "$CONFIG/venv/bin/python" ]]; then
  say "Creating Python venv at $CONFIG/venv..."
  python3 -m venv "$CONFIG/venv"
fi
say "Installing Python deps (Flask, PyYAML)..."
"$CONFIG/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$CONFIG/venv/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"

# --- Scripts ------------------------------------------------------------------
deploy_script() {
  local src="$1" dest="$2"
  rm -f "$dest"  # remove any previous symlink or file
  if (( LINK_MODE )); then
    ln -s "$src" "$dest"
  else
    install -m 0755 "$src" "$dest"
  fi
}
say "Deploying scripts to $SCRIPTS_DIR..."
# Repo files are tracked +x in git; `install -m 0755` and symlink mode both
# ensure the deployed paths end up executable without modifying the repo.
deploy_script "$REPO_ROOT/scripts/worker.sh"          "$SCRIPTS_DIR/claude-source-intake.sh"
deploy_script "$REPO_ROOT/scripts/dashboard.py"       "$SCRIPTS_DIR/claude-source-intake-ui.py"
deploy_script "$REPO_ROOT/scripts/regen-index.py"     "$SCRIPTS_DIR/claude-source-intake-regen-index.py"
deploy_script "$REPO_ROOT/scripts/check-preprints.py" "$SCRIPTS_DIR/claude-source-intake-check-preprints.py"
deploy_script "$REPO_ROOT/scripts/detect-promotion.py" "$SCRIPTS_DIR/claude-source-intake-detect-promotion.py"
deploy_script "$REPO_ROOT/scripts/candidate-manifest.py" "$SCRIPTS_DIR/claude-source-intake-candidate-manifest.py"
deploy_script "$REPO_ROOT/scripts/validate.py"        "$SCRIPTS_DIR/claude-source-intake-validate.py"

# Enforce 0600 on the API key file if it already exists. The README tells
# the user to chmod 600 themselves, but it's the kind of thing that drifts;
# better to converge it on every install run.
if [[ -f "$CONFIG/env" ]]; then
  current=$(stat -f %A "$CONFIG/env" 2>/dev/null || echo "")
  if [[ -n "$current" && "$current" != "600" ]]; then
    say "Tightening perms on $CONFIG/env ($current -> 600)"
    chmod 600 "$CONFIG/env"
  fi
fi

# --- Default prompt (only if missing) -----------------------------------------
if [[ ! -f "$CONFIG/prompt.txt" ]]; then
  say "Installing default autonomy prompt (substituting __LIBRARY__)..."
  sed -e "s|__LIBRARY__|${LIBRARY}|g" -e "s|__INBOX__|${INBOX}|g" \
      "$REPO_ROOT/config/prompt.txt" > "$CONFIG/prompt.txt"
  chmod 0644 "$CONFIG/prompt.txt"
else
  say "Keeping existing prompt at $CONFIG/prompt.txt"
fi

# --- Relevance (triage) prompt: always re-rendered ----------------------------
# Unlike prompt.txt (user-customizable via the dashboard, so rendered only if
# missing), the relevance prompt is treated as a build artifact: re-rendered on
# every install so repo updates actually deploy. Customize it in the repo, not
# in $CONFIG. Rendered even when REFBOOK is unset — the worker's REFBOOK_PATH
# guard is the on/off switch, and a stale copy would otherwise linger.
say "Installing relevance (triage) prompt..."
sed -e "s|__LIBRARY__|${LIBRARY}|g" -e "s|__REFBOOK__|${REFBOOK}|g" \
    "$REPO_ROOT/config/relevance-prompt.txt" > "$CONFIG/relevance-prompt.txt"
chmod 0644 "$CONFIG/relevance-prompt.txt"

# --- Render plists ------------------------------------------------------------
render_plist() {
  local src="$1" dest="$2"
  # DOMAIN is free text destined for an XML document: escape XML entities
  # first (a literal & or < would make plutil -lint reject the plist), then
  # escape sed-replacement specials, and collapse newlines.
  local domain_esc
  domain_esc=$(printf '%s' "$DOMAIN" | tr '\n' ' ' \
    | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
    | sed -e 's/[\\&|]/\\&/g')
  sed -e "s|__HOME__|${HOME}|g" \
      -e "s|__LIBRARY__|${LIBRARY}|g" \
      -e "s|__INBOX__|${INBOX}|g" \
      -e "s|__REFBOOK__|${REFBOOK}|g" \
      -e "s|__CLAUDE__|${CLAUDE_BIN}|g" \
      -e "s|__LABEL_PREFIX__|${LABEL_PREFIX}|g" \
      -e "s|__CATEGORY_ORDER__|${CATEGORY_ORDER}|g" \
      -e "s|__DOMAIN__|${domain_esc}|g" \
      -e "s|__OPENALEX_EMAIL__|${OPENALEX_EMAIL}|g" \
      -e "s|__DASHBOARD_EXTRA_ORIGINS__|${DASHBOARD_EXTRA_ORIGINS}|g" \
      -e "s|__DASHBOARD_PORT__|${DASHBOARD_PORT}|g" \
      -e "s|__PREPRINT_REFRESH_DAYS__|${PREPRINT_REFRESH_DAYS}|g" \
      "$src" > "$dest"
  plutil -lint "$dest" >/dev/null
}
say "Rendering launchd plists..."
render_plist "$REPO_ROOT/launchd/worker.plist.template"          "$PLISTS_DIR/$WORKER_LABEL.plist"
render_plist "$REPO_ROOT/launchd/dashboard.plist.template"       "$PLISTS_DIR/$DASHBOARD_LABEL.plist"
render_plist "$REPO_ROOT/launchd/preprint-check.plist.template"  "$PLISTS_DIR/$PREPRINT_LABEL.plist"

# --- (Re)load launchd agents --------------------------------------------------
UID_NUM="$(id -u)"
reload_agent() {
  local label="$1"
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
    sleep 0.5
  fi
  launchctl bootstrap "gui/$UID_NUM" "$PLISTS_DIR/$label.plist"
}
say "Loading launchd agents..."
reload_agent "$WORKER_LABEL"
reload_agent "$DASHBOARD_LABEL"
reload_agent "$PREPRINT_LABEL"

# --- Summary ------------------------------------------------------------------
echo
say "Installed."
echo "  Dashboard:     http://localhost:${DASHBOARD_PORT}"
echo "  Inbox:         $INBOX"
echo "  Library:       $LIBRARY"
echo "  Logs:          /tmp/claude-source-intake{,.err}.log"
echo

if [[ ! -f "$CONFIG/env" ]]; then
  warn "API key not configured."
  echo "  Set it with:"
  echo "    echo 'ANTHROPIC_API_KEY=sk-ant-...' > $CONFIG/env"
  echo "    chmod 600 $CONFIG/env"
fi
