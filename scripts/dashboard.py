#!/usr/bin/env python3
"""Local dashboard for the autonomous source-intake pipeline.

Binds to 127.0.0.1 only. Cross-origin POSTs are blocked at the
request layer as defense-in-depth against browser CSRF (see
`block_cross_origin_post` below).
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import bleach
import markdown
import yaml
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file

HOME = Path.home()
INBOX = Path(os.environ.get("INBOX_PATH") or (HOME / "source-library-inbox"))
STAGED = INBOX / ".staged"
FAILED = INBOX / "_failed"
LIBRARY = Path(os.environ.get("LIBRARY_PATH") or (HOME / "source-library"))
CONFIG = HOME / ".config/claude-source-intake"
PROMPT_FILE = CONFIG / "prompt.txt"
PAUSED_FLAG = CONFIG / "paused"
RUNS_LOG = CONFIG / "runs.jsonl"
WORKER_OUT_LOG = "/tmp/claude-source-intake.out.log"
WORKER_ERR_LOG = "/tmp/claude-source-intake.err.log"
WORKER = HOME / "Library/Scripts/claude-source-intake.sh"
REGEN_INDEX = HOME / "Library/Scripts/claude-source-intake-regen-index.py"
CHECK_PREPRINTS = HOME / "Library/Scripts/claude-source-intake-check-preprints.py"
PREPRINT_CACHE = CONFIG / "preprint-checks.json"
VENV_PYTHON = CONFIG / "venv/bin/python"

PORT = int(os.environ.get("DASHBOARD_PORT", "7341"))

# Origins permitted to make state-changing requests. Browser CSRF defense.
# DASHBOARD_EXTRA_ORIGINS is an optional comma-separated list of full origins
# (scheme://host[:port], no trailing slash) — e.g. a reverse-proxy hostname
# that fronts the dashboard for the local browser.
_extra_origins = {
    o.strip().rstrip("/")
    for o in os.environ.get("DASHBOARD_EXTRA_ORIGINS", "").split(",")
    if o.strip()
}
ALLOWED_ORIGINS = frozenset({
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
    *_extra_origins,
})

# UUID prefix the worker assigns to staged input files. Must stay in sync
# with the `uuidgen` line in scripts/worker.sh — if that contract changes,
# strip_uuid and stray-detection here will break.
UUID_PREFIX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-")

# Whitelist for sanitizing LLM-generated markdown. LLM-written summaries
# are untrusted output — a prompt-injecting PDF could induce hostile HTML.
# Bleach strips anything not on this list, including script tags and event
# handlers on permitted tags.
ALLOWED_HTML_TAGS = [
    "p", "br", "strong", "em", "del", "code", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "blockquote", "hr",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
    "sup", "sub",
]
ALLOWED_HTML_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "title"],
    "*": ["id", "class"],
}
ALLOWED_HTML_PROTOCOLS = ["http", "https", "mailto"]

# File extensions /api/open will hand off to the OS. Anything else (e.g.
# executables, scripts) is refused even if it lives inside the library.
OPENABLE_EXTS = frozenset({".pdf", ".md", ".html", ".htm", ".txt"})


def strip_uuid(name: str) -> str:
    return UUID_PREFIX.sub("", name, count=1)


def safe_name(name: str) -> str:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\x00" in name
        or "\n" in name
        or "\r" in name
    ):
        abort(400, "invalid filename")
    return name


def list_dir(path: Path):
    if not path.exists():
        return []
    out = []
    for p in sorted(path.iterdir()):
        if p.name.startswith(".") or not p.is_file():
            continue
        out.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
        })
    return out


def list_failed():
    return _list_quarantine(FAILED)


DUPLICATE = INBOX / "_duplicate"


def list_duplicates():
    return _list_quarantine(DUPLICATE)


def _list_quarantine(folder: Path):
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.suffix == ".log" or not p.is_file():
            continue
        log_path = folder / f"{p.name}.log"
        log = ""
        if log_path.exists():
            try:
                log = log_path.read_text(errors="replace")[-4000:]
            except Exception as e:
                log = f"(could not read log: {e})"
        out.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
            "log": log,
        })
    return out


def recent_runs(limit=20):
    if not RUNS_LOG.exists():
        return []
    # Tail-read so we don't scan the whole file on every dashboard request.
    # The worker rotates runs.jsonl when it crosses RUNS_LOG_MAX_BYTES; old
    # entries land in runs.jsonl.1 and are not shown here.
    try:
        result = subprocess.run(
            ["tail", "-n", str(limit * 2), str(RUNS_LOG)],
            capture_output=True, text=True, check=True, timeout=2,
        )
        lines = result.stdout.splitlines()
    except (subprocess.SubprocessError, OSError):
        # Fallback for unusual environments (no tail in PATH, perms, etc.)
        lines = RUNS_LOG.read_text(errors="replace").splitlines()
    library_resolved = LIBRARY.resolve() if LIBRARY.exists() else None
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        rel_paths = []
        for p in entry.get("output_paths") or []:
            if library_resolved:
                try:
                    rel = Path(p).resolve().relative_to(library_resolved).as_posix()
                    rel_paths.append(rel)
                    continue
                except (ValueError, OSError):
                    pass
            rel_paths.append(p)
        entry["output_paths_relative"] = rel_paths
        out.append(entry)
    out.reverse()
    return out


def list_categories():
    if not LIBRARY.exists():
        return []
    return sorted(
        p.name for p in LIBRARY.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
    )


CATEGORY_ORDER = [c.strip() for c in os.environ.get("CATEGORY_ORDER", "").split(",") if c.strip()]


def parse_frontmatter(path: Path):
    try:
        text = path.read_text()
    except Exception:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None


def collect_library_sources():
    by_cat: dict[str, list[dict]] = {}
    if not LIBRARY.exists():
        return by_cat
    for summary in LIBRARY.glob("*/*/*.summary.md"):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        fm = parse_frontmatter(summary)
        if fm is None:
            continue
        rel_path = summary.relative_to(LIBRARY).as_posix()
        authors = fm.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        date = str(fm.get("date") or "")
        by_cat.setdefault(category, []).append({
            "title": fm.get("title") or summary.stem,
            "tldr": (fm.get("tldr") or "").strip(),
            "authors": authors,
            "date": date,
            "year": date[:4] if len(date) >= 4 else "",
            "tags": fm.get("tags") or [],
            "url": fm.get("url") or "",
            "rel_path": rel_path,
            "category": category,
            "source_file": find_source_file(summary),
        })
    for sources in by_cat.values():
        sources.sort(key=lambda s: (s["date"], s["title"]), reverse=True)
    return by_cat


def find_source_file(summary: Path):
    """Return sibling .pdf or .snapshot.md as a dict, or None."""
    name = summary.name
    if not name.endswith(".summary.md"):
        return None
    slug = name[: -len(".summary.md")]
    for ext, kind in ((".pdf", "pdf"), (".snapshot.md", "snapshot")):
        candidate = summary.parent / f"{slug}{ext}"
        if candidate.exists():
            try:
                rel = candidate.resolve().relative_to(LIBRARY.resolve()).as_posix()
            except (ValueError, OSError):
                rel = candidate.as_posix()
            return {"rel_path": rel, "kind": kind}
    return None


INDEX_LINK_RE = re.compile(r"\]\(([^)\s]+\.summary\.md)\)")


def audit_library():
    """Walk the library and surface drift between disk and INDEX.md.

    Catches the failure mode where the user deletes a source folder by hand
    but INDEX.md hasn't been regenerated since (it's only rewritten on a
    successful intake), plus missing-sidecar and unparseable-frontmatter
    cases that the index generator silently skips.
    """
    findings = {
        "summaries": [],
        "missing_originals": [],
        "bad_frontmatter": [],
        "index_orphans": [],
        "unindexed": [],
        "index_exists": False,
        "index_mtime": None,
        "library_exists": LIBRARY.exists(),
    }
    if not LIBRARY.exists():
        return findings

    on_disk_paths: set[str] = set()
    for summary in LIBRARY.glob("*/*/*.summary.md"):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        rel = summary.relative_to(LIBRARY).as_posix()
        on_disk_paths.add(rel)
        fm = parse_frontmatter(summary)
        if fm is None:
            findings["bad_frontmatter"].append({"rel_path": rel, "category": category})
            continue
        sf = find_source_file(summary)
        entry = {
            "rel_path": rel,
            "title": fm.get("title") or summary.stem,
            "category": category,
            "original_kind": sf["kind"] if sf else None,
            "original_rel": sf["rel_path"] if sf else None,
        }
        findings["summaries"].append(entry)
        if sf is None:
            findings["missing_originals"].append(entry)

    index_path = LIBRARY / "INDEX.md"
    indexed_paths: set[str] = set()
    if index_path.exists():
        findings["index_exists"] = True
        findings["index_mtime"] = index_path.stat().st_mtime
        try:
            text = index_path.read_text(errors="replace")
            for m in INDEX_LINK_RE.finditer(text):
                indexed_paths.add(m.group(1))
        except OSError:
            pass

    findings["index_orphans"] = sorted(indexed_paths - on_disk_paths)
    findings["unindexed"] = sorted(on_disk_paths - indexed_paths)
    findings["summaries"].sort(key=lambda s: (s["category"], s["title"].lower()))
    return findings


def load_preprint_cache() -> dict:
    if not PREPRINT_CACHE.exists():
        return {}
    try:
        return json.loads(PREPRINT_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def check_preprints_now(timeout: int = 600) -> tuple[bool, str]:
    """Invoke the deployed check-preprints script via the worker venv.

    Runs synchronously so the dashboard can flash success/failure. Network
    calls go to OpenAlex; the script paces itself but a large library can
    still take a minute or two.
    """
    if not CHECK_PREPRINTS.exists():
        return False, f"check-preprints script not found at {CHECK_PREPRINTS}"
    if not VENV_PYTHON.exists():
        return False, f"venv python not found at {VENV_PYTHON}"
    env = {**os.environ, "LIBRARY_PATH": str(LIBRARY)}
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), str(CHECK_PREPRINTS)],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.SubprocessError as e:
        return False, f"check-preprints failed: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "check-preprints returned non-zero").strip()
    return True, (r.stdout or "preprint check complete").strip()


def regen_index_now(timeout: int = 30) -> tuple[bool, str]:
    """Invoke the deployed regen-index.py via the worker venv.

    Returns (ok, message). Failures are non-fatal — the caller decides
    whether to flash an error or proceed.
    """
    if not REGEN_INDEX.exists():
        return False, f"regen-index script not found at {REGEN_INDEX}"
    if not VENV_PYTHON.exists():
        return False, f"venv python not found at {VENV_PYTHON}"
    env = {**os.environ, "LIBRARY_PATH": str(LIBRARY)}
    try:
        r = subprocess.run(
            [str(VENV_PYTHON), str(REGEN_INDEX)],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.SubprocessError as e:
        return False, f"regen-index failed: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "regen-index returned non-zero").strip()
    return True, (r.stdout or "INDEX.md regenerated").strip()


# Heuristics for distinguishing institutional authors (e.g. "U.S. Government
# Accountability Office") from individuals ("Vaswani, Ashish"). Keep these
# definitions in sync with the copy in regen-index.py.
_ORG_KEYWORDS = re.compile(
    r"\b("
    r"Office|Agency|Bureau|Committee|Commission|Council|Board|Authority|"
    r"Department|Ministry|Administration|"
    r"Association|Foundation|Society|Federation|Alliance|Coalition|Union|Trust|"
    r"Institute|Institution|Center|Centre|Forum|Initiative|Programme|"
    r"University|College|Law School|"
    r"Corporation|Company|Lab|Laboratory|"
    r"Court|Tribunal|"
    r"State Bar|"
    r"Inc\.?|LLC|Ltd\.?|PLC|GmbH"
    r")\b"
)

_ORG_PHRASES = re.compile(r"\b(School of|Bar Association)\b")

_KNOWN_ORGS = {
    "GAO", "OECD", "IMF", "WHO", "UN", "EU",
    "NIST", "NASA", "NIH", "FDA", "EPA", "FBI", "DOJ", "CDC", "FCC", "SEC",
    "ABA", "NYCBA", "ACLU", "USPTO", "OPM", "BLS",
    "Anthropic", "OpenAI", "Google DeepMind", "DeepMind", "Microsoft Research",
}

_VENUE_HINTS = re.compile(
    r"\b("
    r"arXiv|preprint|SSRN|"
    r"Journal|Review|Transactions|Letters|Proceedings|Conference|Workshop|"
    r"Nature|Cell|Science|JAMA|Lancet|"
    r"ICML|ICLR|NeurIPS|EMNLP|ACL|TACL|PMLR|COLM"
    r")\b"
)


def is_institutional_author(name: str) -> bool:
    """Return True if `name` looks like an organization, not an individual."""
    n = name.strip()
    if not n:
        return False
    if n in _KNOWN_ORGS:
        return True
    if _VENUE_HINTS.search(n):
        return False
    if _ORG_KEYWORDS.search(n) or _ORG_PHRASES.search(n):
        return True
    if "," not in n and len(n.split()) >= 4:
        return True
    return False


def _display_author(name: str) -> str:
    if is_institutional_author(name):
        return name
    if "," in name:
        return name.split(",", 1)[0].strip()
    return name.split()[-1]


def short_authors(authors, max_shown=3):
    if not authors:
        return ""
    displayed = [_display_author(a) for a in authors]
    if len(displayed) <= max_shown:
        return ", ".join(displayed)
    return ", ".join(displayed[:max_shown]) + " et al."


def parse_event(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return [{"icon": "?", "text": line[:300]}]
    t = e.get("type")
    if t == "meta":
        kind = e.get("kind")
        if kind == "start":
            return [{"icon": "▶", "text": f"started: {e.get('input', '')}"}]
        if kind == "end":
            return [{"icon": "■", "text": f"run ended (exit {e.get('exit', '?')})"}]
        return None
    if t == "system":
        if e.get("subtype") == "init":
            return [{"icon": "▶", "text": f"session ready (model: {e.get('model', '?')})"}]
        return None
    if t == "assistant":
        msg = e.get("message") or {}
        out = []
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "text":
                txt = (block.get("text") or "").strip()
                if txt:
                    snippet = txt[:300] + ("…" if len(txt) > 300 else "")
                    out.append({"icon": "💬", "text": snippet})
            elif bt == "tool_use":
                tool = block.get("name", "?")
                inp = block.get("input") or {}
                if tool == "Read":
                    out.append({"icon": "📖", "text": f"Read {inp.get('file_path', '')}"})
                elif tool == "Edit":
                    out.append({"icon": "✏️", "text": f"Edit {inp.get('file_path', '')}"})
                elif tool == "Write":
                    out.append({"icon": "✏️", "text": f"Write {inp.get('file_path', '')}"})
                elif tool == "Bash":
                    cmd = ((inp.get("command") or "").splitlines() or [""])[0][:200]
                    out.append({"icon": "$", "text": cmd})
                elif tool == "Grep":
                    out.append({"icon": "🔍", "text": f"Grep '{inp.get('pattern', '')}' in {inp.get('path', '.')}"})
                elif tool == "Glob":
                    out.append({"icon": "🔍", "text": f"Glob {inp.get('pattern', '')}"})
                elif tool == "TodoWrite":
                    todos = inp.get("todos") or []
                    out.append({"icon": "📋", "text": f"todos updated ({len(todos)} items)"})
                elif tool == "WebFetch":
                    out.append({"icon": "🌐", "text": f"WebFetch {inp.get('url', '')}"})
                elif tool == "Skill":
                    out.append({"icon": "🧰", "text": f"Skill: {inp.get('skill', '?')}"})
                else:
                    keys = list(inp.keys())[:2]
                    out.append({"icon": "🔧", "text": f"{tool}({', '.join(keys)})"})
        return out or None
    if t == "user":
        return None  # tool results - too noisy
    if t == "result":
        sub = e.get("subtype", "")
        cost = e.get("total_cost_usd") or e.get("cost_usd") or 0
        dur_ms = e.get("duration_ms") or 0
        return [{
            "icon": "✅" if sub == "success" else "❌",
            "text": f"{sub} ({dur_ms / 1000:.1f}s, ${float(cost):.3f})",
        }]
    return None


def status_payload():
    # A real in-flight job is UUID-prefixed by the worker. Anything else
    # in .staged/ is a stray (worker leftover, manually dropped file, etc.)
    # and must not light up the "Processing" banner — that's how an
    # orphan helper script was being reported as "running 11h."
    all_staged = list_dir(STAGED)
    staged = [f for f in all_staged if UUID_PREFIX.match(f["name"])]
    strays = [f for f in all_staged if not UUID_PREFIX.match(f["name"])]
    running = None
    if staged:
        first = staged[0]
        running = {"name": strip_uuid(first["name"]), "started_at": first["mtime"]}
    for f in staged:
        f["display"] = strip_uuid(f["name"])
    runs = recent_runs()
    total_cost = sum(float(r.get("cost_usd") or 0) for r in runs)
    return {
        "paused": PAUSED_FLAG.exists(),
        "queue_inbox": list_dir(INBOX),
        "queue_staged": staged,
        "strays": strays,
        "running": running,
        "runs": runs,
        "total_cost": total_cost,
        "failed": list_failed(),
        "duplicates": list_duplicates(),
        "categories": list_categories(),
    }


app = Flask(__name__)


@app.before_request
def block_cross_origin_post():
    """CSRF defense. The dashboard binds to 127.0.0.1 only, but any local
    process — including a browser visiting a malicious site that scripts
    a POST to localhost — can reach it. We block state-changing requests
    whose Origin or Sec-Fetch-Site indicates they didn't originate here.

    Non-browser local clients (curl, scripts) typically send neither header
    and are still allowed. That preserves the prior trust model for local
    automation while closing the browser-CSRF hole.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin", "")
    if origin and origin not in ALLOWED_ORIGINS:
        abort(403, "cross-origin request blocked")
    fetch_site = request.headers.get("Sec-Fetch-Site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        abort(403, "non-same-origin request blocked")
    return None


FONT_LINK = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:ital,wght@0,400;0,700;1,400&family=Libre+Franklin:wght@400;500;600;700&display=swap" rel="stylesheet">"""


SHARED_STYLES = """<link rel="stylesheet" href="https://assets.davidkemp.ai/tokens.css?v=2.0.0">
<style>
:root {
  /* Local-only token not defined in the canonical brand file */
  --color-surface-page: #FAFAF7;
}
* { box-sizing: border-box; }
body { font-family: var(--font-ui); font-size: 14px; line-height: 1.5; background: var(--color-surface-page); color: var(--color-text); margin: 0; padding: var(--space-8); max-width: var(--width-wide); margin-left: auto; margin-right: auto; -webkit-font-smoothing: antialiased; font-feature-settings: "kern", "liga"; }
h1 { font-family: var(--font-ui); font-size: 1.75rem; font-weight: 600; letter-spacing: -0.015em; margin: 0 0 var(--space-2); color: var(--color-primary); }
h2 { font-family: var(--font-ui); font-size: 0.78rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--color-text-faint); margin: var(--space-8) 0 var(--space-3); }
a { color: var(--color-primary-hover); text-decoration: none; }
a:hover { color: var(--color-primary); text-decoration: underline; }
code, pre { font-family: var(--font-mono); }
nav.topnav { display: flex; gap: var(--space-6); padding-bottom: var(--space-3); margin-bottom: var(--space-6); border-bottom: 1px solid var(--color-border); font-size: 0.9rem; }
nav.topnav a { color: var(--color-text-muted); font-weight: 500; text-decoration: none; padding-bottom: var(--space-2); margin-bottom: -1px; }
nav.topnav a:hover { color: var(--color-primary); text-decoration: none; }
nav.topnav a.active { color: var(--color-primary); border-bottom: 2px solid var(--color-accent); }
button, .btn { font: inherit; font-family: var(--font-ui); font-weight: 500; padding: var(--space-2) var(--space-4); background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); cursor: pointer; color: var(--color-text); display: inline-block; text-decoration: none; transition: background 100ms, border-color 100ms; }
a.btn { color: var(--color-text); }
a.btn:hover { color: var(--color-text); text-decoration: none; }
button:hover { background: var(--color-surface-alt); border-color: var(--color-border-strong); }
button.primary { background: var(--color-primary); color: white; border-color: var(--color-primary); }
button.primary:hover { background: var(--color-primary-hover); border-color: var(--color-primary-hover); }
button.danger { color: var(--color-error-600); }
button.danger:hover { color: var(--color-error-700); border-color: var(--color-error-600); background: var(--color-error-100); }
form { margin: 0; display: inline; }
input[type=search], input[type=text], textarea { font: inherit; font-family: var(--font-ui); padding: var(--space-3) var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-md); background: var(--color-surface); color: var(--color-text); }
input[type=search]:focus, input[type=text]:focus, textarea:focus { outline: 2px solid var(--color-accent); outline-offset: -1px; border-color: var(--color-accent); }
textarea { width: 100%; min-height: 14rem; font-family: var(--font-mono); font-size: 0.85rem; line-height: 1.5; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); overflow: hidden; box-shadow: var(--shadow-1); }
th, td { text-align: left; padding: var(--space-3) var(--space-4); border-bottom: 1px solid var(--color-border); vertical-align: top; }
th { background: var(--color-primary-50); font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--color-text-faint); font-weight: 600; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(0, 41, 89, 0.015); }
td.actions { text-align: right; white-space: nowrap; }
.badge { display: inline-block; padding: 0.15rem 0.55rem; border-radius: var(--radius-sm); font-size: 0.72rem; font-weight: 600; font-family: var(--font-ui); text-transform: uppercase; letter-spacing: 0.04em; }
.badge.ok { background: var(--color-success-100); color: var(--color-success-700); }
.badge.bad { background: var(--color-error-100); color: var(--color-error-700); }
.badge.dup { background: var(--color-warning-100); color: var(--color-warning-700); }
.badge.info { background: var(--color-info-100); color: var(--color-info-700); }
.dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
.dot.ok { background: var(--color-success-600); }
.dot.paused { background: var(--color-warning-600); }
.dot.running { background: var(--color-success-600); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(46,125,50,0.45); }
  50%      { opacity: 0.6; box-shadow: 0 0 0 6px rgba(46,125,50,0); }
}
.strip { display: flex; align-items: center; gap: var(--space-4); padding: var(--space-4) var(--space-5); background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); box-shadow: var(--shadow-1); }
.mute { color: var(--color-text-faint); font-size: 0.875rem; }
.grow { flex: 1; }
.h-suffix { color: var(--color-text-faint); font-weight: 400; text-transform: none; letter-spacing: 0; }
h1 .h-suffix { font-size: 1rem; }
h2 .h-suffix { font-size: 0.85rem; }
.tagline { color: var(--color-text-faint); font-size: 0.95rem; margin: calc(-1 * var(--space-1)) 0 var(--space-5); }
.empty { color: var(--color-text-faint); font-style: italic; padding: var(--space-4) var(--space-2); }
.flash { padding: var(--space-3) var(--space-4); background: var(--color-accent-100); border: 1px solid #FFE69C; border-left: 3px solid var(--color-accent); border-radius: var(--radius-sm); margin-bottom: var(--space-4); font-size: 0.9rem; color: var(--color-text); }
details summary { cursor: pointer; color: var(--color-primary-hover); font-size: 0.85rem; font-weight: 500; }
details summary:hover { color: var(--color-primary); }
details.settings { margin-top: var(--space-4); padding: var(--space-5); background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-lg); box-shadow: var(--shadow-1); }
details.settings summary { color: var(--color-text); font-weight: 600; font-size: 0.95rem; }
pre { background: #1A202C; color: #E2E8F0; padding: var(--space-3) var(--space-4); border-radius: var(--radius-md); overflow-x: auto; font-size: 0.8rem; line-height: 1.5; max-height: 300px; }
.livelog { background: #1A202C; color: #E2E8F0; font-family: var(--font-mono); font-size: 0.78rem; line-height: 1.55; padding: var(--space-3) var(--space-4); border-radius: var(--radius-md); max-height: 22rem; overflow-y: auto; border: 1px solid var(--color-border); }
.livelog .ev { display: flex; gap: var(--space-2); padding: 0.2rem 0; border-bottom: 1px solid #2D3748; }
.livelog .ev:last-child { border-bottom: none; }
.livelog .ev .icon { width: 1.6rem; flex-shrink: 0; text-align: center; }
.livelog .ev .text { white-space: pre-wrap; word-break: break-word; flex: 1; }
.livelog .empty { padding: var(--space-2); opacity: 0.6; color: #A0AEC0; }
.paths { font-size: 0.85rem; color: var(--color-text-muted); }
.paths > div { padding: 0.1rem 0; }
.paths code { font-size: 0.85rem; background: var(--color-surface-alt); padding: 0.1rem 0.35rem; border-radius: var(--radius-sm); color: var(--color-text-muted); }
.paths a { color: var(--color-primary-hover); text-decoration: none; }
.paths a:hover code { background: var(--color-primary-50); color: var(--color-primary); }
.cats { margin-top: var(--space-2); font-family: var(--font-mono); font-size: 0.85rem; color: var(--color-text-muted); }
.title { font-family: var(--font-ui); font-weight: 600; font-size: 1rem; line-height: 1.4; }
.title a { color: var(--color-text); text-decoration: none; }
.title a:hover { color: var(--color-primary); text-decoration: none; }
.meta { font-size: 0.82rem; color: var(--color-text-faint); margin-top: 0.2rem; }
.meta a { color: var(--color-text-faint); }
.meta a:hover { color: var(--color-text-muted); text-decoration: underline; }
.issue-section { margin-top: var(--space-6); }
.issue-section .desc { color: var(--color-text-muted); font-size: 0.9rem; margin: var(--space-2) 0 var(--space-3); }
.summary-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: var(--space-3); margin: var(--space-4) 0 var(--space-6); }
.tile { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-md); padding: var(--space-4); box-shadow: var(--shadow-1); }
.tile .n { font-family: var(--font-ui); font-size: 1.8rem; font-weight: 600; color: var(--color-primary); line-height: 1.1; }
.tile.ok .n { color: var(--color-success-700); }
.tile.warn .n { color: var(--color-warning-700); }
.tile.bad .n { color: var(--color-error-700); }
.tile .label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--color-text-faint); font-weight: 600; margin-top: var(--space-1); }
@media (max-width: 640px) {
  body { padding: var(--space-4); }
  nav.topnav { flex-wrap: wrap; gap: var(--space-3); }
  .strip { flex-wrap: wrap; }
}
</style>"""


def nav_html(active: str) -> str:
    home_class = ' class="active"' if active == "home" else ""
    lib_class = ' class="active"' if active == "library" else ""
    audit_class = ' class="active"' if active == "audit" else ""
    preprints_class = ' class="active"' if active == "preprints" else ""
    return f"""<nav class="topnav">
  <a href="/"{home_class}>Source Intake</a>
  <a href="/library"{lib_class}>Library</a>
  <a href="/preprints"{preprints_class}>Preprints</a>
  <a href="/audit"{audit_class}>Audit</a>
</nav>"""


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Source Intake</title>
{{ font_link | safe }}
{{ shared_styles | safe }}
</head>
<body>
{{ nav | safe }}
<h1>Source Intake</h1>
{% if domain %}<p class="tagline">{{ domain }}</p>{% endif %}

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

<div class="strip">
  {% if status.running %}
    <span><span class="dot running"></span>
      <strong>Processing</strong> <code>{{ status.running.name }}</code></span>
    <span class="mute" id="elapsed" data-start="{{ status.running.started_at }}">…</span>
  {% elif status.paused %}
    <span><span class="dot paused"></span><strong>Paused</strong></span>
    <span class="mute">{{ status.queue_inbox|length }} waiting in inbox</span>
  {% else %}
    <span><span class="dot ok"></span><strong>Watching</strong></span>
    <span class="mute">{% if status.queue_inbox %}{{ status.queue_inbox|length }} in inbox{% else %}idle{% endif %}</span>
  {% endif %}
  <span class="grow"></span>
  {% if status.paused %}
    <form method="post" action="/resume"><button class="primary">Resume</button></form>
  {% else %}
    <form method="post" action="/pause"><button>Pause</button></form>
  {% endif %}
  <form method="post" action="/trigger"><button>Run now</button></form>
</div>

{% if status.running %}
<h2>Live activity</h2>
<div id="livelog" class="livelog"><div class="empty">connecting…</div></div>
{% endif %}

<h2>Queue</h2>
{% if status.queue_inbox or status.queue_staged %}
<div class="table-scroll"><table>
  <thead><tr><th>File</th><th>Where</th><th>Size</th><th>Modified</th></tr></thead>
  <tbody>
    {% for f in status.queue_inbox %}
    <tr><td><code>{{ f.name }}</code></td><td>inbox</td><td>{{ f.size|filesizeformat }}</td><td>{{ f.mtime|tsfmt }}</td></tr>
    {% endfor %}
    {% for f in status.queue_staged %}
    <tr><td><code>{{ f.display }}</code></td><td><span class="dot running" style="vertical-align:middle"></span> processing</td><td>{{ f.size|filesizeformat }}</td><td>{{ f.mtime|tsfmt }}</td></tr>
    {% endfor %}
  </tbody>
</table></div>
{% else %}
<p class="empty">Empty — drop a file in <code>{{ inbox_path }}</code></p>
{% endif %}

<h2>Recent runs {% if status.total_cost %}<span class="h-suffix">— total ${{ '%.3f'|format(status.total_cost) }} across {{ status.runs|length }}</span>{% endif %}</h2>
{% if status.runs %}
<div class="table-scroll"><table>
  <thead><tr><th style="width:12rem">When</th><th>Input</th><th>Outcome</th><th style="width:6rem">Cost</th><th style="width:5rem">Time</th><th>Output / error</th></tr></thead>
  <tbody>
    {% for r in status.runs %}
    <tr>
      <td class="mute">{{ r.ts }}</td>
      <td>{% if r.output_paths_relative %}<a href="/source/{{ r.output_paths_relative[0]|urlencode }}"><code>{{ r.input_name }}</code></a>{% else %}<code>{{ r.input_name }}</code>{% endif %}</td>
      <td>
        {% if r.outcome == 'success' %}<span class="badge ok">success</span>
        {% elif r.outcome == 'duplicate' %}<span class="badge dup">duplicate</span>
        {% elif r.outcome == 'backfill' %}<span class="badge info">backfill</span>
        {% else %}<span class="badge bad">failure</span>{% endif %}
      </td>
      <td class="mute">{% if r.cost_usd %}${{ '%.3f'|format(r.cost_usd) }}{% else %}—{% endif %}</td>
      <td class="mute">{% if r.duration_ms %}{{ '%.1f'|format(r.duration_ms / 1000) }}s{% else %}—{% endif %}</td>
      <td>
        {% if r.output_paths_relative %}
          <div class="paths">
            {% for p in r.output_paths_relative %}<div><a href="/source/{{ p|urlencode }}"><code>{{ p }}</code></a></div>{% endfor %}
          </div>
        {% endif %}
        {% if r.error_excerpt %}
          <details><summary>error log</summary><pre>{{ r.error_excerpt }}</pre></details>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table></div>
{% else %}
<p class="empty">No runs yet.</p>
{% endif %}

{% if status.strays %}
<h2>Stray files in .staged/ <span class="h-suffix">— not part of any active run</span></h2>
<div class="table-scroll"><table>
  <thead><tr><th>File</th><th>Size</th><th>Modified</th><th class="actions">Actions</th></tr></thead>
  <tbody>
    {% for f in status.strays %}
    <tr>
      <td><code>{{ f.name }}</code></td>
      <td>{{ f.size|filesizeformat }}</td>
      <td class="mute">{{ f.mtime|tsfmt }}</td>
      <td class="actions">
        <form method="post" action="/discard-stray/{{ f.name|urlencode }}" onsubmit="return confirm('Delete {{ f.name }}?');"><button class="danger">Discard</button></form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table></div>
<p class="mute" style="margin-top:.5rem; font-size:.85rem">These files were left in <code>.staged/</code> without the UUID prefix the worker assigns to real jobs. The next worker tick will also sweep them automatically.</p>
{% endif %}

<h2>Failed items</h2>
{% if status.failed %}
<div class="table-scroll"><table>
  <thead><tr><th>File</th><th>Size</th><th>Modified</th><th>Log</th><th class="actions">Actions</th></tr></thead>
  <tbody>
    {% for f in status.failed %}
    <tr>
      <td><code>{{ f.name }}</code></td>
      <td>{{ f.size|filesizeformat }}</td>
      <td class="mute">{{ f.mtime|tsfmt }}</td>
      <td>{% if f.log %}<details><summary>view</summary><pre>{{ f.log }}</pre></details>{% else %}<span class="mute">none</span>{% endif %}</td>
      <td class="actions">
        <form method="post" action="/retry/{{ f.name|urlencode }}"><button class="primary">Retry</button></form>
        <form method="post" action="/discard/{{ f.name|urlencode }}" onsubmit="return confirm('Delete {{ f.name }} permanently?');"><button class="danger">Discard</button></form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table></div>
{% else %}
<p class="empty">Nothing has failed.</p>
{% endif %}

<h2>Duplicates</h2>
{% if status.duplicates %}
<div class="table-scroll"><table>
  <thead><tr><th>File</th><th>Size</th><th>Detected</th><th>Matched</th><th class="actions">Actions</th></tr></thead>
  <tbody>
    {% for f in status.duplicates %}
    <tr>
      <td><code>{{ f.name }}</code></td>
      <td>{{ f.size|filesizeformat }}</td>
      <td class="mute">{{ f.mtime|tsfmt }}</td>
      <td>{% if f.log %}<details><summary>view</summary><pre>{{ f.log }}</pre></details>{% else %}<span class="mute">none</span>{% endif %}</td>
      <td class="actions">
        <form method="post" action="/discard-duplicate/{{ f.name|urlencode }}" onsubmit="return confirm('Delete {{ f.name }}?');"><button class="danger">Discard</button></form>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table></div>
<p class="mute" style="margin-top:.5rem; font-size:.85rem">A file lands here when its bytes (or URL, for <code>.txt</code>/<code>.url</code> inputs) match an existing summary's <code>source_hash:</code> or <code>url:</code>. To force a re-process, delete the matched summary first, then move the file from <code>_duplicate/</code> back to the inbox.</p>
{% else %}
<p class="empty">No duplicates detected.</p>
{% endif %}

<details class="settings" id="lastrun-details">
  <summary>Last run details</summary>
  <p class="mute">Replay of the most recent <code>claude</code> invocation. Useful for inspecting a run after it finishes.</p>
  <div id="lastlog" class="livelog"><div class="empty">click to load</div></div>
</details>

<details class="settings">
  <summary>Settings</summary>
  <h2 style="margin-top:1rem">Autonomy prompt</h2>
  <form method="post" action="/prompt">
    <textarea name="content">{{ prompt }}</textarea>
    <p style="margin-top:.5rem"><button class="primary">Save prompt</button>
      <span class="mute">Stored at <code>{{ prompt_path }}</code></span></p>
  </form>
  <h2>Library categories</h2>
  <p class="cats">{{ status.categories|join('  •  ') }}</p>
  <p class="mute">New categories are created by the skill itself when no existing one fits.</p>
</details>

<script>
// Live elapsed-time ticker for the in-flight job
function fmtElapsed(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  const s = seconds % 60;
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600);
  if (h > 0) return `running ${h}h ${m}m ${s}s`;
  if (m > 0) return `running ${m}m ${s}s`;
  return `running ${s}s`;
}
const elapsedEl = document.getElementById("elapsed");
if (elapsedEl) {
  const start = parseFloat(elapsedEl.dataset.start);
  const tick = () => { elapsedEl.textContent = fmtElapsed(Date.now() / 1000 - start); };
  tick();
  setInterval(tick, 1000);
}

