#!/bin/bash
# Commit, push, and back up the library. Every step is opt-in and non-fatal.
#
# Called two ways:
#   - by the worker after each successful intake, with a commit message that
#     names the filed sources ("Intake: ai-alignment/zhao-2026-jagged-judges");
#   - daily by launchd (library-sync.plist) with no argument, as a backstop
#     for changes made outside the worker: dashboard deletes, INDEX regens,
#     hand edits. Those get a generic "Library sync" commit.
#
# Configuration (rendered into the plists by install.sh from .env):
#   LIBRARY_AUTOCOMMIT=1          commit pending changes when LIBRARY is a git repo
#   LIBRARY_GIT_REMOTE=nas        push the current branch to this remote
#   LIBRARY_BACKUP_DEST=host:/dir rsync the working tree (incl. gitignored
#                                 PDFs) here; never deletes on the destination
#   LIBRARY_BACKUP_RSYNC_PATH=    remote rsync binary, if not the default
#                                 (Synology: /usr/bin/rsync)
#   REFBOOK_PATH=                 when set, also write retirement triage reports
#                                 (retirement-triage.py) into $REFBOOK_PATH/triage/
set -uo pipefail

CONFIG="$HOME/.config/claude-source-intake"
if [[ -f "$CONFIG/env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$CONFIG/env"
  set +a
fi

if [[ -z "${LIBRARY_PATH:-}" ]]; then
  echo "ERROR: LIBRARY_PATH must be set." >&2
  exit 1
fi
LIBRARY="$LIBRARY_PATH"
AUTOCOMMIT="${LIBRARY_AUTOCOMMIT:-0}"
REMOTE="${LIBRARY_GIT_REMOTE:-}"
DEST="${LIBRARY_BACKUP_DEST:-}"
RSYNC_PATH="${LIBRARY_BACKUP_RSYNC_PATH:-}"
MSG="${1:-}"

# Commits made here are attributed to the pipeline, not the user, so the
# history shows which changes a human made by hand.
GIT_IDENT=(-c user.name="source-intake-agent" -c user.email="source-intake-agent@localhost")
SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=15"
export GIT_SSH_COMMAND="$SSH_CMD"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] library-sync: $*"; }

# One sync at a time. The worker's call waits up to two minutes for a
# backstop run to finish rather than skipping, so its specific commit
# message isn't lost to a generic one.
LOCKDIR="/tmp/claude-source-intake-sync.lock"
waited=0
until mkdir "$LOCKDIR" 2>/dev/null; do
  if [[ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +60 2>/dev/null)" ]]; then
    log "reclaiming stale lock"
    rmdir "$LOCKDIR" 2>/dev/null
    continue
  fi
  if (( waited >= 120 )); then
    log "another sync is running; skipping"
    exit 0
  fi
  sleep 5
  waited=$((waited + 5))
done
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

status=0

if [[ "$AUTOCOMMIT" == "1" ]] && git -C "$LIBRARY" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  changes=$(git -C "$LIBRARY" status --porcelain | wc -l | tr -d ' ')
  if (( changes > 0 )); then
    [[ -n "$MSG" ]] || MSG="Library sync: ${changes} path(s) changed outside intake"
    if git -C "$LIBRARY" add -A && \
       git -C "$LIBRARY" "${GIT_IDENT[@]}" commit -q -m "$MSG"; then
      log "committed: $MSG"
    else
      log "WARNING: commit failed"
      status=1
    fi
  fi
  if [[ -n "$REMOTE" ]]; then
    if git -C "$LIBRARY" push -q "$REMOTE" HEAD 2>&1; then
      log "pushed to $REMOTE"
    else
      log "WARNING: push to $REMOTE failed; commits are safe locally and will go on the next sync"
      status=1
    fi
  fi
fi

if [[ -n "$DEST" ]]; then
  rsync_args=(-a -e "$SSH_CMD" --exclude .git --exclude .claude --exclude .DS_Store)
  [[ -n "$RSYNC_PATH" ]] && rsync_args+=(--rsync-path="$RSYNC_PATH")
  if rsync "${rsync_args[@]}" "$LIBRARY/" "$DEST/"; then
    log "backed up to $DEST"
  else
    log "WARNING: backup to $DEST failed"
    status=1
  fi
fi

# Retirement triage: when a source has been marked superseded/retracted/
# withdrawn, propose a book review of every passage that cites it. Writes only
# new report files under $REFBOOK_PATH/triage/. Non-fatal.
RETIREMENT_TRIAGE="$HOME/Library/Scripts/claude-source-intake-retirement-triage.py"
if [[ -n "${REFBOOK_PATH:-}" && -x "$RETIREMENT_TRIAGE" ]]; then
  "$HOME/.config/claude-source-intake/venv/bin/python" "$RETIREMENT_TRIAGE" 2>&1 \
    | sed 's/^/[library-sync] /' || log "WARNING: retirement triage failed"
fi

exit $status
