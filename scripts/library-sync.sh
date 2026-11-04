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
#   LIBRARY_BACKUP_DEST=host:/dir mirror the working tree (incl. gitignored
#                                 PDFs) here. Files removed from the library
#                                 are removed from the mirror too, but never
#                                 destroyed: each deleted or overwritten file
#                                 is moved to <dir>-history/<run timestamp>/.
#   LIBRARY_BACKUP_RSYNC_PATH=    remote rsync binary, if not the default
#                                 (Synology: /usr/bin/rsync)
#   REFBOOK_PATH=                 when set, also write retirement triage reports
#                                 (retirement-triage.py) into $REFBOOK_PATH/triage/
#
# Script defaults (not rendered by install.sh; override in the environment):
#   LIBRARY_BACKUP_MAX_DELETE=25  a run that would delete more files than this
#                                 from the mirror stops deleting and fails, so
#                                 an emptied or half-mounted library can't
#                                 empty the mirror
#   LIBRARY_SYNC_STATUS=          status file, default
#                                 ~/.config/claude-source-intake/library-sync-status.json
#   LIBRARY_SYNC_NOTIFY=1         post a macOS notification when a step fails
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
MAX_DELETE="${LIBRARY_BACKUP_MAX_DELETE:-25}"
STATUS_FILE="${LIBRARY_SYNC_STATUS:-$CONFIG/library-sync-status.json}"
NOTIFY="${LIBRARY_SYNC_NOTIFY:-1}"
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
ERRFILE=$(mktemp -t library-sync)
trap 'rmdir "$LOCKDIR" 2>/dev/null; rm -f "$ERRFILE"' EXIT

status=0
failed_steps=""   # comma-separated; a plain string because /bin/bash is 3.2
detail=""         # the first failure's last error line, for the notification
mirror_deleted=0
history_dir=""

# Record a failed step. The step's own output has already been logged.
fail() {  # fail <step> <log message> [error line]
  log "WARNING: $2"
  failed_steps="${failed_steps:+$failed_steps,}$1"
  [[ -n "$detail" ]] || detail="$1: ${3:-$2}"
  status=1
}

# Last non-empty line of a command's output: usually the actual error.
last_line() { printf '%s\n' "$1" | sed '/^[[:space:]]*$/d' | tail -1; }

if [[ "$AUTOCOMMIT" == "1" ]] && git -C "$LIBRARY" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  changes=$(git -C "$LIBRARY" status --porcelain | wc -l | tr -d ' ')
  if (( changes > 0 )); then
    [[ -n "$MSG" ]] || MSG="Library sync: ${changes} path(s) changed outside intake"
    if out=$(git -C "$LIBRARY" add -A 2>&1 && \
             git -C "$LIBRARY" "${GIT_IDENT[@]}" commit -q -m "$MSG" 2>&1); then
      log "committed: $MSG"
    else
      [[ -n "$out" ]] && printf '%s\n' "$out"
      fail commit "commit failed" "$(last_line "$out")"
    fi
  fi
  if [[ -n "$REMOTE" ]]; then
    if out=$(git -C "$LIBRARY" push -q "$REMOTE" HEAD 2>&1); then
      [[ -n "$out" ]] && printf '%s\n' "$out"
      log "pushed to $REMOTE"
    else
      [[ -n "$out" ]] && printf '%s\n' "$out"
      fail push "push to $REMOTE failed; commits are safe locally and will go on the next sync" \
        "$(last_line "$out")"
    fi
  fi
fi