// Live activity log
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function loadLog(which, targetId) {
  const target = document.getElementById(targetId);
  if (!target) return;
  try {
    const r = await fetch(`/api/run-log?which=${which}`);
    const data = await r.json();
    if (!data.exists) { target.innerHTML = '<div class="empty">(no log file yet)</div>'; return; }
    if (!data.events.length) { target.innerHTML = '<div class="empty">(starting up…)</div>'; return; }
    const wasNearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 40;
    target.innerHTML = data.events.map(ev =>
      `<div class="ev"><span class="icon">${escapeHtml(ev.icon)}</span><span class="text">${escapeHtml(ev.text)}</span></div>`
    ).join('');
    if (wasNearBottom) target.scrollTop = target.scrollHeight;
  } catch (e) {
    target.innerHTML = `<div class="empty">(error: ${escapeHtml(e.message)})</div>`;
  }
}
{% if status.running %}
loadLog('current', 'livelog');
setInterval(() => loadLog('current', 'livelog'), 1500);
{% endif %}
const lastDetails = document.getElementById('lastrun-details');
if (lastDetails) {
  lastDetails.addEventListener('toggle', () => { if (lastDetails.open) loadLog('last', 'lastlog'); });
}

// Auto-refresh: re-poll /api/status every 3s and reload if anything changed.
// Tighter cadence (3s) so completion-of-run shows up promptly. We skip the reload
// while the user is touching the prompt textarea (focused or dirty) so a status
// change mid-edit doesn't blow away their work.
const promptEl = document.querySelector('textarea[name="content"]');
const promptOriginal = promptEl ? promptEl.value : "";
function userBusy() {
  if (!promptEl) return false;
  if (document.activeElement === promptEl) return true;
  return promptEl.value !== promptOriginal;
}
let lastSig = "{{ status.runs|length }}-{{ status.queue_inbox|length }}-{{ status.queue_staged|length }}-{{ status.strays|length }}-{{ status.failed|length }}-{{ status.duplicates|length }}-{{ 'p' if status.paused else 'w' }}-{{ 'r' if status.running else 'i' }}";
setInterval(async () => {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const s = await r.json();
    const sig = `${s.runs.length}-${s.queue_inbox.length}-${s.queue_staged.length}-${(s.strays||[]).length}-${s.failed.length}-${(s.duplicates||[]).length}-${s.paused ? 'p' : 'w'}-${s.running ? 'r' : 'i'}`;
    if (sig !== lastSig && !userBusy()) location.reload();
  } catch (_) {}
}, 3000);
</script>

