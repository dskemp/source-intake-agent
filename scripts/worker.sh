#!/bin/bash
set -euo pipefail

INBOX="${INBOX_PATH:-$HOME/source-library-inbox}"
LIBRARY="${LIBRARY_PATH:-$HOME/source-library}"
CONFIG="$HOME/.config/claude-source-intake"
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo $HOME/.local/bin/claude)}"
PYTHON="$CONFIG/venv/bin/python"

LOCKDIR="/tmp/claude-source-intake.lock"
PROMPT_FILE="$CONFIG/prompt.txt"
PAUSED_FLAG="$CONFIG/paused"
RUNS_LOG="$CONFIG/runs.jsonl"
ENV_FILE="$CONFIG/env"

mkdir -p "$INBOX/.staged" "$INBOX/_failed" "$INBOX/_duplicate" "$CONFIG"

# launchd-spawned processes don't inherit your shell's env or interactive
# Claude session vars. Put ANTHROPIC_API_KEY (and any other auth/config
# claude needs) in ~/.config/claude-source-intake/env - mode 0600.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
  cat > "$PROMPT_FILE" <<PROMPT_EOF
Process the source at <STAGED_PATH> autonomously and add it to the library
at $LIBRARY/, following the source-intake skill.

You are running headlessly with no human in the loop. Do NOT ask any
clarifying questions. Make best-judgment calls based on:
  - the source content itself (PDF text / URL fetch / markdown);
  - existing summaries in the library as exemplars of tone, depth, tags,
    and the kinds of "Relevance" connections I tend to draw;
  - the existing category folders - prefer an existing category over
    proposing a new one.

For the Relevance section: infer 2-4 plausible connections from the
source's themes and the existing library's coverage. Be specific but
hedged ("plausibly relevant to..."). If you genuinely can't infer one,
write a single sentence noting the source's likely use case.

If the input is a .txt or .url file containing a URL, fetch the URL
and treat it as a web source (snapshot + summary). If it's a .md or
.html file, treat it as the snapshot directly. If it's a .pdf, treat
it as the primary document.

After filing, also update INDEX.md and CHANGELOG.md per the skill's
normal behavior. Do not delete the input file - the wrapper script
handles cleanup.
PROMPT_EOF
fi

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# Dedup check. Echoes "<matched-summary-path>\t<reason>" if input duplicates an
# existing source (by sha256 of bytes, or by URL for .txt/.url inputs).
# Empty output = not a duplicate.
check_dedup() {
  local input_path="$1"
  "$PYTHON" - "$LIBRARY" "$input_path" <<'PY'
import hashlib, re, sys
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
        print(f"{summary}\thash")
        sys.exit(0)
    if input_url and fm.get('url') == input_url:
        print(f"{summary}\turl")
        sys.exit(0)
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

if [[ -e "$PAUSED_FLAG" ]]; then
  log "watcher paused; exiting"
  exit 0
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "another worker instance is running; exiting"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

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
import sys
template = open(sys.argv[1]).read()
print(template.replace("<STAGED_PATH>", sys.argv[2]), end="")
' "$PROMPT_FILE" "$staged_path")

  set +e
  (
    cd "$LIBRARY"
    "$CLAUDE" -p "$prompt" \
      --model claude-sonnet-4-6 \
      --permission-mode acceptEdits \
      --add-dir "$LIBRARY" \
      --add-dir "$INBOX/.staged" \
      --output-format stream-json \
      --verbose
  ) >>"$run_log" 2>&1
  claude_exit=$?
  set -e

  printf '{"type":"meta","kind":"end","ts":"%s","exit":%d}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$claude_exit" >> "$run_log"
  cp "$run_log" "$CONFIG/last-run.log"

  produced_list=$(mktemp -t intake-produced)
  find "$LIBRARY" -type f -name '*.summary.md' -newer "$sentinel" > "$produced_list" 2>/dev/null || true
  num_produced=$(wc -l < "$produced_list" | tr -d ' ')

  if (( claude_exit == 0 )) && (( num_produced > 0 )); then
    log "  success (claude exit 0, $num_produced summary file(s) produced)"
    # Inject source_hash into each produced summary so future drops dedup.
    input_hash=$(file_sha256 "$staged_path")
    while IFS= read -r produced_path; do
      [[ -n "$produced_path" ]] || continue
      if inject_hash "$produced_path" "$input_hash"; then
        log "  injected source_hash into $produced_path"
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

  rm -f "$produced_list" "$sentinel"
done

if (( processed_any == 0 )); then
  log "no files to process"
fi