if [[ -n "$DEST" ]]; then
  # Mirror, but keep what the mirror loses. --delete removes files that are
  # gone from the library; --backup moves each deleted or overwritten file
  # into a per-run folder beside the mirror (a relative --backup-dir is
  # resolved on the receiving side, relative to the destination) instead of
  # destroying it. Excluded paths are never deleted on the destination.
  # The receiving side must be rsync 3.x: macOS's openrsync skips deletions
  # when --backup is set, so a local destination only gains files.
  dest_dir="${DEST%/}"
  dest_dir="${dest_dir##*:}"
  history_dir="../$(basename "$dest_dir")-history/$(date -u '+%Y%m%dT%H%M%SZ')"
  rsync_args=(-a -e "$SSH_CMD" --exclude .git --exclude .claude --exclude .DS_Store)
  [[ -n "$RSYNC_PATH" ]] && rsync_args+=(--rsync-path="$RSYNC_PATH")

  # Count the deletions first, so a run over the limit deletes nothing at all
  # (rsync's own --max-delete deletes up to the limit before it stops).
  if out=$(rsync "${rsync_args[@]}" -n --delete --itemize-changes "$LIBRARY/" "$DEST/" 2>"$ERRFILE"); then
    # sort -u: openrsync prints each deletion twice
    pending=$(printf '%s\n' "$out" | grep '^\*deleting' | sed 's/^\*deleting *//' | sort -u | grep -c .)
    delete_args=(--delete --max-delete="$MAX_DELETE")
    if (( pending > MAX_DELETE )); then
      delete_args=()
    fi
    out=$(rsync "${rsync_args[@]}" "${delete_args[@]+"${delete_args[@]}"}" \
            --backup --backup-dir="$history_dir" --itemize-changes \
            "$LIBRARY/" "$DEST/" 2>"$ERRFILE")
    rc=$?
  else
    rc=$?
    pending=0
  fi
  errs=$(cat "$ERRFILE")
  [[ -n "$errs" ]] && printf '%s\n' "$errs" >&2
  deleted=$(printf '%s\n' "$out" | grep '^\*deleting' | sed 's/^\*deleting *//' | sort -u)
  # Existing files rsync replaced (">f" with a change string, not "+++++++" for new).
  replaced=$(printf '%s\n' "$out" | grep -E '^[<>]f' | grep -vc '+++++++')
  if [[ -n "$deleted" ]]; then
    mirror_deleted=$(printf '%s\n' "$deleted" | grep -c .)
    printf '%s\n' "$deleted" | sed 's/^/  removed from mirror: /'
  fi
  if (( mirror_deleted + replaced > 0 )); then
    log "kept ${mirror_deleted} removed and ${replaced} replaced file(s) in ${history_dir#../} beside the mirror"
  else
    history_dir=""   # rsync creates the folder only when it moves something into it
  fi
  if (( pending > MAX_DELETE )); then
    fail backup "backup to $DEST copied new and changed files but deleted nothing: ${pending} file(s) are gone from the library, more than LIBRARY_BACKUP_MAX_DELETE=${MAX_DELETE}. If that is intended, run once with the limit raised" \
      "${pending} files would be deleted from the mirror (limit ${MAX_DELETE}); deleted none$( (( rc != 0 )) && echo "; rsync also failed: $(last_line "$errs")")"
  elif (( rc == 0 )); then
    log "backed up to $DEST"
  else
    fail backup "backup to $DEST failed (rsync exit $rc)" "$(last_line "$errs")"
  fi
fi

# Status file: read by the currency console's backup card, and the durable
# record of failures (the launchd logs live in /tmp, which a reboot clears).
json_str() {
  local s
  s=$(printf '%s' "$1" | tr -d '\000-\037')
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}
now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
prev_success=$(sed -n 's/^ *"last_success": *"\([^"]*\)".*/\1/p' "$STATUS_FILE" 2>/dev/null | head -1)
prev_failures=$(sed -n 's/^ *"consecutive_failures": *\([0-9]*\).*/\1/p' "$STATUS_FILE" 2>/dev/null | head -1)
if (( status == 0 )); then
  outcome=ok; last_success="$now"; failures=0
else
  outcome=failed; last_success="$prev_success"; failures=$(( ${prev_failures:-0} + 1 ))
fi
mkdir -p "$(dirname "$STATUS_FILE")"
cat > "$STATUS_FILE.tmp" <<EOF
{
  "last_run": "$now",
  "outcome": "$outcome",
  "failed_steps": $(json_str "$failed_steps"),
  "detail": $(json_str "$detail"),
  "last_success": $(json_str "$last_success"),
  "consecutive_failures": $failures,
  "mirror_deleted": $mirror_deleted,
  "history_dir": $(json_str "${history_dir#../}")
}
EOF
mv "$STATUS_FILE.tmp" "$STATUS_FILE"

if (( status != 0 )) && [[ "$NOTIFY" == "1" ]]; then
  note="$detail"
  (( failures > 1 )) && note="$note (failed ${failures} runs in a row; last success ${last_success:-never})"
  # Pass the text as arguments so quotes in an error line can't break the script.
  osascript -e 'on run argv' \
            -e 'display notification (item 1 of argv) with title "Library backup failed" subtitle (item 2 of argv)' \
            -e 'end run' "$note" "Steps: $failed_steps" >/dev/null 2>&1 \
    || log "WARNING: could not post the failure notification"
fi

# Retirement triage: when a source has been marked superseded/retracted/
# withdrawn, propose a book review of every passage that cites it. Writes only
# new report files under $REFBOOK_PATH/triage/. Non-fatal.
RETIREMENT_TRIAGE="$HOME/Library/Scripts/claude-source-intake-retirement-triage.py"
if [[ -n "${REFBOOK_PATH:-}" && -x "$RETIREMENT_TRIAGE" ]]; then
  "$HOME/.config/claude-source-intake/venv/bin/python" "$RETIREMENT_TRIAGE" 2>&1 \
    | sed 's/^/[library-sync] /' || log "WARNING: retirement triage failed"
fi

# Commit pending triage reports (new ones, and the user's decision edits) in
# the refbook checkout, so the record lives in git. Local commits on main only.
COMMIT_TRIAGE="$HOME/Library/Scripts/claude-source-intake-commit-triage.sh"
if [[ -n "${REFBOOK_PATH:-}" && -x "$COMMIT_TRIAGE" ]]; then
  "$COMMIT_TRIAGE" 2>&1 | sed 's/^/[library-sync] /' || log "WARNING: committing triage reports failed"
fi

exit $status