</body>
</html>"""


@app.template_filter("filesizeformat")
def filesizeformat(b):
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


@app.template_filter("tsfmt")
def tsfmt(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


@app.route("/")
def index():
    flash = request.args.get("flash", "")
    prompt = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ""
    return render_template_string(
        TEMPLATE,
        status=status_payload(),
        prompt=prompt,
        prompt_path=str(PROMPT_FILE),
        inbox_path=str(INBOX),
        domain=os.environ.get("DOMAIN", "").strip(),
        flash=flash,
        nav=nav_html("home"),
        font_link=FONT_LINK,
        shared_styles=SHARED_STYLES,
    )


@app.route("/api/status")
def api_status():
    return jsonify(status_payload())


LIBRARY_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Library</title>
{{ font_link | safe }}
{{ shared_styles | safe }}
<style>
  .controls { position: sticky; top: 0; background: var(--color-surface-page); padding: var(--space-3) 0; z-index: 5; display: flex; gap: var(--space-4); align-items: center; border-bottom: 1px solid var(--color-border); margin-bottom: var(--space-4); }
  .controls input[type=search] { flex: 1; max-width: 30rem; font-size: 0.95rem; }
  .tldr { font-size: 0.92rem; line-height: 1.55; color: var(--color-text-muted); font-family: var(--font-ui); }
  .tags { margin-top: var(--space-2); display: flex; flex-wrap: wrap; gap: 0.3rem; }
  .tag { display: inline-block; padding: 0.1rem 0.5rem; background: var(--color-primary-50); color: var(--color-primary); border-radius: var(--radius-sm); font-size: 0.72rem; font-weight: 500; cursor: pointer; font-family: var(--font-ui); transition: background 100ms; }
  .tag:hover { background: #DBE5F0; }
  .hidden { display: none !important; }
</style>
</head>
<body>
{{ nav | safe }}
<h1>Library <span class="h-suffix">— {{ total }} sources across {{ by_cat|length }} {{ 'category' if by_cat|length == 1 else 'categories' }}</span></h1>
{% if domain %}<p class="tagline">{{ domain }}</p>{% endif %}

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

<div class="controls">
  <input id="filter" type="search" placeholder="Filter by title, tldr, author, tag, category…" autofocus>
  <span class="mute" id="match-count"></span>
</div>

<div id="nomatch" class="empty hidden">No sources match.</div>

{% for cat in cats %}
{% if cat in by_cat %}
<section data-cat="{{ cat }}">
  <h2>{{ cat }} <span class="h-suffix">({{ by_cat[cat]|length }})</span></h2>
  <div class="table-scroll"><table>
    <thead><tr><th style="width:24%">Title</th><th>Tl;dr</th><th style="width:8%">Year</th><th style="width:5rem"></th></tr></thead>
    <tbody>
      {% for s in by_cat[cat] %}
      <tr class="row" data-search="{{ (s.title ~ ' ' ~ s.tldr ~ ' ' ~ (s.authors|join(' ')) ~ ' ' ~ (s.tags|join(' ')) ~ ' ' ~ s.category)|lower }}">
        <td>
          <div class="title"><a href="/source/{{ s.rel_path|urlencode }}">{{ s.title }}</a></div>
          <div class="meta">
            {{ short_authors(s.authors) }}
            {% if s.url %} · <a href="{{ s.url }}" target="_blank" rel="noopener">url ↗</a>{% endif %}
            {% if s.source_file %} · <a href="/file/{{ s.source_file.rel_path|urlencode }}" target="_blank" rel="noopener">{{ s.source_file.kind }} ↗</a>{% endif %}
          </div>
          <div class="tags">{% for t in s.tags %}<span class="tag" onclick="setFilter('{{ t|e }}')">{{ t }}</span>{% endfor %}</div>
        </td>
        <td class="tldr">{{ s.tldr or '—' }}</td>
        <td class="mute">{{ s.year or '—' }}</td>
        <td class="actions">
          <a href="/source/{{ s.rel_path|urlencode }}" class="btn">View</a>
          <form method="post" action="/delete-source" onsubmit="return confirm('Permanently delete this source folder and all its files? INDEX.md will be regenerated.');" style="display:inline;margin-left:.25rem">
            <input type="hidden" name="rel_path" value="{{ s.rel_path }}">
            <button type="submit" class="danger">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}
{% endfor %}

{% for cat in empty %}
<section data-cat="{{ cat }}" data-empty="1">
  <h2>{{ cat }}</h2>
  <p class="empty">No sources yet.</p>
</section>
{% endfor %}

<script>
const filterEl = document.getElementById('filter');
const matchCountEl = document.getElementById('match-count');
const nomatchEl = document.getElementById('nomatch');
const allRows = Array.from(document.querySelectorAll('tr.row'));
const allSections = Array.from(document.querySelectorAll('section[data-cat]'));

function applyFilter() {
  const q = filterEl.value.trim().toLowerCase();
  let shown = 0;
  allRows.forEach(r => {
    const match = !q || r.dataset.search.includes(q);
    r.classList.toggle('hidden', !match);
    if (match) shown++;
  });
  // Hide sections whose visible row count is zero
  allSections.forEach(sec => {
    if (sec.dataset.empty) {
      sec.classList.toggle('hidden', !!q);
      return;
    }
    const visibleRows = sec.querySelectorAll('tr.row:not(.hidden)').length;
    sec.classList.toggle('hidden', visibleRows === 0);
  });
  matchCountEl.textContent = q ? `${shown} match${shown === 1 ? '' : 'es'}` : '';
  nomatchEl.classList.toggle('hidden', shown !== 0 || !q);
}
filterEl.addEventListener('input', applyFilter);
function setFilter(v) { filterEl.value = v; applyFilter(); filterEl.focus(); }
</script>
</body>
</html>"""


