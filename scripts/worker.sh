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
# No fallback defaults here: guessing a path would silently create and
# process against a directory tree the user never configured.
if [[ -z "${INBOX_PATH:-}" || -z "${LIBRARY_PATH:-}" ]]; then
  echo "ERROR: INBOX_PATH and LIBRARY_PATH must be set." >&2
  echo "launchd provides them via the installed plist; for a manual run," >&2
  echo "export them first or set them in $ENV_FILE." >&2
  exit 1
fi
INBOX="$INBOX_PATH"
LIBRARY="$LIBRARY_PATH"
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
# Fetching candidate sources. Many hosts (CourtListener, americanbar.org, and
# other WAF-fronted sites) 403 the bare "curl/x.y" User-Agent but serve a
# normal 200 to a browser UA, so we send one by default. FETCH_MIN_BYTES is the
# smallest body we accept as a real document: a curl that "succeeds" with HTTP
# 2xx but an empty/near-empty body (e.g. EUR-Lex answering an async request with
# HTTP 202 + 0 bytes) must be treated as a fetch failure, not filed as content.
FETCH_USER_AGENT="${FETCH_USER_AGENT:-Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36}"
FETCH_MIN_BYTES="${FETCH_MIN_BYTES:-1}"          # reject empty bodies; bump to e.g. 512 to also drop tiny challenge pages

mkdir -p "$INBOX/.staged" "$INBOX/_failed" "$INBOX/_duplicate" "$INBOX/_promoted" "$INBOX/_notes" "$CONFIG" "$RUN_LOGS_DIR"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "ERROR: prompt file missing at $PROMPT_FILE" >&2
  echo "Run install.sh from the source-intake-agent repo to restore it." >&2
  exit 1
fi

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# fetch_url <url> <dest>: download <url> into <dest>. Returns 0 only when curl
# succeeded AND the body is a plausibly-real document. Returns non-zero on a
# transport/HTTP error (curl -f already fails on >=400) OR on an empty/too-small
# body. The size guard is the important half: curl -f treats any 2xx — including
# HTTP 202 "Accepted, come back later" (EUR-Lex's async delivery) — as success,
# so without it a 0-byte download was filed as "fetched" and then poisoned the
# drain loop (the intake agent correctly refuses an empty file, wasting a run).
# A browser User-Agent is sent so WAF-fronted hosts that 403 the bare curl UA
# (CourtListener, americanbar.org) serve their normal 200. Both behaviors are
# tunable via FETCH_USER_AGENT / FETCH_MIN_BYTES.
fetch_url() {
  local url="$1" out="$2" size
  curl -fsSL --max-time 300 --retry 2 \
    -A "$FETCH_USER_AGENT" \
    -H 'Accept: text/html,application/xhtml+xml,application/pdf,*/*;q=0.8' \
    -o "$out" "$url" || return 1
  size=$(stat -f %z "$out" 2>/dev/null || echo 0)
  if (( size < FETCH_MIN_BYTES )); then
    log "  fetch returned an empty/too-small body (${size}B < ${FETCH_MIN_BYTES}B): $url"
    return 2
  fi
  return 0
}

# Dedup check. Echoes "<matched-summary-path>\t<reason>" where reason is one of:
#   hash | url              — duplicate of an existing source (move to _duplicate/)
#   backfill:hash           — same bytes as an existing summary whose original is missing
#   backfill:url            — same URL as an existing summary whose original is missing
#   backfill:fuzzy          — input filename or PDF title/first-page content strongly
#                             matches a missing-original summary's slug/title
# Backfill applies to PDF and .md/.html inputs; the file is filed as the
# matched summary's missing <slug>.pdf / <slug>.snapshot.md instead of running
# a full intake. Empty output = not a duplicate; proceed with normal intake.
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
is_doc = input_path.suffix.lower() in ('.md', '.html', '.htm')
can_backfill = is_pdf or is_doc

def is_staging_note(p):
    """True if p is a draft-section candidate note (status:
    candidate-for-intake) rather than a real content capture. Such a note
    filed as <slug>.snapshot.md is metadata about the source, not the
    source — counting it as the original would block backfill of the
    real document."""
    head = read_frontmatter_text(p)
    if head is None:
        return False
    end = head.find('\n---\n', 4)
    if end == -1:
        return False
    try:
        fm = yaml.safe_load(head[4:end]) or {}
    except yaml.YAMLError:
        return False
    return str(fm.get('status') or '').strip() == 'candidate-for-intake'

def has_original(summary_path):
    slug = summary_path.name[: -len('.summary.md')]
    if (summary_path.parent / f"{slug}.pdf").exists():
        return True
    snap = summary_path.parent / f"{slug}.snapshot.md"
    return snap.exists() and not is_staging_note(snap)

