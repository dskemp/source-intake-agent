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
# Preprint promotion: how to handle PDFs that look like the published version
# of a tracked preprint. auto = archive the preprint summary and intake the
# published PDF into its category slot. stage = route the PDF to
# _promoted/_pending/ for manual review. off = skip detection entirely.
PREPRINT_PROMOTION_MODE="${PREPRINT_PROMOTION_MODE:-auto}"

mkdir -p "$INBOX/.staged" "$INBOX/_failed" "$INBOX/_duplicate" "$INBOX/_promoted" "$CONFIG" "$RUN_LOGS_DIR"

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
from urllib.parse import urlsplit, urlunsplit
import yaml

library, input_path = sys.argv[1:3]
input_path = Path(input_path)


def canon_url(u):
    """Conservative URL normalization so trivially-different forms of the
    same address still dedup: lowercase scheme/host, drop the fragment and
    common tracking params, strip a trailing slash. Anything non-http(s)
    is compared as-is."""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return u
    query = "&".join(
        q for q in parts.query.split("&")
        if q and not q.lower().startswith(("utm_", "fbclid=", "gclid=", "mc_cid=", "mc_eid="))
    )
    return urlunsplit((scheme, parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def read_frontmatter_text(p):
    """Read enough of a summary to cover its frontmatter without slurping
    the whole body; the dedup pass only needs the YAML block. Falls back
    to a full read in the (unlikely) case frontmatter exceeds the window."""
    try:
        with open(p, errors="replace") as f:
            head = f.read(65536)
    except Exception:
        return None
    if not head.startswith("---\n"):
        return None
    if head.find("\n---\n", 4) == -1 and len(head) == 65536:
        try:
            head = p.read_text(errors="replace")
        except Exception:
            return None
    return head

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
input_url_canon = canon_url(input_url)

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
    text = read_frontmatter_text(summary)
    if text is None:
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
    if input_url_canon and canon_url(fm.get('url') or '') == input_url_canon:
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
if not text.startswith('---\n'):
    sys.exit(1)
end = text.find('\n---\n', 4)
if end == -1:
    sys.exit(1)
# Scope the already-present check to the frontmatter block — a summary whose
# BODY merely mentions the literal string must still get its hash injected.
if 'source_hash:' in text[:end + 1]:
    sys.exit(0)
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
                # A retried run appends one result event per attempt; sum
                # them so cost/duration reflect what was actually spent.
                if e.get("type") == "result":
                    c = e.get("total_cost_usd") or e.get("cost_usd")
                    if c is not None:
                        cost_usd = (cost_usd or 0) + c
                    d = e.get("duration_ms")
                    if d is not None:
                        duration_ms = (duration_ms or 0) + d
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

# Rotate runs.jsonl if it has grown past the configured ceiling. Keeps one
# generation of history (.1) so post-mortem grep still works on recent past.
# Done under the lock so two near-simultaneous invocations can't race the mv.
if [[ -f "$RUNS_LOG" ]]; then
  size=$(stat -f %z "$RUNS_LOG" 2>/dev/null || echo 0)
  if (( size > RUNS_LOG_MAX_BYTES )); then
    log "rotating runs.jsonl (${size} bytes > ${RUNS_LOG_MAX_BYTES})"
    rm -f "${RUNS_LOG}.1" 2>/dev/null || true
    mv "$RUNS_LOG" "${RUNS_LOG}.1" 2>/dev/null || true
    : > "$RUNS_LOG"
  fi
fi

# Same treatment for promotion.log (detect-promotion's stderr), which is
# append-only and would otherwise grow without bound.
PROMOTION_LOG="$CONFIG/promotion.log"
if [[ -f "$PROMOTION_LOG" ]]; then
  psize=$(stat -f %z "$PROMOTION_LOG" 2>/dev/null || echo 0)
  if (( psize > 1048576 )); then
    log "rotating promotion.log (${psize} bytes)"
    mv -f "$PROMOTION_LOG" "${PROMOTION_LOG}.1" 2>/dev/null || true
  fi
fi

shopt -s nullglob

# Sweep .staged/ leftovers from a previous run. With the lock held we know no
# other worker is touching .staged/. A UUID-prefixed file is the staged input
# of a run that died mid-flight (reboot, kill -9 — the EXIT trap never fired);
# between staging and cleanup it is the ONLY copy of the user's file, so move
# it back to the inbox for reprocessing rather than deleting it. Anything
# without the UUID prefix is an orphaned artifact and is safe to remove.
UUID_PREFIX_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-'
for stale in "$INBOX/.staged"/*; do
  [[ -f "$stale" ]] || continue
  sb=$(basename "$stale")
  if [[ "$sb" =~ $UUID_PREFIX_RE ]]; then
    orig="${sb:37}"   # strip "<uuid>-" (36 uuid chars + 1 hyphen)
    dest="$INBOX/$orig"
    n=1
    while [[ -e "$dest" ]]; do
      if [[ "$orig" == *.* ]]; then
        dest="$INBOX/${orig%.*}.${n}.${orig##*.}"
      else
        dest="$INBOX/${orig}.${n}"
      fi
      n=$((n+1))
    done
    if mv "$stale" "$dest" 2>/dev/null; then
      log "recovered staged input from interrupted run: $sb -> $(basename "$dest")"
    else
      log "WARNING: could not recover staged input $sb"
    fi
  else
    log "deleting stray artifact in .staged/: $sb"
    rm -f "$stale" 2>/dev/null || true
  fi
done

NOW=$(date +%s)
processed_any=0
for path in "$INBOX"/*; do
  [[ -f "$path" ]] || continue
  base=$(basename "$path")
  [[ "$base" == .* ]] && continue

  # File-too-new guard: a file still being written has a recent mtime, and
  # processing it mid-copy yields a truncated read. We can't just skip — the
  # launchd plist uses WatchPaths only (no StartInterval), so if we exit here
  # nothing re-fires and the file stays stuck. Wait until the file ages past
  # the threshold, then re-check mtime to confirm it's no longer being
  # written. If mtime moved during the wait the writer is still active, so
  # punt (next directory event will retrigger us).
  mtime=$(stat -f %m "$path")
  age=$(( $(date +%s) - mtime ))
  if (( age < 5 )); then
    wait_s=$(( 5 - age + 1 ))
    log "waiting ${wait_s}s for '$base' to settle (mtime age ${age}s)"
    sleep "$wait_s"
    new_mtime=$(stat -f %m "$path" 2>/dev/null || echo 0)
    if [[ "$new_mtime" != "$mtime" ]]; then
      log "skipping '$base' (still being written; mtime advanced during wait)"
      continue
    fi
  fi

  processed_any=1

  # Per-iteration promotion state. is_promotion=1 means the rest of this loop
  # should run normal intake but post-process the produced summary into the
  # archived preprint's category folder (see "promote" branch below).
  is_promotion=0
  promote_target_dir=""
  promote_category_dir=""
  promote_preprint_rel=""

  # --- Dedup check ----------------------------------------------------------
  dedup_result="$(check_dedup "$path" || true)"
  # Preprint promotion: if standard dedup didn't fire and this is a PDF, ask
  # detect-promotion.py whether the input looks like the published version of
  # a tracked preprint. Returns "PROMOTE:<library-rel-path>" or nothing.
  if [[ -z "$dedup_result" ]] && [[ "$PREPRINT_PROMOTION_MODE" != "off" ]] && [[ "$base" == *.[Pp][Dd][Ff] ]]; then
    # In a deployed install, this script lives at $SCRIPTS_DIR alongside
    # claude-source-intake-detect-promotion.py. In the repo, it's
    # scripts/detect-promotion.py. Try both.
    promote_helper=""
    for cand in \
      "$(dirname "$0")/claude-source-intake-detect-promotion.py" \
      "$(dirname "$0")/detect-promotion.py"; do
      [[ -f "$cand" ]] && { promote_helper="$cand"; break; }
    done
    if [[ -n "$promote_helper" ]]; then
      promote_line=$(LIBRARY_PATH="$LIBRARY" PREPRINT_CONFIG="$CONFIG" \
        "$PYTHON" "$promote_helper" "$path" 2>>"$CONFIG/promotion.log" || true)
    else
      promote_line=""
    fi
    if [[ "$promote_line" == PROMOTE:* ]]; then
      promote_rel="${promote_line#PROMOTE:}"
      dedup_result="$LIBRARY/$promote_rel"$'\t'"promote"
    fi
  fi
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

    # Promote: input PDF looks like the published version of a tracked
    # preprint summary. In stage mode, route the PDF to _promoted/_pending/
    # for manual review. In auto mode, archive the preprint summary + its
    # source artifacts and fall through to normal intake; post-success the
    # produced summary is moved into the preprint's category slot.
    if [[ "$matched_reason" == "promote" ]]; then
      preprint_summary="$matched_path"
      preprint_dir=$(dirname "$preprint_summary")
      preprint_slug=$(basename "$preprint_summary" .summary.md)
      preprint_rel="${preprint_summary#"$LIBRARY/"}"

      if [[ "$PREPRINT_PROMOTION_MODE" == "stage" ]]; then
        pend_dir="$INBOX/_promoted/_pending"
        mkdir -p "$pend_dir"
        pend_dest="$pend_dir/$base"
        n=1
        while [[ -e "$pend_dest" ]]; do
          if [[ "$base" == *.* ]]; then
            pend_dest="$pend_dir/${base%.*}.${n}.${base##*.}"
          else
            pend_dest="$pend_dir/${base}.${n}"
          fi
          n=$((n+1))
        done
        mv "$path" "$pend_dest"
        cat > "${pend_dest}.log" <<EOF
Promotion candidate for: $preprint_summary
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
PREPRINT_PROMOTION_MODE=stage (no automatic action taken).

To accept: delete the preprint summary + its sidecar PDF/snapshot, then move
this file back into the inbox so it intakes into the same category folder.
To reject: delete this file.
EOF
        log "promote-staged '$base' (preprint: $preprint_summary)"
        stage_paths_file=$(mktemp -t intake-stage-paths)
        printf '%s\n' "$preprint_summary" > "$stage_paths_file"
        append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "promote-staged" "$stage_paths_file" "" ""
        rm -f "$stage_paths_file"
        continue
      fi

      # auto mode: archive the preprint and let normal intake handle the PDF.
      archive_ts=$(date -u '+%Y%m%dT%H%M%SZ')
      archive_dir="$INBOX/_promoted/${archive_ts}-${preprint_slug}"
      mkdir -p "$archive_dir"
      mv "$preprint_summary" "$archive_dir/" 2>/dev/null || true
      [[ -f "$preprint_dir/$preprint_slug.pdf" ]] && \
        mv "$preprint_dir/$preprint_slug.pdf" "$archive_dir/" 2>/dev/null || true
      [[ -f "$preprint_dir/$preprint_slug.snapshot.md" ]] && \
        mv "$preprint_dir/$preprint_slug.snapshot.md" "$archive_dir/" 2>/dev/null || true
      cat > "$archive_dir/promotion.txt" <<EOF
Preprint archived during promotion to its published version.
Original library path: $preprint_summary
Promoted by inbox file: $base
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

The published PDF is being intaked in this tick. The produced summary will
land in $preprint_dir (the preprint's original category folder).
EOF
      # Remove the preprint's cache entry so /preprints no longer lists it.
      "$PYTHON" - "$CONFIG/preprint-checks.json" "$preprint_rel" <<'PY' || true
import json, sys
from pathlib import Path
cache_path, rel = sys.argv[1], sys.argv[2]
p = Path(cache_path)
if not p.exists():
    sys.exit(0)
try:
    cache = json.loads(p.read_text())
except Exception:
    sys.exit(0)
if rel in cache:
    del cache[rel]
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))
PY
      log "promoting '$base' over preprint $preprint_summary (archived to $archive_dir)"
      is_promotion=1
      promote_target_dir="$preprint_dir"
      # The produced summary's directory will be renamed to match the published
      # slug (canonical winner), so we relocate into <category>/<produced-slug>/
      # rather than into the preprint's own slug dir. promote_target_dir is
      # retained for the failure-restore path below.
      promote_category_dir="$(dirname "$preprint_dir")"
      promote_preprint_rel="$preprint_rel"
      # Fall through to normal intake below — do NOT `continue`.
    else
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
  fi

  log "processing '$base'"

  uuid=$(uuidgen | tr '[:upper:]' '[:lower:]')
  staged_path="$INBOX/.staged/${uuid}-${base}"
  if ! mv "$path" "$staged_path" 2>/dev/null; then
    log "  failed to stage '$base' (already claimed?)"
    continue
  fi
  # Bump mtime to "processing start" — mv preserves the source file's mtime,
  # so without this the dashboard's elapsed-time ticker (derived from the
  # staged file's mtime) reports "time since the PDF was downloaded" instead
  # of "time spent processing this job."
  touch "$staged_path"

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
  # WebFetch is allowlisted explicitly: acceptEdits only auto-approves file
  # edits, and in headless -p mode an unanswerable permission prompt is a
  # denial — without this, .txt/.url (web source) intake silently fails.
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
        --allowed-tools "WebFetch" \
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
    # Promotion: the preprint's slug dir was emptied of files at detect-time.
    # Remove it now, BEFORE relocating the produced folder — a published
    # version that re-uses the preprint's slug would otherwise collide with
    # the leftover empty dir and the relocation would be skipped.
    if (( is_promotion )) && [[ -d "$promote_target_dir" ]]; then
      rmdir "$promote_target_dir" 2>/dev/null || true
    fi
    # Inject source_hash into each produced summary so future drops dedup.
    # Also ensure the original input is filed alongside the summary — as
    # <slug>.pdf for PDF inputs, <slug>.snapshot.md for .md/.html inputs.
    # The prompt asks the agent to file it, but agents occasionally skip
    # that step, and the staged copy is deleted below; without this
    # backstop the original would be lost. Belt-and-suspenders here.
    input_hash=$(file_sha256 "$staged_path")
    is_pdf=0
    is_doc=0
    case "$base" in
      *.[Pp][Dd][Ff]) is_pdf=1 ;;
      *.[Mm][Dd]|*.[Hh][Tt][Mm][Ll]|*.[Hh][Tt][Mm]) is_doc=1 ;;
    esac
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
      if (( is_doc )); then
        folder=$(dirname "$produced_path")
        slug=$(basename "$produced_path" .summary.md)
        snap_target="$folder/$slug.snapshot.md"
        if [[ ! -f "$snap_target" ]]; then
          if cp "$staged_path" "$snap_target"; then
            log "  filed snapshot -> $snap_target"
          else
            log "  WARNING: failed to copy snapshot to $snap_target"
          fi
        fi
      fi
      # Promotion: relocate the entire produced folder into the preprint's
      # category as <category>/<produced-slug>/ so the directory name matches
      # the produced summary's filename (slug == dirname invariant). We move
      # the folder as a unit, which carries the summary + PDF (+ snapshot if
      # any) along atomically. The original preprint's empty slug dir is
      # rmdir'd after the loop completes.
      if (( is_promotion )) && [[ -n "$promote_category_dir" ]]; then
        produced_folder=$(dirname "$produced_path")
        produced_slug=$(basename "$produced_path" .summary.md)
        final_dir="$promote_category_dir/$produced_slug"
        if [[ "$produced_folder" != "$final_dir" ]]; then
          if [[ -e "$final_dir" ]]; then
            # Should not happen: dedup would have fired if the published slug
            # already had a slot. Log and leave the produced folder in place
            # rather than corrupting the existing dir with a nested move.
            log "  WARNING: cannot promote into $final_dir (already exists); leaving in $produced_folder"
          else
            mkdir -p "$promote_category_dir"
            if mv "$produced_folder" "$final_dir" 2>/dev/null; then
              log "  promoted folder -> $final_dir"
              new_produced_path="$final_dir/$produced_slug.summary.md"
              # Update produced_list so append_run logs the final location.
              sed -i.bak "s|^${produced_path}$|${new_produced_path}|" "$produced_list" 2>/dev/null && rm -f "${produced_list}.bak"
            else
              log "  WARNING: failed to move $produced_folder to $final_dir"
            fi
          fi
        fi
      fi
    done < "$produced_list"
    # Promotion cleanup: the preprint's original slug dir was emptied at
    # detect-time (summary/PDF/snapshot moved to _promoted/) and the produced
    # folder has now been relocated to <category>/<produced-slug>/. rmdir the
    # leftover empty preprint dir so the library doesn't accumulate orphans.
    if (( is_promotion )) && [[ -n "$promote_target_dir" ]] && [[ -d "$promote_target_dir" ]]; then
      if rmdir "$promote_target_dir" 2>/dev/null; then
        log "  cleaned up empty preprint dir: $promote_target_dir"
      else
        log "  NOTE: $promote_target_dir not empty after promotion; leaving in place"
      fi
    fi
    rm -f "$staged_path"
    outcome="success"
    (( is_promotion )) && outcome="promoted"
    append_run "$start_ts" "$base" "$outcome" "$produced_list" "" "$run_log"
    if "$PYTHON" "$HOME/Library/Scripts/claude-source-intake-regen-index.py" 2>&1; then
      log "  INDEX.md regenerated"
    else
      log "  INDEX.md regen failed (non-fatal)"
    fi
  else
    log "  failure (claude exit $claude_exit, $num_produced summary file(s) produced)"
    # Quarantine partial output: claude may have written summaries before the
    # run failed (e.g. watchdog timeout). Left in the library they'd be
    # unindexed, carry no source_hash, and their url: could dedup-block a
    # later retry of the same input. Only quarantine folders CREATED by this
    # run (birthtime >= the run sentinel) — a pre-existing folder that merely
    # changed is the user's data and stays put.
    if (( num_produced > 0 )); then
      sentinel_ts=$(stat -f %m "$sentinel" 2>/dev/null || echo 0)
      partial_root="$INBOX/_failed/_partial"
      while IFS= read -r produced_path; do
        [[ -n "$produced_path" ]] || continue
        pfolder=$(dirname "$produced_path")
        [[ -d "$pfolder" ]] || continue
        birth=$(stat -f %B "$pfolder" 2>/dev/null || echo 0)
        if (( birth >= sentinel_ts )); then
          mkdir -p "$partial_root"
          pdest="$partial_root/$(basename "$pfolder")"
          n=1
          while [[ -e "$pdest" ]]; do
            pdest="$partial_root/$(basename "$pfolder").$n"
            n=$((n+1))
          done
          if mv "$pfolder" "$pdest" 2>/dev/null; then
            log "  quarantined partial output: $pfolder -> $pdest"
            new_produced_path="$pdest/$(basename "$produced_path")"
            sed -i.bak "s|^${produced_path}$|${new_produced_path}|" "$produced_list" 2>/dev/null && rm -f "${produced_list}.bak"
          fi
        else
          log "  NOTE: $produced_path changed during the failed run but its folder predates it; leaving in place"
        fi
      done < "$produced_list"
    fi
    # Promotion safety net: if we archived a preprint summary in this tick but
    # the intake then failed, restore the preprint so the library isn't left
    # with a hole. The promoted PDF still goes to _failed/ for retry.
    if (( is_promotion )) && [[ -d "$archive_dir" ]] && [[ -n "$promote_target_dir" ]]; then
      mkdir -p "$promote_target_dir"
      restored=0
      for f in "$archive_dir"/*; do
        bn=$(basename "$f")
        [[ "$bn" == "promotion.txt" ]] && continue
        if mv "$f" "$promote_target_dir/" 2>/dev/null; then
          restored=1
        fi
      done
      if (( restored )); then
        log "  restored archived preprint from $archive_dir -> $promote_target_dir (intake failed)"
        # Append a marker explaining why the archive dir is now empty/partial.
        printf '\nNOTE: intake of the promoted PDF failed at %s; preprint files were restored to %s.\n' \
          "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$promote_target_dir" >> "$archive_dir/promotion.txt" 2>/dev/null || true
      fi
    fi
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