@app.route("/library")
def library_view():
    by_cat = collect_library_sources()
    cats_in_order = [c for c in CATEGORY_ORDER if c in by_cat] + sorted(
        c for c in by_cat if c not in CATEGORY_ORDER
    )
    if LIBRARY.exists():
        empty = sorted(
            p.name for p in LIBRARY.iterdir()
            if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_") and p.name not in by_cat
        )
    else:
        empty = []
    return render_template_string(
        LIBRARY_TEMPLATE,
        by_cat=by_cat,
        cats=cats_in_order,
        empty=empty,
        total=sum(len(v) for v in by_cat.values()),
        short_authors=short_authors,
        domain=os.environ.get("DOMAIN", "").strip(),
        nav=nav_html("library"),
        font_link=FONT_LINK,
        shared_styles=SHARED_STYLES,
        flash=request.args.get("flash", ""),
    )


SOURCE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ fm.title or rel_path }}</title>
{{ font_link | safe }}
{{ shared_styles | safe }}
<style>
  body { padding-bottom: var(--space-12); }
  .reading-column { max-width: 780px; margin: 0 auto; }
  .back-link { display: inline-block; margin-bottom: var(--space-4); font-family: var(--font-ui); font-size: 0.85rem; color: var(--color-text-muted); }
  .back-link:hover { color: var(--color-primary); }
  .viewer-header { margin-bottom: var(--space-6); padding-bottom: var(--space-4); border-bottom: 1px solid var(--color-border); }
  .viewer-eyebrow { font-family: var(--font-ui); font-size: 0.78rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--color-accent-text); margin-bottom: var(--space-3); }
  h1.viewer-title { font-family: var(--font-body); font-weight: 700; font-size: 2rem; line-height: 1.2; margin: 0 0 var(--space-3); color: var(--color-text); letter-spacing: -0.01em; }
  .viewer-meta { font-family: var(--font-ui); color: var(--color-text-faint); font-size: 0.9rem; margin-top: var(--space-3); }
  .viewer-meta a { color: var(--color-primary-hover); }
  .viewer-tags { display: flex; flex-wrap: wrap; gap: 0.3rem; margin-top: var(--space-3); }
  .viewer-tags .tag { display: inline-block; padding: 0.1rem 0.5rem; background: var(--color-primary-50); color: var(--color-primary); border-radius: var(--radius-sm); font-size: 0.72rem; font-weight: 500; font-family: var(--font-ui); }
  .viewer-tldr { font-family: var(--font-body); font-size: 1.08rem; line-height: 1.6; font-style: italic; padding: var(--space-4) var(--space-6); margin: var(--space-6) 0; background: var(--color-accent-100); border-left: 4px solid var(--color-accent); border-radius: var(--radius-sm); color: var(--color-text); }
  .prose { font-family: var(--font-body); font-size: 1rem; line-height: 1.7; color: var(--color-text); }
  .prose h1, .prose h2, .prose h3, .prose h4 { font-family: var(--font-ui); color: var(--color-primary); font-weight: 600; letter-spacing: -0.005em; line-height: 1.3; text-transform: none; margin-top: var(--space-8); margin-bottom: var(--space-3); }
  .prose h2 { font-size: 1.35rem; padding-bottom: var(--space-1); border-bottom: 1px solid var(--color-border); }
  .prose h3 { font-size: 1.1rem; }
  .prose h4 { font-size: 1rem; color: var(--color-text); }
  .prose p { margin: 0 0 var(--space-4); }
  .prose ul, .prose ol { padding-left: var(--space-6); margin: 0 0 var(--space-4); }
  .prose li { margin-bottom: var(--space-2); }
  .prose strong { color: var(--color-text); font-weight: 700; }
  .prose em { font-style: italic; }
  .prose code { font-family: var(--font-mono); font-size: 0.9em; background: var(--color-surface-alt); padding: 0.1rem 0.35rem; border-radius: var(--radius-sm); color: var(--color-text); }
  .prose pre { background: var(--color-surface-alt); color: var(--color-text); padding: var(--space-3) var(--space-4); border: 1px solid var(--color-border); border-radius: var(--radius-md); font-size: 0.85rem; max-height: none; }
  .prose blockquote { border-left: 3px solid var(--color-border-strong); margin: var(--space-4) 0; padding: var(--space-1) var(--space-5); color: var(--color-text-muted); font-style: italic; }
  .prose hr { border: none; border-top: 1px solid var(--color-border); margin: var(--space-6) 0; }
  .prose table { box-shadow: none; }
  .viewer-footer { margin-top: var(--space-12); padding-top: var(--space-4); border-top: 1px solid var(--color-border); font-family: var(--font-ui); font-size: 0.8rem; color: var(--color-text-faint); display: flex; gap: var(--space-4); align-items: center; flex-wrap: wrap; }
  .viewer-footer code { font-size: 0.75rem; }
  .viewer-footer .grow { flex: 1; }