def normalize(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

input_name_norm = normalize(input_path.stem)

# Content-based identity signals for PDFs whose filenames are opaque
# (publisher names like 3582269.3615599.pdf say nothing about the paper).
# The embedded Title metadata and the first page's text usually carry the
# real title, which can be matched against the summary's title: field.
pdf_meta_norm = ""
pdf_page1_norm = ""
if is_pdf:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(input_path))
        pdf_meta_norm = normalize((reader.metadata or {}).get('/Title') or '')
        if reader.pages:
            pdf_page1_norm = normalize((reader.pages[0].extract_text() or '')[:2000])
    except Exception:
        pass

fuzzy_candidates = []  # (summary_path, slug_norm, title_norm, url_ids)

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
        reason = 'backfill:hash' if (can_backfill and not has_original(summary)) else 'hash'
        print(f"{summary}\t{reason}")
        sys.exit(0)
    # Match preprint_url: too — a promoted summary's url: is the published
    # venue, but a later drop of the original arXiv/SSRN link must still
    # dedup against it rather than intake a second copy.
    if input_url_canon and input_url_canon in (
        canon_url(fm.get('url') or ''),
        canon_url(fm.get('preprint_url') or ''),
    ):
        reason = 'backfill:url' if (can_backfill and not has_original(summary)) else 'url'
        print(f"{summary}\t{reason}")
        sys.exit(0)
    if can_backfill and not has_original(summary):
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

# Fuzzy backfill: only PDF/doc inputs, only against summaries known to be
# missing their original. Score is the max of filename-vs-slug/title and (for
# PDFs) embedded-title-vs-title similarity, lifted on substring containment
# and boosted +0.10 when a year in the slug also appears in the input
# filename. Require top >= 0.70 AND a >= 0.15 margin over the runner-up so an
# ambiguous pile of candidates declines to guess.
if can_backfill and fuzzy_candidates:
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
        # Content signals: the PDF's own Title metadata or first-page text
        # matching the summary's title identifies the source even when the
        # filename is an opaque publisher artifact.
        if pdf_meta_norm and title_norm:
            sc = max(sc, SequenceMatcher(None, pdf_meta_norm, title_norm).ratio())
            if title_norm in pdf_meta_norm or pdf_meta_norm in title_norm:
                sc = max(sc, 0.90)
        if pdf_page1_norm and title_norm and title_norm in pdf_page1_norm:
            sc = max(sc, 0.90)
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

# Classify a .md inbox input as a draft-section staging note. Echoes the
# note's source URL (pdf_url preferred, then url) when its frontmatter says
# `status: candidate-for-intake`; echoes "NONE" for a candidate note with no
# fetchable URL; echoes nothing for ordinary documents.
candidate_note_url() {
  local input_path="$1"
  "$PYTHON" - "$input_path" <<'PY'
import sys
import yaml

try:
    with open(sys.argv[1], errors='replace') as f:
        head = f.read(65536)
except Exception:
    sys.exit(0)
if not head.startswith('---\n'):
    sys.exit(0)
end = head.find('\n---\n', 4)
if end == -1:
    sys.exit(0)
try:
    fm = yaml.safe_load(head[4:end]) or {}
except yaml.YAMLError:
    sys.exit(0)
if str(fm.get('status') or '').strip() != 'candidate-for-intake':
    sys.exit(0)
url = str(fm.get('pdf_url') or fm.get('url') or '').strip()
print(url if url.startswith(('http://', 'https://')) else 'NONE')
PY
}

# Compute sha256 of a file (for post-success hash injection).
file_sha256() {
  shasum -a 256 "$1" | awk '{print $1}'
}

# Insert source_hash: into a summary's frontmatter, idempotently.
# With a third arg "force", an existing (possibly stale) source_hash line is
# replaced instead of being left alone — used when a backfill just filed the
# authoritative artifact and whatever the frontmatter said is superseded.
inject_hash() {
  local summary_path="$1" hash_val="$2" force="${3:-}"
  "$PYTHON" - "$summary_path" "$hash_val" "$force" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
hash_val = sys.argv[2]
force = sys.argv[3] == 'force'
text = path.read_text()
if not text.startswith('---\n'):
    sys.exit(1)
end = text.find('\n---\n', 4)
if end == -1:
    sys.exit(1)
new_line = f'source_hash: "{hash_val}"\n'
# Scope the already-present check to the frontmatter block — a summary whose
# BODY merely mentions the literal string must still get its hash injected.
if 'source_hash:' in text[:end + 1]:
    if not force:
        sys.exit(0)
    fm = re.sub(r'^source_hash:[^\n]*\n', new_line, text[:end + 1], count=1, flags=re.MULTILINE)
    if fm == text[:end + 1]:
        sys.exit(0)
    path.write_text(fm + text[end + 1:])
    sys.exit(0)
m = re.search(r'^tldr:[^\n]*\n', text[:end + 1], re.MULTILINE)
insert_at = m.end() if m else end + 1
path.write_text(text[:insert_at] + new_line + text[insert_at:])
PY
}

