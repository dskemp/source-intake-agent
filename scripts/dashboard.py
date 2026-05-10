#!/usr/bin/env python3
"""Local dashboard for the autonomous source-intake pipeline.

Binds to 127.0.0.1:7341 only. No auth - relies on localhost-only binding.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, redirect, render_template_string, request

HOME = Path.home()
INBOX = Path(os.environ.get("INBOX_PATH") or (HOME / "source-library-inbox"))
STAGED = INBOX / ".staged"
FAILED = INBOX / "_failed"
LIBRARY = Path(os.environ.get("LIBRARY_PATH") or (HOME / "source-library"))
CONFIG = HOME / ".config/claude-source-intake"
PROMPT_FILE = CONFIG / "prompt.txt"
PAUSED_FLAG = CONFIG / "paused"
RUNS_LOG = CONFIG / "runs.jsonl"
WORKER = HOME / "Library/Scripts/claude-source-intake.sh"

PORT = 7341

UUID_PREFIX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-")


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
    lines = RUNS_LOG.read_text().splitlines()
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
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
        })
    for sources in by_cat.values():
        sources.sort(key=lambda s: (s["date"], s["title"]), reverse=True)
    return by_cat


def short_authors(authors, max_shown=3):
    if not authors:
        return ""
    surnames = [a.split(",")[0].strip() if "," in a else a.split()[-1] for a in authors]
    if len(surnames) <= max_shown:
        return ", ".join(surnames)
    return ", ".join(surnames[:max_shown]) + " et al."


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
    staged = list_dir(STAGED)
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
        "running": running,
        "runs": runs,
        "total_cost": total_cost,
        "failed": list_failed(),
        "duplicates": list_duplicates(),
        "categories": list_categories(),
    }


app = Flask(__name__)


NAV = """<nav style="margin-bottom:1rem; font-size:.95rem">
  <a href="/" style="margin-right:1rem; color:{home_color}; font-weight:{home_weight}; text-decoration:none">Source Intake</a>
  <a href="/library" style="color:{lib_color}; font-weight:{lib_weight}; text-decoration:none">Library</a>