</style>
</head>
<body>
{{ nav | safe }}
<div class="reading-column">
<a class="back-link" href="/library">← Back to Library</a>

<div class="viewer-header">
  {% if fm.category %}<div class="viewer-eyebrow">{{ fm.category }}{% if fm.source_type %} · {{ fm.source_type }}{% endif %}</div>{% endif %}
  <h1 class="viewer-title">{{ fm.title or rel_path }}</h1>
  <div class="viewer-meta">
    {% if authors_short %}{{ authors_short }}{% endif %}
    {% if fm.date %} · {{ fm.date }}{% endif %}
    {% if fm.publication %} · <span style="font-style:italic">{{ fm.publication }}</span>{% endif %}
    {% if fm.url %} · <a href="{{ fm.url }}" target="_blank" rel="noopener">url ↗</a>{% endif %}
    {% if source_file %} · <a href="/file/{{ source_file.rel_path|urlencode }}" target="_blank" rel="noopener">view {{ source_file.kind }} ↗</a>{% endif %}
  </div>
  {% if fm.tags %}
  <div class="viewer-tags">{% for t in fm.tags %}<span class="tag">{{ t }}</span>{% endfor %}</div>
  {% endif %}
</div>

{% if fm.tldr %}<div class="viewer-tldr">{{ fm.tldr }}</div>{% endif %}