# Stamp URL provenance into a promoted summary's frontmatter.
# $2 (published_url, may be empty): force-replaces the url: line — the intake
# agent only saw the bare PDF, so the cache's OpenAlex venue page beats
# whatever it inferred. $3 (preprint_url, may be empty): recorded as
# preprint_url: unless it matches the summary's final url or the key is
# already present (idempotent).
inject_urls() {
  local summary_path="$1" published_url="$2" preprint_url="$3"
  "$PYTHON" - "$summary_path" "$published_url" "$preprint_url" <<'PY'
import re, sys
from pathlib import Path
path = Path(sys.argv[1])
published_url = sys.argv[2].strip()
preprint_url = sys.argv[3].strip()
text = path.read_text()
if not text.startswith('---\n'):
    sys.exit(1)
end = text.find('\n---\n', 4)
if end == -1:
    sys.exit(1)
fm = text[:end + 1]

def yaml_quote(u):
    return '"' + u.replace('\\', '').replace('"', '') + '"'

if published_url:
    new_line = f'url: {yaml_quote(published_url)}\n'
    fm, n = re.subn(r'^url:[^\n]*\n', new_line, fm, count=1, flags=re.MULTILINE)
    if not n:
        fm += new_line
if preprint_url and not re.search(r'^preprint_url:', fm, re.MULTILINE):
    m = re.search(r'^url:[^\n]*\n', fm, re.MULTILINE)
    current_url = ''
    if m:
        current_url = m.group(0).split(':', 1)[1].strip().strip('"\'')
    if current_url.rstrip('/') != preprint_url.rstrip('/'):
        new_line = f'preprint_url: {yaml_quote(preprint_url)}\n'
        insert_at = m.end() if m else len(fm)
        fm = fm[:insert_at] + new_line + fm[insert_at:]
new_text = fm + text[end + 1:]
if new_text != text:
    path.write_text(new_text)
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

# Append a library CHANGELOG.md entry for each produced summary.
# library/CLAUDE.md requires every intake to record what was filed, the
# folder created, and the key finding (2–4 sentences), newest at the top
# under a "## YYYY-MM-DD (latest)" heading. The "(latest)" marker moves to
# the new heading; same-day intakes stack under the existing heading.
# Non-fatal by design: a CHANGELOG hiccup must not fail a filed intake.
append_changelog() {
  local paths_file="$1"
  "$PYTHON" - "$LIBRARY" "$paths_file" <<'PY'
import datetime, re, sys
from pathlib import Path
import yaml

library, paths_file = sys.argv[1:3]
cl = Path(library) / "CHANGELOG.md"
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

entries = []
for line in open(paths_file):
    p = Path(line.strip())
    if not line.strip() or not p.is_file():
        continue
    text = p.read_text()
    if not text.startswith("---\n"):
        continue
    end = text.find("\n---\n", 4)
    if end == -1:
        continue
    try:
        fm = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        continue
    title = fm.get("title") or p.name[: -len(".summary.md")]
    authors = fm.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    surnames = [str(a).split(",")[0].strip() for a in authors if a]
    who = f"{surnames[0]} et al." if len(surnames) > 3 else (", ".join(surnames) or "unattributed")
    year = str(fm.get("date") or "")[:4]
    rel = p.parent.relative_to(library)
    tldr = str(fm.get("tldr") or "").strip().rstrip(".")
    entries.append(f"- Filed *{title}* ({who}, {year}) into `{rel}/`. Key finding: {tldr}.")

if not entries:
    sys.exit(0)
block = "\n".join(entries)

text = cl.read_text() if cl.is_file() else "# Changelog\n"
hdr = re.search(r"^## (.+)$", text, re.M)
if hdr and hdr.group(1).split()[0] == today:
    # Same-day heading exists: stack the new entries directly under it.
    at = hdr.end()
    text = text[:at] + "\n\n" + block + text[at:]
else:
    if hdr and " (latest)" in hdr.group(1):
        text = text[:hdr.start()] + "## " + hdr.group(1).replace(" (latest)", "") + text[hdr.end():]
    top = re.match(r"# Changelog[ \t]*\n", text)
    at = top.end() if top else 0
    text = text[:at] + f"\n## {today} (latest)\n\n{block}\n" + text[at:]
cl.write_text(text)
print(f"appended {len(entries)} CHANGELOG entr{'y' if len(entries)==1 else 'ies'}")
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

processed_any=0

# Drain loop: the for-glob below is expanded once per pass, so a file dropped
# while a pass is in flight is invisible to it — and launchd does not reliably
# re-deliver WatchPaths events that arrive while the job is already running.
# Without a re-scan such a file would sit until the next inbox disturbance (or
# the plist's 5-minute StartInterval backstop). So: keep re-globbing until a
# full pass claims nothing. pass_claimed is set only when a file is actually
# moved out of the inbox; files merely skipped (still being written, backfill
# races) must not count, or an active writer would spin this loop. The body is
# deliberately not re-indented: its heredocs and quoted inline-Python blocks
# are column-sensitive.
while :; do
pass_claimed=0
for path in "$INBOX"/*; do
  [[ -f "$path" ]] || continue
  base=$(basename "$path")
  [[ "$base" == .* ]] && continue

  # File-too-new guard: a file still being written has a recent mtime, and
  # processing it mid-copy yields a truncated read. We can't just skip — a
  # skipped file wouldn't re-fire WatchPaths, leaving it stuck until the
  # plist's 5-minute StartInterval sweep. Wait until the file ages past
  # the threshold, then re-check mtime to confirm it's no longer being
  # written. If mtime moved during the wait the writer is still active, so
  # punt (the next directory event or interval tick will retrigger us).
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
  promote_published_url=""
  promote_preprint_url=""

  # --- Staging-note candidates ----------------------------------------------
  # draft-section stages metadata-only candidate notes: .md files whose
  # frontmatter carries `status: candidate-for-intake` plus a pdf_url/url
  # pointing at the real document. The note is an intake REQUEST, not source
  # content — filed as <slug>.snapshot.md it fake-satisfies the originals
  # audit and blocks a later backfill of the real PDF. Fetch the document the
  # note points at and intake THAT in this same iteration — the drain loop's
  # re-scan would also pick it up next pass, but the swap keeps note and
  # source in one iteration so the run log reads as a single job.
  if [[ "$base" == *.[Mm][Dd] ]]; then
    # Multi-source manifest first: a candidate-for-intake note whose frontmatter
    # carries a `sources:` LIST requests several documents in one drop. Fetch
    # each into the inbox (the drain loop intakes them on later passes) and
    # archive the note to _notes/ — like the single-URL case, filing the note
    # itself as a snapshot would block backfill of the real documents. Deployed
    # alongside this script as claude-source-intake-candidate-manifest.py; in the
    # repo it's scripts/candidate-manifest.py. Try both.
    manifest_helper=""
    for cand in \
      "$(dirname "$0")/claude-source-intake-candidate-manifest.py" \
      "$(dirname "$0")/candidate-manifest.py"; do
      [[ -f "$cand" ]] && { manifest_helper="$cand"; break; }
    done
    manifest_out=""
    if [[ -n "$manifest_helper" ]]; then
      manifest_out=$("$PYTHON" "$manifest_helper" "$path" 2>/dev/null || true)
    fi
    if [[ -n "$manifest_out" ]]; then
      if [[ "$manifest_out" == "NONE" ]]; then
        log "manifest note '$base' lists no fetchable sources; routing to _failed/"
        fail_dest="$INBOX/_failed/$base"
        n=1
        while [[ -e "$fail_dest" ]]; do
          fail_dest="$INBOX/_failed/${base%.*}.${n}.${base##*.}"
          n=$((n+1))
        done
        mv "$path" "$fail_dest"
        pass_claimed=1
        cat > "${fail_dest}.log" <<EOF
Manifest candidate note: its sources: list has no fetchable pdf_url/url.
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

Each sources[] entry needs an http(s) pdf_url (preferred) or url. Fix the
frontmatter and move this note back to the inbox, or download the documents
manually and drop those in instead.
EOF
        cand_err_file=$(mktemp -t intake-cand-err)
        cand_paths_file=$(mktemp -t intake-cand-paths)
        printf 'manifest note has no fetchable sources\n' > "$cand_err_file"
        append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "failure" "$cand_paths_file" "$cand_err_file" ""
        rm -f "$cand_err_file" "$cand_paths_file"
        continue
      fi
      log "candidate manifest '$base'; fetching listed sources"
      cand_paths_file=$(mktemp -t intake-cand-paths)
      cand_fail_file=$(mktemp -t intake-cand-fail)
      manifest_targets_file=$(mktemp -t intake-manifest)
      printf '%s\n' "$manifest_out" > "$manifest_targets_file"
      fetched_n=0
      failed_n=0
      # Read targets from a file (not a pipe) so this loop runs in the current
      # shell and the counters below survive it.
      while IFS=$'\t' read -r m_slug m_url; do
        [[ -n "$m_url" ]] || continue
        fetch_tmp=$(mktemp -t intake-fetch)
        if fetch_url "$m_url" "$fetch_tmp"; then
          if [[ "$(head -c 4 "$fetch_tmp" 2>/dev/null)" == "%PDF" ]]; then
            m_ext="pdf"
          else
            m_ext="html"
          fi
          fetch_dest="$INBOX/${m_slug}.${m_ext}"
          n=1
          while [[ -e "$fetch_dest" ]]; do
            fetch_dest="$INBOX/${m_slug}.${n}.${m_ext}"
            n=$((n+1))
          done
          if mv "$fetch_tmp" "$fetch_dest"; then
            printf '%s\n' "$fetch_dest" >> "$cand_paths_file"
            fetched_n=$((fetched_n+1))
            log "  fetched '$m_slug' -> $(basename "$fetch_dest")"
          else
            rm -f "$fetch_tmp"
            printf '%s\t%s\tcould not move into inbox\n' "$m_slug" "$m_url" >> "$cand_fail_file"
            failed_n=$((failed_n+1))
          fi
        else
          rm -f "$fetch_tmp"
          printf '%s\t%s\tfetch failed\n' "$m_slug" "$m_url" >> "$cand_fail_file"
          failed_n=$((failed_n+1))
          log "  fetch failed for '$m_slug' ($m_url); writing a retry stub to _failed/"
          stub="$INBOX/_failed/${m_slug}.candidate.md"
          n=1
          while [[ -e "$stub" ]]; do
            stub="$INBOX/_failed/${m_slug}.${n}.candidate.md"
            n=$((n+1))
          done
          printf -- '---\nstatus: candidate-for-intake\npdf_url: %s\n---\n' "$m_url" > "$stub"
          cat > "${stub}.log" <<EOF
Manifest entry fetch failed for slug "$m_slug".
URL: $m_url
From manifest: $base
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

Move this stub back into the inbox to retry just this source, or download
the document manually and drop that in instead.
EOF
        fi
      done < "$manifest_targets_file"
      rm -f "$manifest_targets_file"
      # Archive the manifest note (an intake request, not source content).
      note_dest="$INBOX/_notes/$base"
      n=1
      while [[ -e "$note_dest" ]]; do
        note_dest="$INBOX/_notes/${base%.*}.${n}.${base##*.}"
        n=$((n+1))
      done
      if mv "$path" "$note_dest" 2>/dev/null; then
        cat > "${note_dest}.log" <<EOF
Manifest candidate processed.
Fetched $fetched_n source(s) into the inbox; $failed_n failed.
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
      else
        log "  WARNING: could not archive manifest '$base' to _notes/; removing it"
        rm -f "$path" 2>/dev/null || true
      fi
      pass_claimed=1
      if (( fetched_n > 0 )); then
        manifest_outcome="candidate-fetched"
      else
        manifest_outcome="failure"
      fi
      append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "$manifest_outcome" "$cand_paths_file" "$cand_fail_file" ""
      rm -f "$cand_paths_file" "$cand_fail_file"
      log "  manifest '$base': $fetched_n fetched, $failed_n failed; drain loop will intake them"
      continue
    fi

    # Single-source candidate note: frontmatter scalar pdf_url/url (below).
    cand_url=$(candidate_note_url "$path" || true)
    if [[ "$cand_url" == "NONE" ]]; then
      log "candidate note '$base' has no fetchable pdf_url/url; routing to _failed/"
      fail_dest="$INBOX/_failed/$base"
      n=1
      while [[ -e "$fail_dest" ]]; do
        fail_dest="$INBOX/_failed/${base%.*}.${n}.${base##*.}"
        n=$((n+1))
      done
      mv "$path" "$fail_dest"
      pass_claimed=1
      cat > "${fail_dest}.log" <<EOF
Staging-note candidate with no fetchable pdf_url/url in its frontmatter.
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

The note was not intaked: it is metadata about a source, not the source
itself, and filing it as a snapshot would block backfill of the real
document. Add a pdf_url/url to the frontmatter and move it back to the
inbox, or download the document manually and drop that in instead.
EOF
      cand_paths_file=$(mktemp -t intake-cand-paths)
      cand_err_file=$(mktemp -t intake-cand-err)
      printf 'candidate note has no fetchable url\n' > "$cand_err_file"
      append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "failure" "$cand_paths_file" "$cand_err_file" ""
      rm -f "$cand_paths_file" "$cand_err_file"
      continue
    elif [[ -n "$cand_url" ]]; then
      log "candidate note '$base'; fetching source: $cand_url"
      fetch_tmp=$(mktemp -t intake-fetch)
      if fetch_url "$cand_url" "$fetch_tmp"; then
        # Name the fetched file after the note's stem (the suggested slug,
        # minus any .candidate marker) so dedup/backfill matches it strongly.
        fetch_stem="${base%.*}"
        fetch_stem="${fetch_stem%.candidate}"
        if [[ "$(head -c 4 "$fetch_tmp" 2>/dev/null)" == "%PDF" ]]; then
          fetch_base="${fetch_stem}.pdf"
        else
          # The URL served web content rather than a PDF; intake it as a
          # document, which legitimately files as <slug>.snapshot.md.
          fetch_base="${fetch_stem}.html"
        fi
        fetch_dest="$INBOX/$fetch_base"
        n=1
        while [[ -e "$fetch_dest" ]]; do
          fetch_dest="$INBOX/${fetch_base%.*}.${n}.${fetch_base##*.}"
          n=$((n+1))
        done
        if ! mv "$fetch_tmp" "$fetch_dest"; then
          log "  could not move fetched file into inbox; leaving note for a retry"
          rm -f "$fetch_tmp"
          continue
        fi
        note_dest="$INBOX/_notes/$base"
        n=1
        while [[ -e "$note_dest" ]]; do
          note_dest="$INBOX/_notes/${base%.*}.${n}.${base##*.}"
          n=$((n+1))
        done
        if mv "$path" "$note_dest" 2>/dev/null; then
          cat > "${note_dest}.log" <<EOF
Staging-note candidate processed.
Fetched: $cand_url
Filed fetched source into inbox as: $(basename "$fetch_dest")
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF
        else
          log "  WARNING: could not archive note '$base' to _notes/; removing it"
          rm -f "$path" 2>/dev/null || true
        fi
        cand_paths_file=$(mktemp -t intake-cand-paths)
        printf '%s\n' "$fetch_dest" > "$cand_paths_file"
        append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "candidate-fetched" "$cand_paths_file" "" ""
        rm -f "$cand_paths_file"
        path="$fetch_dest"
        base=$(basename "$fetch_dest")
        log "  fetched -> '$base'; continuing this iteration with the fetched source"
      else
        rm -f "$fetch_tmp"
        log "  fetch failed for candidate '$base' ($cand_url); routing note to _failed/"
        fail_dest="$INBOX/_failed/$base"
        n=1
        while [[ -e "$fail_dest" ]]; do
          fail_dest="$INBOX/_failed/${base%.*}.${n}.${base##*.}"
          n=$((n+1))
        done
        mv "$path" "$fail_dest"
        pass_claimed=1
        cat > "${fail_dest}.log" <<EOF
Staging-note candidate; fetching its source failed.
URL: $cand_url
Detected: $(date -u '+%Y-%m-%dT%H:%M:%SZ')

The note was not intaked: filing it as a snapshot would block backfill of
the real document. To retry the fetch, move this file back to the inbox;
or download the document manually and drop that in instead.
EOF
        cand_paths_file=$(mktemp -t intake-cand-paths)
        cand_err_file=$(mktemp -t intake-cand-err)
        printf 'candidate fetch failed: %s\n' "$cand_url" > "$cand_err_file"
        append_run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$base" "failure" "$cand_paths_file" "$cand_err_file" ""
        rm -f "$cand_paths_file" "$cand_err_file"
        continue
      fi
    fi
  fi

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

    # Backfill: matched summary is missing its source artifact and the input
    # hash/URL/title-matches it. File the input in place of running the full
    # intake — the summary is already correct.
    if [[ "$matched_reason" == backfill:* ]]; then
      backfill_kind="${matched_reason#backfill:}"
      summary_dir=$(dirname "$matched_path")
      summary_base=$(basename "$matched_path")
      summary_slug="${summary_base%.summary.md}"
      case "$base" in
        *.[Pp][Dd][Ff]) bf_ext="pdf" ;;
        *)              bf_ext="snapshot.md" ;;
      esac
      pdf_target="$summary_dir/$summary_slug.$bf_ext"
      if [[ -e "$pdf_target" ]]; then
        # Race: artifact appeared since check_dedup looked. Leave the input in
        # the inbox; next tick re-evaluates against the updated library state.
        log "backfill skipped: $pdf_target already exists; leaving '$base' in inbox for next tick"
        continue
      fi
      if ! mv "$path" "$pdf_target"; then
        log "backfill failed: could not move '$base' to $pdf_target; leaving in inbox"
        continue
      fi
      pass_claimed=1
      log "backfilled missing original for '$base' (matched by $backfill_kind) -> $pdf_target"
      # Re-anchor dedup on the artifact we just filed. Force-replace: a
      # fuzzy/title match means whatever source_hash the frontmatter carried
      # (often a stale value from an old multi-summary run) did NOT match the
      # real source, so it must be overwritten, not skipped.
      pdf_hash=$(file_sha256 "$pdf_target")
      inject_hash "$matched_path" "$pdf_hash" force || true
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
        pass_claimed=1
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
      # Capture URL provenance before it disappears: the preprint's own url:
      # (from the just-archived summary) survives as preprint_url: on the
      # produced summary, and the cache entry's published_url replaces the
      # intake agent's guess — it only sees the bare PDF, so for well-known
      # papers it writes the arXiv link, not the venue page.
      promote_preprint_url=$("$PYTHON" - "$archive_dir/$preprint_slug.summary.md" <<'PY' || true
import sys
import yaml
try:
    with open(sys.argv[1], errors='replace') as f:
        head = f.read(65536)
except Exception:
    sys.exit(0)
if not head.startswith('---\n'):
    sys.exit(0)
end = head.find('\n---\n', 4)
if end == -1:
    sys.exit(0)
try:
    fm = yaml.safe_load(head[4:end]) or {}
except yaml.YAMLError:
    sys.exit(0)
url = str(fm.get('url') or '').strip()
if url.startswith(('http://', 'https://')):
    print(url)
PY
)
      # Remove the preprint's cache entry so /preprints no longer lists it,
      # echoing its published_url (if sane) for the post-intake injection.
      promote_published_url=$("$PYTHON" - "$CONFIG/preprint-checks.json" "$preprint_rel" <<'PY' || true
import json, sys
from pathlib import Path
from urllib.parse import urlsplit
cache_path, rel = sys.argv[1], sys.argv[2]
p = Path(cache_path)
if not p.exists():
    sys.exit(0)
try:
    cache = json.loads(p.read_text())
except Exception:
    sys.exit(0)
entry = cache.get(rel)
if isinstance(entry, dict):
    url = str(entry.get('published_url') or '').strip()
    # check-preprints already excludes preprint-server locations, but OpenAlex
    # records are occasionally polluted (junk DOI registrations pointing at
    # unrelated sites), so re-check the cheap invariants here; a value that
    # fails just means the agent's url: is left untouched.
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    host = parts.netloc.lower() if parts else ''
    preprint_hosts = ('arxiv.org', 'ssrn.com', 'biorxiv.org', 'medrxiv.org', 'osf.io')
    if (
        parts is not None
        and parts.scheme in ('http', 'https')
        and host
        and not any(host == h or host.endswith('.' + h) for h in preprint_hosts)
    ):
        print(url)
if rel in cache:
    del cache[rel]
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))
PY
)
      log "promoting '$base' over preprint $preprint_summary (archived to $archive_dir)"
      is_promotion=1
      promote_target_dir="$preprint_dir"
      # The produced summary's directory will be renamed to match the published
      # slug (canonical winner), so we relocate into <category>/<produced-slug>/
      # rather than into the preprint's own slug dir. promote_target_dir is
      # retained for the failure-restore path below.
      promote_category_dir="$(dirname "$preprint_dir")"
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
      pass_claimed=1
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

  # Stage under an ASCII-only name. Non-ASCII filename bytes (curly quotes,
  # accents) don't survive the agent's path round-tripping — its Read tool
  # gets ENOENT on a byte-for-byte-different rendering of the same visible
  # name — and the agent burns its whole run on extraction workarounds.
  # $base keeps the original name for logging and _failed/ routing; only
  # the staged copy is renamed.
  ascii_base=$("$PYTHON" -c '
import sys, unicodedata
name = unicodedata.normalize("NFKD", sys.argv[1])
name = "".join(c for c in name if not unicodedata.combining(c))
out = "".join(c if c.isascii() and c.isprintable() else "_" for c in name)
print(out.strip() or "input")
' "$base")
  uuid=$(uuidgen | tr '[:upper:]' '[:lower:]')
  staged_path="$INBOX/.staged/${uuid}-${ascii_base}"
  if ! mv "$path" "$staged_path" 2>/dev/null; then
    log "  failed to stage '$base' (already claimed?)"
    continue
  fi
  pass_claimed=1
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
  # pdftotext likewise: it's the agent's only sanctioned way to extract PDF
  # text, and when it's denied the agent falls back to grepping `strings`
  # output one fact at a time (slow, low-fidelity, dozens of Bash calls).
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
        --allowed-tools "WebFetch" "Bash(pdftotext:*)" \
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

  # Ensure the original input is filed alongside the summary — as
  # <slug>.pdf for PDF inputs, <slug>.snapshot.md for .md/.html inputs.
  # The prompt asks the agent to file it, but agents occasionally skip
  # that step, and the staged copy is deleted below; without this
  # backstop the original would be lost. Belt-and-suspenders here.
  # Only when the run produced exactly one summary, though: a digest input
  # that yields several summaries is not the original of any one of them,
  # and filing N copies of it would fake-satisfy the originals audit.
  # This runs BEFORE metadata validation so the validator's snapshot-flag
  # check sees the final artifact state.
  validation_failed=0
  if (( claude_exit == 0 )) && (( num_produced > 0 )); then
    input_hash=$(file_sha256 "$staged_path")
    is_pdf=0
    is_doc=0
    case "$base" in
      *.[Pp][Dd][Ff]) is_pdf=1 ;;
      *.[Mm][Dd]|*.[Hh][Tt][Mm][Ll]|*.[Hh][Tt][Mm]) is_doc=1 ;;
    esac
    while IFS= read -r produced_path; do
      [[ -n "$produced_path" ]] || continue
      folder=$(dirname "$produced_path")
      slug=$(basename "$produced_path" .summary.md)
      if (( is_pdf )) && (( num_produced == 1 )); then
        pdf_target="$folder/$slug.pdf"
        if [[ ! -f "$pdf_target" ]]; then
          if cp "$staged_path" "$pdf_target"; then
            log "  filed PDF -> $pdf_target"
          else
            log "  WARNING: failed to copy PDF to $pdf_target"
          fi
        fi
      fi
      if (( is_doc )) && (( num_produced == 1 )); then
        snap_target="$folder/$slug.snapshot.md"
        if [[ ! -f "$snap_target" ]]; then
          if cp "$staged_path" "$snap_target"; then
            log "  filed snapshot -> $snap_target"
          else
            log "  WARNING: failed to copy snapshot to $snap_target"
          fi
        fi
      fi
    done < "$produced_list"
    # Metadata gate: normalize mechanical deviations in place (null fields,
    # synonym source_types, snapshot flag vs. file presence, missing
    # retrieved/currency_check), then hard-validate against the template
    # (enum membership, "Last, First" authors, no "et al." placeholders,
    # arXiv author cross-check). A failure drops the run into the failure
    # branch below, which quarantines the produced folders and routes the
    # input to _failed/ — bad metadata never gets filed or indexed.
    if "$PYTHON" "$HOME/Library/Scripts/claude-source-intake-validate.py" \
        --paths-file "$produced_list" >>"$run_log" 2>&1; then
      log "  metadata validation passed"
    else
      validation_failed=1
      log "  metadata validation FAILED — rejecting run (details in run log)"
    fi
  fi

  if (( claude_exit == 0 )) && (( num_produced > 0 )) && (( validation_failed == 0 )); then
    log "  success (claude exit 0, $num_produced summary file(s) produced)"
    # Promotion: the preprint's slug dir was emptied of files at detect-time.
    # Remove it now, BEFORE relocating the produced folder — a published
    # version that re-uses the preprint's slug would otherwise collide with
    # the leftover empty dir and the relocation would be skipped.
    if (( is_promotion )) && [[ -d "$promote_target_dir" ]]; then
      rmdir "$promote_target_dir" 2>/dev/null || true
    fi
    while IFS= read -r produced_path; do
      [[ -n "$produced_path" ]] || continue
      folder=$(dirname "$produced_path")
      slug=$(basename "$produced_path" .summary.md)
      # Anchor future dedup: hash the summary's own source artifact, not the
      # run's input. A multi-summary run used to stamp the single input's
      # hash onto every summary it produced, which (a) is the wrong value
      # for each individual source and (b) made any later PDF whose hash
      # collided with it bounce as a "duplicate" of an unrelated summary.
      artifact=""
      [[ -f "$folder/$slug.pdf" ]] && artifact="$folder/$slug.pdf"
      [[ -z "$artifact" && -f "$folder/$slug.snapshot.md" ]] && artifact="$folder/$slug.snapshot.md"
      if [[ -n "$artifact" ]]; then
        if inject_hash "$produced_path" "$(file_sha256 "$artifact")"; then
          log "  injected source_hash into $produced_path"
        fi
      elif (( num_produced == 1 )); then
        # No artifact on disk (e.g. .txt/.url input and the agent wrote no
        # snapshot) — the input's own hash still dedups an identical re-drop.
        if inject_hash "$produced_path" "$input_hash"; then
          log "  injected source_hash into $produced_path"
        fi
      else
        log "  NOTE: no per-source artifact for $produced_path; leaving source_hash unset"
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
      # Promotion: stamp URL provenance into the (possibly relocated) summary.
      # Single-summary runs only — a multi-summary run can't say which one the
      # promoted preprint corresponds to, so guessing would mislabel sources.
      if (( is_promotion )) && (( num_produced == 1 )) && \
         [[ -n "$promote_published_url" || -n "$promote_preprint_url" ]]; then
        final_summary="$produced_path"
        [[ -n "$promote_category_dir" && -f "$promote_category_dir/$slug/$slug.summary.md" ]] && \
          final_summary="$promote_category_dir/$slug/$slug.summary.md"
        if inject_urls "$final_summary" "$promote_published_url" "$promote_preprint_url"; then
          log "  stamped url provenance into $final_summary"
        else
          log "  WARNING: failed to stamp url provenance into $final_summary"
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
    if append_changelog "$produced_list" >>"$run_log" 2>&1; then
      log "  CHANGELOG.md updated"
    else
      log "  CHANGELOG.md update failed (non-fatal)"
    fi
    if "$PYTHON" "$HOME/Library/Scripts/claude-source-intake-regen-index.py" 2>&1; then
      log "  INDEX.md regenerated"
    else
      log "  INDEX.md regen failed (non-fatal)"
    fi
  else
    if (( validation_failed )); then
      log "  rejected (metadata validation failed; $num_produced summary file(s) quarantined)"
    else
      log "  failure (claude exit $claude_exit, $num_produced summary file(s) produced)"
    fi
    # Quarantine partial output: claude may have written summaries before the
    # run failed (e.g. watchdog timeout). Left in the library they'd be
    # unindexed, carry no source_hash, and their url: could dedup-block a
    # later retry of the same input. Only quarantine folders CREATED by this
    # run (birthtime >= the run sentinel) — a pre-existing folder that merely
    # changed is the user's data and stays put.
    if (( num_produced > 0 )); then
      sentinel_ts=$(stat -f %m "$sentinel" 2>/dev/null || echo 0)
      partial_root="$INBOX/_failed/_partial"
      (( validation_failed )) && partial_root="$INBOX/_failed/_rejected"
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
            # Drop the category dir too if the quarantine emptied it — a
            # category created by this run shouldn't linger as an empty
            # shell in the library. rmdir is a no-op when non-empty.
            rmdir "$(dirname "$pfolder")" 2>/dev/null || true
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
    fail_outcome="failure"
    (( validation_failed )) && fail_outcome="rejected-metadata"
    append_run "$start_ts" "$base" "$fail_outcome" "$produced_list" "$run_log" "$run_log"
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
(( pass_claimed )) || break
log "pass complete; re-scanning inbox for files dropped mid-run"
done

if (( processed_any == 0 )); then
  log "no files to process"
fi
