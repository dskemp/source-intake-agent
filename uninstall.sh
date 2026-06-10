#!/usr/bin/env bash
# Removes the launchd agents and the deployed scripts/plists.
# Preserves runtime state by default ($HOME/.config/claude-source-intake/* and
# the inbox/library) so you can reinstall without losing history.
#
# Pass --purge to also wipe the config dir (env, runs.jsonl, prompt.txt, venv).
# The library and inbox folders are NEVER touched.
set -euo pipefail

LABEL_PREFIX="${LABEL_PREFIX:-com.user}"
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    -h|--help)
      cat <<USAGE
usage: uninstall.sh [--purge]

  --purge   also delete ~/.config/claude-source-intake (api key, run history,
            prompt, python venv). Default keeps these so a reinstall is seamless.
USAGE
      exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

UID_NUM="$(id -u)"
SCRIPTS_DIR="$HOME/Library/Scripts"
PLISTS_DIR="$HOME/Library/LaunchAgents"
CONFIG="$HOME/.config/claude-source-intake"

unload_agent() {
  local label="$1"
  if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
    echo "==> unloading $label"
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  fi
}

unload_agent "${LABEL_PREFIX}.claude-source-intake"
unload_agent "${LABEL_PREFIX}.claude-source-intake-ui"
unload_agent "${LABEL_PREFIX}.claude-source-intake-preprint-check"

echo "==> removing deployed scripts and plists"
rm -f "$PLISTS_DIR/${LABEL_PREFIX}.claude-source-intake.plist" \
      "$PLISTS_DIR/${LABEL_PREFIX}.claude-source-intake-ui.plist" \
      "$PLISTS_DIR/${LABEL_PREFIX}.claude-source-intake-preprint-check.plist" \
      "$SCRIPTS_DIR/claude-source-intake.sh" \
      "$SCRIPTS_DIR/claude-source-intake-ui.py" \
      "$SCRIPTS_DIR/claude-source-intake-regen-index.py" \
      "$SCRIPTS_DIR/claude-source-intake-check-preprints.py" \
      "$SCRIPTS_DIR/claude-source-intake-detect-promotion.py"

if (( PURGE )); then
  echo "==> purging config dir $CONFIG"
  rm -rf "$CONFIG"
else
  echo "==> keeping $CONFIG (api key, runs, prompt, venv) -- pass --purge to wipe"
fi

echo "Uninstalled. Library and inbox folders were not touched."