<div class="prose">
{{ body_html | safe }}
</div>

<div class="viewer-footer">
  <span><code>{{ rel_path }}</code></span>
  <span class="grow"></span>
  <form method="post" action="/api/open"><input type="hidden" name="path" value="{{ rel_path }}"><button type="submit">Open externally</button></form>
  <form method="post" action="/delete-source" onsubmit="return confirm('Permanently delete this source folder and all its files? INDEX.md will be regenerated.');">
    <input type="hidden" name="rel_path" value="{{ rel_path }}">
    <button type="submit" class="danger">Delete source</button>
  </form>
</div>
</div>
</body>
</html>"""


def render_markdown(body: str) -> str:
    """Convert summary markdown to HTML, then strip anything not in the
    allowlist. Summaries are LLM-authored from arbitrary PDFs — without
    this pass a prompt-injecting source could inject scripts that run
    when the user opens the source page."""
    md = markdown.Markdown(extensions=["extra", "sane_lists", "smarty"], output_format="html5")
    raw_html = md.convert(body)
    return bleach.clean(
        raw_html,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRS,
        protocols=ALLOWED_HTML_PROTOCOLS,
        strip=True,
    )


@app.route("/source/<path:rel>")
def view_source(rel):
    if ".." in rel.split("/") or rel.startswith("/"):
        abort(400, "invalid path")
    target = (LIBRARY / rel).resolve()
    try:
        target.relative_to(LIBRARY.resolve())
    except ValueError:
        abort(400, "outside library")
    if not target.exists() or not target.is_file():
        abort(404)
    if target.suffix != ".md":
        abort(400, "only markdown sources are viewable")
    text = target.read_text()
    fm = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 5:]
    if not isinstance(fm, dict):
        fm = {}
    authors = fm.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    source_file = find_source_file(target) if target.name.endswith(".summary.md") else None
    return render_template_string(
        SOURCE_TEMPLATE,
        fm=fm,
        body_html=render_markdown(body),
        authors_short=short_authors(authors),
        rel_path=rel,
        source_file=source_file,
        nav=nav_html("library"),
        font_link=FONT_LINK,
        shared_styles=SHARED_STYLES,
    )


SERVABLE_EXTS = {".pdf", ".md", ".html", ".htm", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}


@app.route("/file/<path:rel>")
def serve_file(rel):
    """Serve a file inside the library inline (PDFs render natively in modern browsers)."""
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        abort(400, "invalid path")
    target = (LIBRARY / rel).resolve()
    try:
        target.relative_to(LIBRARY.resolve())
    except ValueError:
        abort(400, "outside library")
    if not target.exists() or not target.is_file():
        abort(404)
    if target.suffix.lower() not in SERVABLE_EXTS:
        abort(415, "file type not served")
    return send_file(str(target))


@app.route("/api/open", methods=["POST"])
def api_open():
    rel = request.form.get("path", "")
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        abort(400, "invalid path")
    target = (LIBRARY / rel).resolve()
    try:
        target.relative_to(LIBRARY.resolve())
    except ValueError:
        abort(400, "outside library")
    if not target.exists():
        abort(404)
    if target.suffix.lower() not in OPENABLE_EXTS:
        abort(415, "file type not openable")
    subprocess.Popen(["open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ("", 204)


@app.route("/api/run-log")
def api_run_log():
    which = request.args.get("which", "current")
    fname = "current-run.log" if which == "current" else "last-run.log"
    path = CONFIG / fname
    if not path.exists():
        return jsonify({"events": [], "exists": False})
    events = []
    try:
        with open(path) as f:
            for line in f:
                parsed = parse_event(line)
                if parsed:
                    events.extend(parsed)
    except Exception as ex:
        return jsonify({"events": [], "exists": True, "error": str(ex)})
    return jsonify({"events": events[-200:], "exists": True})


@app.route("/pause", methods=["POST"])
def pause():
    PAUSED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PAUSED_FLAG.touch()
    return redirect("/?flash=Watcher+paused")


@app.route("/resume", methods=["POST"])
def resume():
    if PAUSED_FLAG.exists():
        PAUSED_FLAG.unlink()
    return redirect("/?flash=Watcher+resumed")


@app.route("/trigger", methods=["POST"])
def trigger():
    # Route worker output to the same log files launchd writes to so
    # errors don't silently disappear when the user hits "Run now."
    # opened in append mode, closed when the child process exits.
    try:
        out = open(WORKER_OUT_LOG, "a")
        err = open(WORKER_ERR_LOG, "a")
    except OSError:
        out = err = subprocess.DEVNULL
    subprocess.Popen(
        ["/bin/bash", str(WORKER)],
        stdout=out, stderr=err,
        start_new_session=True,
    )
    return redirect("/?flash=Worker+triggered+%28check+/tmp+logs+for+errors%29")


@app.route("/retry/<path:filename>", methods=["POST"])
def retry(filename):
    name = safe_name(filename)
    src = FAILED / name
    if not src.exists():
        abort(404)
    dest = INBOX / name
    n = 1
    while dest.exists():
        stem, dot, ext = name.partition(".")
        dest = INBOX / (f"{stem}.{n}.{ext}" if dot else f"{name}.{n}")
        n += 1
    shutil.move(str(src), str(dest))
    log_src = FAILED / f"{name}.log"
    if log_src.exists():
        log_src.unlink()
    return redirect("/?flash=Retried+" + name)


@app.route("/discard/<path:filename>", methods=["POST"])
def discard(filename):
    name = safe_name(filename)
    target = FAILED / name
    if not target.exists():
        abort(404)
    target.unlink()
    log_path = FAILED / f"{name}.log"
    if log_path.exists():
        log_path.unlink()
    return redirect("/?flash=Discarded+" + name)


@app.route("/discard-duplicate/<path:filename>", methods=["POST"])
def discard_duplicate(filename):
    name = safe_name(filename)
    target = DUPLICATE / name
    if not target.exists():
        abort(404)
    target.unlink()
    log_path = DUPLICATE / f"{name}.log"
    if log_path.exists():
        log_path.unlink()
    return redirect("/?flash=Discarded+" + name)


@app.route("/discard-stray/<path:filename>", methods=["POST"])
def discard_stray(filename):
    name = safe_name(filename)
    # Reject UUID-prefixed files — those are real in-flight jobs the
    # worker is processing, not strays.
    if UUID_PREFIX.match(name):
        abort(400, "not a stray (UUID-prefixed)")
    target = STAGED / name
    if not target.exists():
        abort(404)
    target.unlink()
    return redirect("/?flash=Discarded+stray+" + name)


@app.route("/prompt", methods=["POST"])
def save_prompt():
    content = request.form.get("content", "")
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.write_text(content)
    return redirect("/?flash=Prompt+saved")


PREPRINTS_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preprint check</title>
{{ font_link | safe }}
{{ shared_styles | safe }}
<style>
  .conf-high { color: var(--color-success-700); font-weight: 600; }
  .conf-medium { color: var(--color-warning-700); font-weight: 600; }
  .conf-low { color: var(--color-error-700); font-weight: 600; }
  .venue { font-style: italic; }
  .note { color: var(--color-text-faint); font-size: 0.85rem; }
</style>
</head>
<body>
{{ nav | safe }}
<h1>Preprint check <span class="h-suffix">— peer-review tracking for arXiv / SSRN sources</span></h1>
<p class="tagline">For each preprint in the library, checks OpenAlex weekly for a peer-reviewed version. Conservative by design: low/medium confidence findings are flagged for your review, not auto-applied.</p>

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

<div class="strip">
  <span class="mute">{% if last_checked %}Most recent check: {{ last_checked }}{% else %}Not yet run.{% endif %}</span>
  <span class="grow"></span>
  <form method="post" action="/check-preprints"><button class="primary">Check now</button></form>
</div>
<p class="mute" style="margin-top:.5rem;font-size:.85rem">A weekly launchd agent refreshes stale entries automatically. "Check now" re-checks anything older than {{ refresh_days }} days. Network calls to OpenAlex; large libraries may take a minute.</p>

<div class="summary-tiles">
  <div class="tile ok">
    <div class="n">{{ stats.total }}</div>
    <div class="label">Preprints tracked</div>
  </div>
  <div class="tile {% if stats.published %}warn{% else %}ok{% endif %}">
    <div class="n">{{ stats.published }}</div>
    <div class="label">Likely published</div>
  </div>
  <div class="tile ok">
    <div class="n">{{ stats.preprint_only }}</div>
    <div class="label">Preprint-only</div>
  </div>
  <div class="tile">
    <div class="n">{{ stats.unknown }}</div>
    <div class="label">Unknown</div>
  </div>
  <div class="tile {% if stats.errors %}bad{% else %}ok{% endif %}">
    <div class="n">{{ stats.errors }}</div>
    <div class="label">Errors</div>
  </div>
  <div class="tile {% if stats.unchecked %}warn{% else %}ok{% endif %}">
    <div class="n">{{ stats.unchecked }}</div>
    <div class="label">Not yet checked</div>
  </div>
</div>

{% if not entries %}
<p class="empty">No preprints in the library yet. The check picks up summaries whose URL is on <code>arxiv.org</code> or <code>ssrn.com</code>, or whose <code>source_type:</code> is <code>preprint</code>.</p>
{% else %}

{% if published %}
<section class="issue-section">
  <h2>Likely published <span class="h-suffix">— {{ published|length }} — review and consider updating <code>superseded_by:</code></span></h2>
  <p class="note">When a preprint also appears in a peer-reviewed venue, OpenAlex usually has both. Confidence reflects how well we matched titles + arXiv ids. Click through to verify before treating any single hit as canonical — OpenAlex's data for CS/ML is patchy.</p>
  <div class="table-scroll"><table>
    <thead><tr><th style="width:24%">Preprint</th><th>Found at</th><th style="width:7rem">Conf.</th><th style="width:9rem">Checked</th></tr></thead>
    <tbody>
      {% for e in published %}
      <tr>
        <td>
          <div class="title"><a href="/source/{{ e.rel_path|urlencode }}">{{ e.title }}</a></div>
          <div class="meta">
            {% if e.preprint_url %}<a href="{{ e.preprint_url }}" target="_blank" rel="noopener">{{ e.preprint_venue }} ↗</a>{% else %}{{ e.preprint_venue }}{% endif %}
            {% if e.preprint_id %} · <code>{{ e.preprint_id }}</code>{% endif %}
          </div>
        </td>
        <td>
          <div><span class="venue">{{ e.publication }}</span> <span class="mute">({{ e.venue_type }}{% if e.version %} · {{ e.version }}{% endif %})</span></div>
          <div class="meta">
            {% if e.published_url %}<a href="{{ e.published_url }}" target="_blank" rel="noopener">{{ e.published_url }} ↗</a>{% endif %}
            {% if e.doi %} · DOI <code>{{ e.doi }}</code>{% endif %}
          </div>
        </td>
        <td><span class="conf-{{ e.confidence or 'low' }}">{{ (e.confidence or 'low')|capitalize }}</span>{% if e.match_score %} <span class="mute">{{ '%.2f'|format(e.match_score) }}</span>{% endif %}</td>
        <td class="mute">{{ e.checked_ago }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

{% if unknown %}
<section class="issue-section">
  <h2>Unknown <span class="h-suffix">— {{ unknown|length }} — not indexed or no high-confidence match</span></h2>
  <p class="note">OpenAlex returned no match (or none that cleared the title-similarity + author cross-check threshold). Worth a manual look if the source is recent or in a non-mainstream venue.</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Preprint</th><th>Note</th><th style="width:9rem">Checked</th></tr></thead>
    <tbody>
      {% for e in unknown %}
      <tr>
        <td>
          <div class="title"><a href="/source/{{ e.rel_path|urlencode }}">{{ e.title }}</a></div>
          <div class="meta">{% if e.preprint_url %}<a href="{{ e.preprint_url }}" target="_blank" rel="noopener">{{ e.preprint_venue }} ↗</a>{% else %}{{ e.preprint_venue }}{% endif %}{% if e.preprint_id %} · <code>{{ e.preprint_id }}</code>{% endif %}</div>
        </td>
        <td class="note">{{ e.note or '—' }}</td>
        <td class="mute">{{ e.checked_ago }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

{% if errors %}
<section class="issue-section">
  <h2>Errors <span class="h-suffix">— {{ errors|length }}</span></h2>
  <p class="note">Transient network or API failures — they'll retry on the next check.</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Preprint</th><th>Error</th><th style="width:9rem">Checked</th></tr></thead>
    <tbody>
      {% for e in errors %}
      <tr>
        <td><a href="/source/{{ e.rel_path|urlencode }}">{{ e.title }}</a></td>
        <td class="note">{{ e.error }}</td>
        <td class="mute">{{ e.checked_ago }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

<details class="settings" style="margin-top:2rem">
  <summary>Preprint-only ({{ preprint_only|length }})</summary>
  <p class="note">Found in OpenAlex but with no peer-reviewed location — i.e. the preprint hasn't been published yet (or OpenAlex hasn't indexed the published version).</p>
  <div class="table-scroll"><table style="margin-top:1rem">
    <thead><tr><th>Preprint</th><th>Venue</th><th style="width:9rem">Checked</th></tr></thead>
    <tbody>
      {% for e in preprint_only %}
      <tr>
        <td><a href="/source/{{ e.rel_path|urlencode }}">{{ e.title }}</a></td>
        <td>{% if e.preprint_url %}<a href="{{ e.preprint_url }}" target="_blank" rel="noopener">{{ e.preprint_venue }} ↗</a>{% else %}{{ e.preprint_venue }}{% endif %}{% if e.preprint_id %} · <code>{{ e.preprint_id }}</code>{% endif %}</td>
        <td class="mute">{{ e.checked_ago }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</details>

{% if unchecked %}
<details class="settings">
  <summary>Not yet checked ({{ unchecked|length }})</summary>
  <p class="note">Detected in the library but not yet in the cache. The next "Check now" or weekly run will pick them up.</p>
  <ul>
    {% for e in unchecked %}
    <li><a href="/source/{{ e.rel_path|urlencode }}">{{ e.rel_path }}</a></li>
    {% endfor %}
  </ul>
</details>
{% endif %}

{% endif %}

</body>
</html>"""


