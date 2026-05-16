#!/bin/bash
set -euo pipefail

CONFIG="$HOME/.config/claude-source-intake"
ENV_FILE="$CONFIG/env"

# Source $CONFIG/env FIRST so values it sets — ANTHROPIC_API_KEY, plus any
# tunables the user wants to override (MODEL, CLAUDE_TIMEOUT, etc.) — are
# in effect when the parameter expansions below evaluate. launchd-spawned
# processes don't inherit your shell's env, so this file is also where
# ANTHROPIC_API_KEY lives. Mode 0600 enforced by install.sh.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# NB: install.sh consumes INBOX/LIBRARY and renders them into the launchd
# plist as INBOX_PATH/LIBRARY_PATH. The asymmetry is intentional — the
# plist exposes the "_PATH" suffix to make the runtime contract explicit.
INBOX="${INBOX_PATH:-$HOME/source-library-inbox}"
LIBRARY="${LIBRARY_PATH:-$HOME/source-library}"
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo $HOME/.local/bin/claude)}"
PYTHON="$CONFIG/venv/bin/python"

LOCKDIR="/tmp/claude-source-intake.lock"
PROMPT_FILE="$CONFIG/prompt.txt"
PAUSED_FLAG="$CONFIG/paused"
RUNS_LOG="$CONFIG/runs.jsonl"
RUN_LOGS_DIR="$CONFIG/run-logs"

# Tunables (override via the env file above, or the launchd plist).
MODEL="${MODEL:-claude-sonnet-4-6}"
CLAUDE_TIMEOUT="${CLAUDE_TIMEOUT:-900}"          # 15 min wall clock per attempt
MAX_RETRIES="${MAX_RETRIES:-2}"                  # 0 = single attempt, 2 = up to 3 attempts
RETRY_BACKOFF="${RETRY_BACKOFF:-30}"             # seconds between retries
RUNS_LOG_MAX_BYTES="${RUNS_LOG_MAX_BYTES:-5242880}"   # rotate runs.jsonl > 5 MB
RUN_LOG_KEEP="${RUN_LOG_KEEP:-50}"               # number of per-iteration archives to retain

mkdir -p "$INBOX/.staged" "$INBOX/_failed" "$INBOX/_duplicate" "$CONFIG" "$RUN_LOGS_DIR"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "ERROR: prompt file missing at $PROMPT_FILE" >&2
  echo "Run install.sh from the source-intake-agent repo to restore it." >&2
  exit 1