</nav>"""

NAV_HOME = NAV.format(home_color="#222", home_weight=600, lib_color="#1565c0", lib_weight=400)
NAV_LIB = NAV.format(home_color="#1565c0", home_weight=400, lib_color="#222", lib_weight=600)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Source Intake</title>
<style>
  :root { --bg:#fafaf7; --fg:#222; --mute:#666; --line:#ddd; --ok:#2e7d32; --bad:#c62828; --accent:#1565c0; }
  body { font: 14px/1.45 -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 1.5rem 2rem; max-width: 1100px; }
  h1 { font-size: 1.4rem; margin: 0 0 1rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: var(--mute); text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
  .strip { display: flex; align-items: center; gap: 1rem; padding: .75rem 1rem; background: white; border: 1px solid var(--line); border-radius: 6px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
  .dot.ok { background: var(--ok); }
  .dot.paused { background: #ed8a00; }
  .dot.running { background: var(--ok); animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(46,125,50,.5); }
    50% { opacity: .55; box-shadow: 0 0 0 6px rgba(46,125,50,0); }
  }
  button, .btn { font: inherit; padding: .35rem .75rem; background: white; border: 1px solid var(--line); border-radius: 4px; cursor: pointer; color: var(--fg); }
  button:hover { background: #f0f0ec; }
  button.primary { background: var(--accent); color: white; border-color: var(--accent); }
  button.primary:hover { background: #0d4a91; }
  button.danger { color: var(--bad); }
  table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { background: #f0f0ec; font-size: .85rem; color: var(--mute); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.actions { text-align: right; white-space: nowrap; }
  .badge { display: inline-block; padding: .1rem .5rem; border-radius: 3px; font-size: .8rem; font-weight: 600; }
  .badge.ok { background: #e8f5e9; color: var(--ok); }
  .badge.bad { background: #ffebee; color: var(--bad); }
  .badge.dup { background: #fff4e5; color: #c66800; }
  .empty { color: var(--mute); font-style: italic; padding: .75rem; }
  details { margin-top: .25rem; }
  details summary { cursor: pointer; color: var(--accent); font-size: .85rem; }
  pre { background: #1e1e1e; color: #ddd; padding: .75rem; border-radius: 4px; overflow-x: auto; font-size: .8rem; max-height: 300px; }
  textarea { width: 100%; min-height: 14rem; font: 13px/1.4 ui-monospace, Menlo, monospace; padding: .75rem; border: 1px solid var(--line); border-radius: 4px; box-sizing: border-box; }
  form { margin: 0; display: inline; }
  .mute { color: var(--mute); font-size: .85rem; }
  .grow { flex: 1; }
  .flash { padding: .5rem .75rem; background: #fff3cd; border: 1px solid #ffe69c; border-radius: 4px; margin-bottom: 1rem; font-size: .9rem; }
  ul.paths { margin: .25rem 0; padding-left: 1.25rem; font-size: .85rem; color: var(--mute); }
  ul.paths code { font-size: .85rem; }
  details.settings { margin-top: 1rem; padding: 1rem; background: white; border: 1px solid var(--line); border-radius: 6px; }
  details.settings summary { color: var(--fg); font-weight: 600; }
  .livelog { background: #1e1e1e; color: #d4d4d4; font: 12px/1.5 ui-monospace, Menlo, Monaco, monospace; padding: .5rem .75rem; border-radius: 6px; max-height: 22rem; overflow-y: auto; border: 1px solid var(--line); }
  .livelog .ev { display: flex; gap: .5rem; padding: .15rem 0; border-bottom: 1px solid #2a2a2a; }
  .livelog .ev:last-child { border-bottom: none; }
  .livelog .ev .icon { width: 1.6rem; flex-shrink: 0; text-align: center; }
  .livelog .ev .text { white-space: pre-wrap; word-break: break-word; flex: 1; }
  .livelog .empty { padding: .5rem; opacity: .6; }
  .cats { margin-top: .5rem; font-family: ui-monospace, Menlo, monospace; font-size: .85rem; color: var(--mute); }
</style>
</head>
<body>
{{ nav | safe }}
<h1>Source Intake</h1>

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
<table>
  <thead><tr><th>File</th><th>Where</th><th>Size</th><th>Modified</th></tr></thead>
  <tbody>
    {% for f in status.queue_inbox %}
    <tr><td><code>{{ f.name }}</code></td><td>inbox</td><td>{{ f.size|filesizeformat }}</td><td>{{ f.mtime|tsfmt }}</td></tr>
    {% endfor %}
    {% for f in status.queue_staged %}
    <tr><td><code>{{ f.display }}</code></td><td><span class="dot running" style="vertical-align:middle"></span> processing</td><td>{{ f.size|filesizeformat }}</td><td>{{ f.mtime|tsfmt }}</td></tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">Empty — drop a file in <code>{{ inbox_path }}</code></p>
{% endif %}

<h2>Recent runs {% if status.total_cost %}<span class="mute" style="font-weight:400; text-transform:none; letter-spacing:0; font-size:.85rem">— total ${{ '%.3f'|format(status.total_cost) }} across {{ status.runs|length }}</span>{% endif %}</h2>
{% if status.runs %}
<table>
  <thead><tr><th style="width:12rem">When</th><th>Input</th><th>Outcome</th><th style="width:6rem">Cost</th><th style="width:5rem">Time</th><th>Output / error</th></tr></thead>
  <tbody>
    {% for r in status.runs %}
    <tr>
      <td class="mute">{{ r.ts }}</td>
      <td><code>{{ r.input_name }}</code></td>
      <td>
        {% if r.outcome == 'success' %}<span class="badge ok">success</span>
        {% elif r.outcome == 'duplicate' %}<span class="badge dup">duplicate</span>
        {% else %}<span class="badge bad">failure</span>{% endif %}
      </td>
      <td class="mute">{% if r.cost_usd %}${{ '%.3f'|format(r.cost_usd) }}{% else %}—{% endif %}</td>
      <td class="mute">{% if r.duration_ms %}{{ '%.1f'|format(r.duration_ms / 1000) }}s{% else %}—{% endif %}</td>
      <td>
        {% if r.output_paths %}
          <ul class="paths">{% for p in r.output_paths %}<li><code>{{ p }}</code></li>{% endfor %}</ul>
        {% endif %}
        {% if r.error_excerpt %}
          <details><summary>error log</summary><pre>{{ r.error_excerpt }}</pre></details>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">No runs yet.</p>
{% endif %}

<h2>Failed items</h2>
{% if status.failed %}
<table>
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
</table>
{% else %}
<p class="empty">Nothing has failed.</p>
{% endif %}

<h2>Duplicates</h2>
{% if status.duplicates %}
<table>
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
</table>
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
// Tighter cadence (3s) so completion-of-run shows up promptly.
let lastSig = "{{ status.runs|length }}-{{ status.queue_inbox|length }}-{{ status.queue_staged|length }}-{{ status.failed|length }}-{{ status.duplicates|length }}-{{ 'p' if status.paused else 'w' }}-{{ 'r' if status.running else 'i' }}";
setInterval(async () => {
  try {
    const r = await fetch("/api/status");
    if (!r.ok) return;
    const s = await r.json();
    const sig = `${s.runs.length}-${s.queue_inbox.length}-${s.queue_staged.length}-${s.failed.length}-${(s.duplicates||[]).length}-${s.paused ? 'p' : 'w'}-${s.running ? 'r' : 'i'}`;
    if (sig !== lastSig) location.reload();
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
        flash=flash,
        nav=NAV_HOME,
    )


@app.route("/api/status")
def api_status():
    return jsonify(status_payload())


LIBRARY_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Library</title>
<style>
  :root { --bg:#fafaf7; --fg:#222; --mute:#666; --line:#ddd; --accent:#1565c0; }
  body { font: 14px/1.45 -apple-system, system-ui, sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 1.5rem 2rem; max-width: 1300px; }
  h1 { font-size: 1.4rem; margin: 0 0 1rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 .5rem; color: var(--mute); text-transform: uppercase; letter-spacing: .05em; font-weight: 600; }
  .controls { position: sticky; top: 0; background: var(--bg); padding-bottom: .5rem; z-index: 5; display: flex; gap: 1rem; align-items: center; }
  .controls input[type=search] { font: inherit; padding: .5rem .75rem; border: 1px solid var(--line); border-radius: 6px; flex: 1; max-width: 30rem; background: white; }
  .mute { color: var(--mute); font-size: .9rem; }
  table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
  th, td { text-align: left; padding: .6rem .75rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { background: #f0f0ec; font-size: .85rem; color: var(--mute); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  td.actions { text-align: right; white-space: nowrap; }
  .title { font-weight: 600; }
  .title a { color: var(--fg); text-decoration: none; }
  .title a:hover { color: var(--accent); }
  .tldr { font-size: .9rem; line-height: 1.5; }
  .meta { font-size: .85rem; color: var(--mute); margin-top: .25rem; }
  .tags { margin-top: .35rem; display: flex; flex-wrap: wrap; gap: .3rem; }
  .tag { display: inline-block; padding: .05rem .45rem; background: #eef3f8; color: #1d4d80; border-radius: 3px; font-size: .75rem; cursor: pointer; }
  .tag:hover { background: #d6e4f1; }
  button { font: inherit; padding: .25rem .6rem; background: white; border: 1px solid var(--line); border-radius: 4px; cursor: pointer; color: var(--fg); }
  button:hover { background: #f0f0ec; }
  .empty { color: var(--mute); font-style: italic; padding: 1rem; }
  .hidden { display: none !important; }
  .nomatch { padding: 2rem; text-align: center; color: var(--mute); font-style: italic; }
</style>
</head>
<body>
{{ nav | safe }}
<h1>Library <span class="mute" style="font-weight:400; font-size:.95rem">— {{ total }} sources across {{ by_cat|length }} {{ 'category' if by_cat|length == 1 else 'categories' }}</span></h1>

<div class="controls">
  <input id="filter" type="search" placeholder="Filter by title, tldr, author, tag, category…" autofocus>
  <span class="mute" id="match-count"></span>
</div>

<div id="nomatch" class="nomatch hidden">No sources match.</div>

{% for cat in cats %}
{% if cat in by_cat %}
<section data-cat="{{ cat }}">
  <h2>{{ cat }} <span class="mute" style="font-weight:400; text-transform:none; letter-spacing:0; font-size:.85rem">({{ by_cat[cat]|length }})</span></h2>
  <table>
    <thead><tr><th style="width:24%">Title</th><th>Tl;dr</th><th style="width:8%">Year</th><th style="width:5rem"></th></tr></thead>
    <tbody>
      {% for s in by_cat[cat] %}
      <tr class="row" data-search="{{ (s.title ~ ' ' ~ s.tldr ~ ' ' ~ (s.authors|join(' ')) ~ ' ' ~ (s.tags|join(' ')) ~ ' ' ~ s.category)|lower }}">
        <td>
          <div class="title"><a href="#" onclick="openSummary('{{ s.rel_path|e }}'); return false;">{{ s.title }}</a></div>
          <div class="meta">{{ short_authors(s.authors) }}{% if s.url %} · <a href="{{ s.url }}" target="_blank" rel="noopener" style="color:var(--mute)">source ↗</a>{% endif %}</div>
          <div class="tags">{% for t in s.tags %}<span class="tag" onclick="setFilter('{{ t|e }}')">{{ t }}</span>{% endfor %}</div>
        </td>
        <td class="tldr">{{ s.tldr or '—' }}</td>
        <td class="mute">{{ s.year or '—' }}</td>
        <td class="actions">
          <button onclick="openSummary('{{ s.rel_path|e }}')">Open</button>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
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

async function openSummary(path) {
  try {
    const fd = new FormData();
    fd.append('path', path);
    await fetch('/api/open', { method: 'POST', body: fd });
  } catch (e) { /* no-op */ }
}
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
        nav=NAV_LIB,
    )


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
    subprocess.Popen(
        ["/bin/bash", str(WORKER)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return redirect("/?flash=Worker+triggered")


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


@app.route("/prompt", methods=["POST"])
def save_prompt():
    content = request.form.get("content", "")
    PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_FILE.write_text(content)
    return redirect("/?flash=Prompt+saved")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False)