def preprint_view_data():
    """Read the cache and group entries by status for template rendering.

    Also detects library preprints that aren't in the cache yet (new since the
    last check) so we can show an "unchecked" bucket. Detection mirrors
    scripts/check-preprints.py's classify() — we don't re-import to keep the
    dashboard process free of network code, just duplicate the simple regex.
    """
    cache = load_preprint_cache()
    discovered_rels: set[str] = set()
    if LIBRARY.exists():
        for summary in LIBRARY.glob("*/*/*.summary.md"):
            category = summary.parent.parent.name
            if category.startswith(".") or category.startswith("_"):
                continue
            fm = parse_frontmatter(summary)
            if fm is None:
                continue
            url = (fm.get("url") or "").lower()
            source_type = (fm.get("source_type") or "").lower()
            publication = (fm.get("publication") or "").lower()
            looks_preprint = (
                "arxiv.org" in url or "ssrn.com" in url
                or source_type == "preprint"
                or publication.startswith(("arxiv", "ssrn", "preprint"))
            )
            if looks_preprint:
                discovered_rels.add(summary.relative_to(LIBRARY).as_posix())

    now = time.time()

    def fmt_ago(ts_str: str) -> str:
        if not ts_str:
            return "—"
        try:
            t = time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            return ts_str
        delta = now - t
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"

    entries = []
    for rel, v in cache.items():
        entries.append({
            "rel_path": rel,
            "title": v.get("title") or rel,
            "preprint_venue": v.get("preprint_venue") or "",
            "preprint_id": v.get("preprint_id") or "",
            "preprint_url": v.get("preprint_url") or "",
            "status": v.get("status") or "unknown",
            "publication": v.get("publication") or "",
            "venue_type": v.get("venue_type") or "",
            "version": v.get("version") or "",
            "published_url": v.get("published_url") or "",
            "doi": v.get("doi") or "",
            "confidence": v.get("confidence") or "",
            "match_score": v.get("match_score") or 0,
            "note": v.get("note") or "",
            "error": v.get("error") or "",
            "checked_at": v.get("checked_at") or "",
            "checked_ago": fmt_ago(v.get("checked_at") or ""),
        })
    entries.sort(key=lambda e: (e["status"] != "published", e["title"].lower()))
    published = [e for e in entries if e["status"] == "published"]
    preprint_only = [e for e in entries if e["status"] == "preprint-only"]
    unknown = [e for e in entries if e["status"] == "unknown"]
    errors = [e for e in entries if e["status"] == "error"]
    unchecked = [
        {"rel_path": rel} for rel in sorted(discovered_rels - set(cache.keys()))
    ]
    last_checked_ts = max(
        (e["checked_at"] for e in entries if e["checked_at"]), default=""
    )
    last_checked = fmt_ago(last_checked_ts) if last_checked_ts else ""
    stats = {
        "total": len(entries) + len(unchecked),
        "published": len(published),
        "preprint_only": len(preprint_only),
        "unknown": len(unknown),
        "errors": len(errors),
        "unchecked": len(unchecked),
    }
    return {
        "entries": entries,
        "published": published,
        "preprint_only": preprint_only,
        "unknown": unknown,
        "errors": errors,
        "unchecked": unchecked,
        "stats": stats,
        "last_checked": last_checked,
    }