fi

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# Dedup check. Echoes "<matched-summary-path>\t<reason>" where reason is one of:
#   hash | url              — duplicate of an existing source (move to _duplicate/)
#   backfill:hash           — same bytes as an existing summary whose .pdf is missing
#   backfill:url            — same URL as an existing summary whose .pdf is missing
#   backfill:fuzzy          — input PDF filename strongly matches a missing-original
#                             summary's slug/title (last-ditch repair signal)
# Empty output = not a duplicate; proceed with normal intake.
check_dedup() {
  local input_path="$1"
  "$PYTHON" - "$LIBRARY" "$input_path" <<'PY'
import hashlib, re, sys
from difflib import SequenceMatcher
from pathlib import Path
import yaml

library, input_path = sys.argv[1:3]
input_path = Path(input_path)

hasher = hashlib.sha256()
with open(input_path, 'rb') as f:
    for chunk in iter(lambda: f.read(65536), b''):
        hasher.update(chunk)
input_hash = hasher.hexdigest()

input_url = ""
if input_path.suffix.lower() in ('.txt', '.url'):
    try:
        text = input_path.read_text(errors='replace')
        m = re.search(r'https?://\S+', text)
        if m:
            input_url = m.group(0).rstrip('.,;)>"\'')
    except Exception:
        pass

is_pdf = input_path.suffix.lower() == '.pdf'

def has_original(summary_path):
    slug = summary_path.name[: -len('.summary.md')]
    return (summary_path.parent / f"{slug}.pdf").exists() or \
           (summary_path.parent / f"{slug}.snapshot.md").exists()

def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

input_name_norm = normalize(input_path.stem)
fuzzy_candidates = []  # (summary_path, slug_norm, title_norm)

for summary in Path(library).glob('*/*/*.summary.md'):
    try:
        text = summary.read_text()
    except Exception:
        continue
    if not text.startswith('---\n'):
        continue
    end = text.find('\n---\n', 4)
    if end == -1:
        continue
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        continue
    if input_hash and fm.get('source_hash') == input_hash:
        reason = 'backfill:hash' if (is_pdf and not has_original(summary)) else 'hash'
        print(f"{summary}\t{reason}")
        sys.exit(0)
    if input_url and fm.get('url') == input_url:
        reason = 'backfill:url' if (is_pdf and not has_original(summary)) else 'url'
        print(f"{summary}\t{reason}")
        sys.exit(0)
    if is_pdf and not has_original(summary):
        slug = summary.name[: -len('.summary.md')]
        # Arxiv IDs (NNNN.NNNNN) in the summary's URL are nearly unique, so
        # we surface them as a strong-signal match against the raw filename.
        url_ids = re.findall(r'\b\d{4}\.\d{4,5}\b', str(fm.get('url') or ''))
        fuzzy_candidates.append((
            summary,
            normalize(slug),
            normalize(fm.get('title') or ''),
            url_ids,
        ))

# Fuzzy backfill: only PDF inputs, only against summaries known to be missing
# their original. Score is the max of slug- and title-similarity, lifted to 0.85
# on substring containment and boosted +0.10 when a year in the slug also
# appears in the input filename. Require top >= 0.70 AND a >= 0.15 margin over
# the runner-up so an ambiguous pile of candidates declines to guess.
if is_pdf and fuzzy_candidates:
    input_name_raw = input_path.stem  # for arxiv-id substring check (case-sensitive ids)
    def score(slug_norm, title_norm, url_ids):
        # Arxiv ID hit on the raw filename is decisive — IDs are unique, so a
        # match here outweighs whatever fuzzy strings say.
        for uid in url_ids:
            if uid in input_name_raw:
                return 0.95
        s1 = SequenceMatcher(None, input_name_norm, slug_norm).ratio() if slug_norm else 0
        s2 = SequenceMatcher(None, input_name_norm, title_norm).ratio() if title_norm else 0
        sc = max(s1, s2)
        if slug_norm and (slug_norm in input_name_norm or input_name_norm in slug_norm):
            sc = max(sc, 0.85)
        if title_norm and (title_norm in input_name_norm or input_name_norm in title_norm):
            sc = max(sc, 0.85)
        ym = re.search(r'\b(19|20)\d{2}\b', slug_norm)
        if ym and ym.group(0) in input_name_norm:
            sc += 0.10
        return min(sc, 1.0)
    scored = sorted(
        ((score(s, t, u), p) for p, s, t, u in fuzzy_candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    top_score, top_summary = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= 0.70 and (top_score - second_score) >= 0.15:
        print(f"{top_summary}\tbackfill:fuzzy")
PY
}

# Compute sha256 of a file (for post-success hash injection).
file_sha256() {
  shasum -a 256 "$1" | awk '{print $1}'
}

# Insert source_hash: into a summary's frontmatter, idempotently.
inject_hash() {
  local summary_path="$1" hash_val="$2"
  "$PYTHON" - "$summary_path" "$hash_val" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
hash_val = sys.argv[2]
text = path.read_text()
if 'source_hash:' in text:
    sys.exit(0)
if not text.startswith('---\n'):
    sys.exit(1)
end = text.find('\n---\n', 4)
if end == -1:
    sys.exit(1)
new_line = f'source_hash: "{hash_val}"\n'
m = re.search(r'^tldr:[^\n]*\n', text[:end + 1], re.MULTILINE)
insert_at = m.end() if m else end + 1
path.write_text(text[:insert_at] + new_line + text[insert_at:])
PY
}

append_run() {
  local ts="$1" name="$2" outcome="$3" paths_file="$4" err_file="$5" log_file="$6"
  "$PYTHON" - "$ts" "$name" "$outcome" "$paths_file" "$err_file" "$log_file" "$RUNS_LOG" <<'PY'
import sys, json
ts, name, outcome, paths_file, err_file, log_file, runs_log = sys.argv[1:8]
try:
    with open(paths_file) as f:
        paths = [l.strip() for l in f if l.strip()]
except FileNotFoundError:
    paths = []
err = ""
if err_file:
    try:
        with open(err_file) as f:
            err = f.read()[-2000:]
    except FileNotFoundError:
        pass
cost_usd = None
duration_ms = None
if log_file:
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "result":
                    cost_usd = e.get("total_cost_usd") or e.get("cost_usd")
                    duration_ms = e.get("duration_ms")
    except FileNotFoundError:
        pass
entry = {
    "ts": ts, "input_name": name, "outcome": outcome,
    "output_paths": paths, "error_excerpt": err,
    "cost_usd": cost_usd, "duration_ms": duration_ms,
}
with open(runs_log, "a") as f:
    f.write(json.dumps(entry) + "\n")
PY
}

# Rotate runs.jsonl if it has grown past the configured ceiling. Keeps one
# generation of history (.1) so post-mortem grep still works on recent past.
if [[ -f "$RUNS_LOG" ]]; then
  size=$(stat -f %z "$RUNS_LOG" 2>/dev/null || echo 0)
  if (( size > RUNS_LOG_MAX_BYTES )); then
    log "rotating runs.jsonl (${size} bytes > ${RUNS_LOG_MAX_BYTES})"
    rm -f "${RUNS_LOG}.1" 2>/dev/null || true
    mv "$RUNS_LOG" "${RUNS_LOG}.1" 2>/dev/null || true
    : > "$RUNS_LOG"
  fi
fi

# Acquire the worker lock. Writes our PID into the lockdir so a stale lock
# left by a killed worker can be detected and reclaimed.
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo "$$" > "$LOCKDIR/pid"
    return 0
  fi
  local holder
  holder=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
    log "another worker instance is running (pid $holder); exiting"
    return 1
  fi
  # Holder is dead or unknown — reclaim. Lock-dir mtime helps diagnose.
  local age="?"
  age=$(stat -f %m "$LOCKDIR" 2>/dev/null || echo "")
  log "stale lock detected (holder pid=${holder:-unknown}, lock mtime=${age}); reclaiming"
  rm -rf "$LOCKDIR"
  if mkdir "$LOCKDIR" 2>/dev/null; then
    echo "$$" > "$LOCKDIR/pid"
    return 0
  fi
  log "failed to reclaim lock after stale-detection (lost race?); exiting"
  return 1
}

if [[ -e "$PAUSED_FLAG" ]]; then
  log "watcher paused; exiting"
  exit 0
fi

if ! acquire_lock; then
  exit 0
fi
trap 'rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT

# Sweep stale files left in .staged/ from a previous run. With the lock
# held we know no other worker is touching .staged/, so anything here is
# an orphan (e.g. a helper script Claude wrote and didn't clean up).
stale_in_staged=$(find "$INBOX/.staged" -mindepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')
if (( stale_in_staged > 0 )); then
  log "found $stale_in_staged stale file(s) in .staged/; cleaning up"
  find "$INBOX/.staged" -mindepth 1 -type f -delete 2>/dev/null || true
fi

NOW=$(date +%s)
processed_any=0

shopt -s nullglob
for path in "$INBOX"/*; do
  [[ -f "$path" ]] || continue
  base=$(basename "$path")
  [[ "$base" == .* ]] && continue

  mtime=$(stat -f %m "$path")
  age=$((NOW - mtime))
  if (( age < 5 )); then
    log "skipping '$base' (mtime age ${age}s)"
    continue
  fi

  processed_any=1

  # --- Dedup check ----------------------------------------------------------
  dedup_result="$(check_dedup "$path" || true)"
  if [[ -n "$dedup_result" ]]; then
    matched_path="${dedup_result%%$'\t'*}"
    matched_reason="${dedup_result##*$'\t'}"

    # Backfill: matched summary is missing its source artifact and the input is
    # a PDF that hash/URL/title-matches it. File the PDF in place of running
    # the full intake — the summary is already correct.
    if [[ "$matched_reason" == backfill:* ]]; then
      backfill_kind="${matched_reason#backfill:}"
      summary_dir=$(dirname "$matched_path")
      summary_base=$(basename "$matched_path")
      summary_slug="${summary_base%.summary.md}"
      pdf_target="$summary_dir/$summary_slug.pdf"
      if [[ -e "$pdf_target" ]]; then
        # Race: PDF appeared since check_dedup looked. Leave the input in
        # the inbox; next tick re-evaluates against the updated library state.
        log "backfill skipped: $pdf_target already exists; leaving '$base' in inbox for next tick"
        continue
      fi
      if ! mv "$path" "$pdf_target"; then
        log "backfill failed: could not move '$base' to $pdf_target; leaving in inbox"
        continue
      fi
      log "backfilled missing PDF for '$base' (matched by $backfill_kind) -> $pdf_target"
      # Fuzzy matches don't carry a verified hash on the summary, so inject
      # the filed PDF's hash to anchor future dedup. Hash/URL backfills
      # already have the right value in frontmatter (it's how they matched).
      if [[ "$backfill_kind" == fuzzy ]]; then
        pdf_hash=$(file_sha256 "$pdf_target")
        inject_hash "$matched_path" "$pdf_hash" || true
      fi
      bf_paths_file=$(mktemp -t intake-bf-paths)
      printf '%s\n' "$matched_path" > "$bf_paths_file"
      append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "backfill" "$bf_paths_file" "" ""
      rm -f "$bf_paths_file"
      continue
    fi

    log "duplicate '$base' (matched by $matched_reason): $matched_path"
    dup_dest="$INBOX/_duplicate/$base"
    n=1
    while [[ -e "$dup_dest" ]]; do
      if [[ "$base" == *.* ]]; then
        dup_dest="$INBOX/_duplicate/${base%.*}.${n}.${base##*.}"
      else
        dup_dest="$INBOX/_duplicate/${base}.${n}"
      fi
      n=$((n+1))
    done
    mv "$path" "$dup_dest"
    cat > "${dup_dest}.log" <<EOF
Duplicate of: $matched_path
Matched by: $matched_reason
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

This file was not processed because an existing summary in the library
already references the same source ($matched_reason match). To force
re-processing, delete the existing summary first, then move this file
back to the inbox.
EOF
    # Append a duplicate entry to runs.jsonl
    dup_paths_file=$(mktemp -t intake-dup-paths)
    printf '%s\n' "$matched_path" > "$dup_paths_file"
    dup_err_file=$(mktemp -t intake-dup-err)
    printf 'duplicate of %s (matched by %s)\n' "$matched_path" "$matched_reason" > "$dup_err_file"
    append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "duplicate" "$dup_paths_file" "$dup_err_file" ""
    rm -f "$dup_paths_file" "$dup_err_file"
    continue
  fi

  log "processing '$base'"

  uuid=$(uuidgen | tr '[:upper:]' '[:lower:]')
  staged_path="$INBOX/.staged/${uuid}-${base}"
  if ! mv "$path" "$staged_path" 2>/dev/null; then
    log "  failed to stage '$base' (already claimed?)"
    continue
  fi

  sentinel=$(mktemp -t intake-sentinel)
  touch "$sentinel"
  run_log="$CONFIG/current-run.log"
  : > "$run_log"
  printf '{"type":"meta","kind":"start","ts":"%s","input":"%s"}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" >> "$run_log"
  start_ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  prompt=$("$PYTHON" -c '
import os, sys
template = open(sys.argv[1]).read()
domain = os.environ.get("DOMAIN", "A general-purpose personal research library.")
print(template.replace("<STAGED_PATH>", sys.argv[2]).replace("<DOMAIN>", domain), end="")
' "$PROMPT_FILE" "$staged_path")

  # Run claude with a wall-clock timeout AND a retry loop. The watchdog
  # subshell SIGTERMs claude after CLAUDE_TIMEOUT seconds, then SIGKILLs
  # if it doesn't respond. Exit codes mapped to: 124 = our timeout,
  # 127 = binary missing (no point retrying), anything else = retryable.
  # Deny rules block writes into .staged/ — Claude needs Read access
  # (granted via --add-dir) but the staging dir is a managed queue.
  # The // prefix marks absolute paths in Claude Code's permission DSL;
  # $INBOX starts with /, so "/${INBOX}" yields "//Users/...".
  attempt=0
  claude_exit=0
  while :; do
    attempt=$((attempt + 1))
    printf '{"type":"meta","kind":"attempt","ts":"%s","attempt":%d}\n' \
      "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$attempt" >> "$run_log"

    set +e
    {
      cd "$LIBRARY"
      exec "$CLAUDE" -p "$prompt" \
        --model "$MODEL" \
        --permission-mode acceptEdits \
        --add-dir "$LIBRARY" \
        --add-dir "$INBOX/.staged" \
        --disallowed-tools \
          "Write(/${INBOX}/.staged/**)" \
          "Edit(/${INBOX}/.staged/**)" \
          "NotebookEdit(/${INBOX}/.staged/**)" \
        --output-format stream-json \
        --verbose
    } >>"$run_log" 2>&1 &
    claude_pid=$!

    (
      sleep "$CLAUDE_TIMEOUT"
      if kill -0 "$claude_pid" 2>/dev/null; then
        kill -TERM "$claude_pid" 2>/dev/null || true
        sleep 3
        kill -KILL "$claude_pid" 2>/dev/null || true
        printf '{"type":"meta","kind":"timeout","ts":"%s","after_s":%d}\n' \
          "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$CLAUDE_TIMEOUT" >> "$run_log"
      fi
    ) &
    watchdog_pid=$!

    wait "$claude_pid"
    claude_exit=$?
    set -e

    # Stop the watchdog if claude finished naturally.
    kill "$watchdog_pid" 2>/dev/null || true
    wait "$watchdog_pid" 2>/dev/null || true

    # If claude was killed by the watchdog the exit code is 143 (SIGTERM)
    # or 137 (SIGKILL); normalize to 124 so the runs log reads cleanly.
    if (( claude_exit == 143 || claude_exit == 137 )); then
      claude_exit=124
    fi

    if (( claude_exit == 0 )); then
      break
    fi
    if (( claude_exit == 127 )); then
      log "  claude binary missing (exit 127); not retrying"
      break
    fi
    if (( attempt > MAX_RETRIES )); then
      break
    fi
    log "  attempt $attempt failed (exit $claude_exit); retrying in ${RETRY_BACKOFF}s"
    sleep "$RETRY_BACKOFF"
  done

  printf '{"type":"meta","kind":"end","ts":"%s","exit":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$claude_exit" >> "$run_log"
  cp "$run_log" "$CONFIG/last-run.log"

  produced_list=$(mktemp -t intake-produced)
  find "$LIBRARY" -type f -name '*.summary.md' -newer "$sentinel" > "$produced_list" 2>/dev/null || true
  num_produced=$(wc -l < "$produced_list" | tr -d ' ')

  if (( claude_exit == 0 )) && (( num_produced > 0 )); then
    log "  success (claude exit 0, $num_produced summary file(s) produced)"
    # Inject source_hash into each produced summary so future drops dedup.
    # Also ensure the original input is filed alongside the summary as
    # <slug>.pdf — the source-intake skill is supposed to copy it, but isn't
    # always registered for headless runs, in which case the agent's manual
    # fallback can forget the copy step. Belt-and-suspenders here.
    input_hash=$(file_sha256 "$staged_path")
    is_pdf=0
    [[ "$base" == *.[Pp][Dd][Ff] ]] && is_pdf=1
    while IFS= read -r produced_path; do
      [[ -n "$produced_path" ]] || continue
      if inject_hash "$produced_path" "$input_hash"; then
        log "  injected source_hash into $produced_path"
      fi
      if (( is_pdf )); then
        folder=$(dirname "$produced_path")
        slug=$(basename "$produced_path" .summary.md)
        pdf_target="$folder/$slug.pdf"
        if [[ ! -f "$pdf_target" ]]; then
          if cp "$staged_path" "$pdf_target"; then
            log "  filed PDF -> $pdf_target"
          else
            log "  WARNING: failed to copy PDF to $pdf_target"
          fi
        fi
      fi
    done < "$produced_list"
    rm -f "$staged_path"
    append_run "$start_ts" "$base" "success" "$produced_list" "" "$run_log"
    if "$PYTHON" "$HOME/Library/Scripts/claude-source-intake-regen-index.py" 2>&1; then
      log "  INDEX.md regenerated"
    else
      log "  INDEX.md regen failed (non-fatal)"
    fi
  else
    log "  failure (claude exit $claude_exit, $num_produced summary file(s) produced)"
    fail_dest="$INBOX/_failed/$base"
    n=1
    while [[ -e "$fail_dest" ]]; do
      if [[ "$base" == *.* ]]; then
        fail_dest="$INBOX/_failed/${base%.*}.${n}.${base##*.}"
      else
        fail_dest="$INBOX/_failed/${base}.${n}"
      fi
      n=$((n+1))
    done
    mv "$staged_path" "$fail_dest"
    cp "$run_log" "${fail_dest}.log"
    append_run "$start_ts" "$base" "failure" "$produced_list" "$run_log" "$run_log"
  fi

  # Archive this iteration's run log under a unique name so a multi-file
  # tick doesn't lose the middle iterations. current-run.log / last-run.log
  # still get clobbered as before — they're the dashboard's live-tail and
  # most-recent-replay slots — but run-logs/ keeps the last RUN_LOG_KEEP.
  archive_ts=$(date -u '+%Y%m%dT%H%M%SZ')
  # Sanitize base for safe filename use: replace anything not alnum/._- with _
  safe_base=$(printf '%s' "$base" | tr -c 'A-Za-z0-9._-' '_')
  cp "$run_log" "$RUN_LOGS_DIR/${archive_ts}-${safe_base}.log" 2>/dev/null || true
  # Prune to the most recent RUN_LOG_KEEP archives. Names are timestamp-prefixed
  # so they sort lexicographically by recency.
  ( cd "$RUN_LOGS_DIR" && ls -1 *.log 2>/dev/null | sort -r | awk -v k="$RUN_LOG_KEEP" 'NR > k' | while IFS= read -r f; do rm -f -- "$f"; done ) || true

  # Sweep any files Claude wrote into .staged/ during this iteration.
  # The iteration's own staged input is already gone (success: rm'd above;
  # failure: mv'd to _failed/), so anything still here is an artifact
  # (helper scripts, scratch files, etc.) that would otherwise show up
  # as a stuck job in the dashboard.
  find "$INBOX/.staged" -mindepth 1 -type f -delete 2>/dev/null || true

  rm -f "$produced_list" "$sentinel"
done

if (( processed_any == 0 )); then
  log "no files to process"
fi