AUDIT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Library audit</title>
{{ font_link | safe }}
{{ shared_styles | safe }}
<style>
  .all-clear { background: var(--color-success-100); border: 1px solid #C8E6C9; border-left: 4px solid var(--color-success-600); border-radius: var(--radius-md); padding: var(--space-4) var(--space-5); color: var(--color-success-700); margin: var(--space-4) 0; }
</style>
</head>
<body>
{{ nav | safe }}
<h1>Library audit</h1>
<p class="tagline">Cross-checks <code>INDEX.md</code> against what's on disk and flags missing originals or broken frontmatter.</p>

{% if flash %}<div class="flash">{{ flash }}</div>{% endif %}

<div class="strip">
  <span class="mute">Library: <code>{{ library_path }}</code></span>
  <span class="grow"></span>
  <form method="post" action="/regen-index"><button class="primary">Regenerate INDEX.md</button></form>
</div>

{% if not findings.library_exists %}
<div class="flash" style="background:var(--color-error-100); border-color:var(--color-error-600); color:var(--color-error-700)">
  Library directory does not exist at <code>{{ library_path }}</code>. Check <code>LIBRARY_PATH</code> in the launchd plist.
</div>
{% else %}

<div class="summary-tiles">
  <div class="tile ok">
    <div class="n">{{ findings.summaries|length }}</div>
    <div class="label">Summaries on disk</div>
  </div>
  <div class="tile {% if findings.index_orphans %}bad{% else %}ok{% endif %}">
    <div class="n">{{ findings.index_orphans|length }}</div>
    <div class="label">Stale INDEX entries</div>
  </div>
  <div class="tile {% if findings.unindexed %}warn{% else %}ok{% endif %}">
    <div class="n">{{ findings.unindexed|length }}</div>
    <div class="label">Unindexed summaries</div>
  </div>
  <div class="tile {% if findings.missing_originals %}warn{% else %}ok{% endif %}">
    <div class="n">{{ findings.missing_originals|length }}</div>
    <div class="label">Missing originals</div>
  </div>
  <div class="tile {% if findings.bad_frontmatter %}bad{% else %}ok{% endif %}">
    <div class="n">{{ findings.bad_frontmatter|length }}</div>
    <div class="label">Bad frontmatter</div>
  </div>
</div>

{% set total_issues = findings.index_orphans|length + findings.unindexed|length + findings.missing_originals|length + findings.bad_frontmatter|length %}
{% if total_issues == 0 %}
<div class="all-clear">✔ All clear. {{ findings.summaries|length }} summaries on disk, all indexed, all with original sidecars.</div>
{% endif %}

{% if findings.index_orphans %}
<section class="issue-section">
  <h2>Stale INDEX entries <span class="h-suffix">— {{ findings.index_orphans|length }}</span></h2>
  <p class="desc">These paths are linked from <code>INDEX.md</code> but no <code>.summary.md</code> exists at them on disk. Almost always: a source folder was deleted by hand and INDEX.md hasn't been regenerated since. Click <strong>Regenerate INDEX.md</strong> above to clean them up.</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Path referenced in INDEX.md</th></tr></thead>
    <tbody>
      {% for p in findings.index_orphans %}
      <tr><td><code>{{ p }}</code></td></tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

{% if findings.unindexed %}
<section class="issue-section">
  <h2>Unindexed summaries <span class="h-suffix">— {{ findings.unindexed|length }}</span></h2>
  <p class="desc">Summary files that exist on disk but aren't linked from <code>INDEX.md</code>. Usually means INDEX.md is stale (regenerate) or the summary's frontmatter doesn't parse (see "Bad frontmatter" below).</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Path on disk</th><th class="actions">Actions</th></tr></thead>
    <tbody>
      {% for p in findings.unindexed %}
      <tr>
        <td><a href="/source/{{ p|urlencode }}"><code>{{ p }}</code></a></td>
        <td class="actions"><a href="/source/{{ p|urlencode }}" class="btn">View</a></td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

{% if findings.missing_originals %}
<section class="issue-section">
  <h2>Missing originals <span class="h-suffix">— {{ findings.missing_originals|length }}</span></h2>
  <p class="desc">Summaries that have no sibling <code>.pdf</code> or <code>.snapshot.md</code>. The summary alone is fine to keep, but you've lost the source artifact. To restore: drop a PDF in the inbox &mdash; if its filename matches the summary's slug, title, or URL (e.g. an arxiv ID), the worker files it back in place instead of treating it as a new source. Otherwise, delete the summary and re-process from the inbox.</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Title</th><th>Category</th><th>Path</th><th class="actions">Actions</th></tr></thead>
    <tbody>
      {% for s in findings.missing_originals %}
      <tr>
        <td>{{ s.title }}</td>
        <td class="mute">{{ s.category }}</td>
        <td><code>{{ s.rel_path }}</code></td>
        <td class="actions">
          <a href="/source/{{ s.rel_path|urlencode }}" class="btn">View</a>
          <form method="post" action="/delete-source" onsubmit="return confirm('Permanently delete this source folder? INDEX.md will be regenerated.');" style="display:inline;margin-left:.25rem">
            <input type="hidden" name="rel_path" value="{{ s.rel_path }}">
            <button type="submit" class="danger">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

{% if findings.bad_frontmatter %}
<section class="issue-section">
  <h2>Bad frontmatter <span class="h-suffix">— {{ findings.bad_frontmatter|length }}</span></h2>
  <p class="desc">Summary files whose YAML frontmatter is missing or can't be parsed. <code>regen-index.py</code> silently skips these, which is why they may also appear under "Unindexed".</p>
  <div class="table-scroll"><table>
    <thead><tr><th>Path</th><th>Category</th></tr></thead>
    <tbody>
      {% for s in findings.bad_frontmatter %}
      <tr><td><code>{{ s.rel_path }}</code></td><td class="mute">{{ s.category }}</td></tr>
      {% endfor %}
    </tbody>
  </table></div>
</section>
{% endif %}

<details class="settings" style="margin-top:2rem">
  <summary>All summaries ({{ findings.summaries|length }})</summary>
  <div class="table-scroll"><table style="margin-top:1rem">
    <thead><tr><th>Title</th><th>Category</th><th>Original</th><th class="actions">Actions</th></tr></thead>
    <tbody>
      {% for s in findings.summaries %}
      <tr>
        <td><a href="/source/{{ s.rel_path|urlencode }}">{{ s.title }}</a></td>
        <td class="mute">{{ s.category }}</td>
        <td>{% if s.original_kind %}<span class="badge ok">{{ s.original_kind }}</span>{% else %}<span class="badge bad">missing</span>{% endif %}</td>
        <td class="actions">
          <form method="post" action="/delete-source" onsubmit="return confirm('Permanently delete this source folder? INDEX.md will be regenerated.');" style="display:inline">
            <input type="hidden" name="rel_path" value="{{ s.rel_path }}">
            <button type="submit" class="danger">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table></div>
</details>
{% endif %}
</body>
</html>"""


@app.route("/preprints")
def preprints_view():
    data = preprint_view_data()
    return render_template_string(
        PREPRINTS_TEMPLATE,
        **data,
        refresh_days=int(os.environ.get("PREPRINT_REFRESH_DAYS", "7")),
        nav=nav_html("preprints"),
        font_link=FONT_LINK,
        shared_styles=SHARED_STYLES,
        flash=request.args.get("flash", ""),
    )


@app.route("/check-preprints", methods=["POST"])
def check_preprints_route():
    ok, msg = check_preprints_now()
    flash = ("Preprint check complete: " if ok else "Preprint check failed: ") + msg
    safe = flash.replace("&", "and").replace("#", "")
    return redirect(f"/preprints?flash={safe}")


@app.route("/audit")
def audit_view():
    findings = audit_library()
    return render_template_string(
        AUDIT_TEMPLATE,
        findings=findings,
        library_path=str(LIBRARY),
        nav=nav_html("audit"),
        font_link=FONT_LINK,
        shared_styles=SHARED_STYLES,
        flash=request.args.get("flash", ""),
    )


@app.route("/regen-index", methods=["POST"])
def regen_index_route():
    ok, msg = regen_index_now()
    flash = ("INDEX.md regenerated: " if ok else "Regenerate failed: ") + msg
    # urlencode-safe via Flask's redirect (which doesn't auto-encode the query);
    # use replace as a minimal guard against fragment / ampersand confusion.
    safe = flash.replace("&", "and").replace("#", "")
    return redirect(f"/audit?flash={safe}")


@app.route("/delete-source", methods=["POST"])
def delete_source():
    rel = request.form.get("rel_path", "")
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        abort(400, "invalid path")
    target = (LIBRARY / rel).resolve()
    try:
        target.relative_to(LIBRARY.resolve())
    except ValueError:
        abort(400, "outside library")
    if not target.exists() or not target.is_file():
        abort(404)
    if not target.name.endswith(".summary.md"):
        abort(400, "not a summary path")
    folder = target.parent
    try:
        parts = folder.relative_to(LIBRARY.resolve()).parts
    except ValueError:
        abort(400, "outside library")
    # Must be exactly <category>/<slug>/. Any other depth is suspicious.
    if len(parts) != 2:
        abort(400, "unexpected folder depth")
    category, slug = parts
    if category.startswith(".") or category.startswith("_"):
        abort(400, "category is private")
    if not slug or slug.startswith(".") or slug.startswith("_"):
        abort(400, "invalid slug")
    shutil.rmtree(folder)
    ok, msg = regen_index_now()
    flash = f"Deleted {category}/{slug}"
    if not ok:
        flash += f" (warning: index regen failed — {msg})"
    return redirect("/library?flash=" + flash.replace("&", "and").replace("#", "").replace(" ", "+"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
