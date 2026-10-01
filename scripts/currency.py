#!/usr/bin/env python3
"""Currency console: everything in the digest → library → refbook → guides
pipeline that is waiting on a human, gathered read-only into cards.

Used two ways:
  - imported by dashboard.py for the /currency page;
  - run by launchd weekly with --notify, which prints a plain-text summary and
    posts a macOS notification pointing at the page.

Every gatherer is read-only and fails soft: a missing repo, an unreachable
digest, or an unmerged script turns into a card saying so, never an
exception. Sources (all optional except LIBRARY_PATH):

  REFBOOK_PATH         refbook repo; scripts/pipeline-health.py (drift, retired
                       citations, triage, currency) and the orchestrate skill's
                       detect-state.py (Gate C)
  LIBRARY_PATH         the library (git state, discovered_via values)
  LIBRARY_AUDIT_PATH   library-audit repo (last audit date)
  DIGEST_REPO_PATH     digest repo (last judge recalibration)
  DIGEST_URL           digest dashboard; its public /api/library-flagged
  ~/.config/digest-backup/status.json   written by the digest's NAS backup job

Usage: currency.py [--json] [--notify]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()


def _path(var: str, default: str | None) -> Path | None:
    value = os.environ.get(var) or default
    return Path(value).expanduser() if value else None


REFBOOK = _path("REFBOOK_PATH", None)
LIBRARY = _path("LIBRARY_PATH", None)
AUDIT_DIR = _path("LIBRARY_AUDIT_PATH", str(HOME / "Cowork/active/library-audit"))
DIGEST_REPO = _path("DIGEST_REPO_PATH", str(HOME / "Developer/digest"))
DIGEST_URL = os.environ.get("DIGEST_URL", "https://digest.davidkemp.ai").rstrip("/")
DIGEST_BACKUP_STATUS = HOME / ".config/digest-backup/status.json"
LIBRARY_SYNC_LOG = Path("/tmp/claude-source-intake-library-sync.out.log")
SYNTHESIS = "sources/prompt-engineering-best-practices-2026.md"
# Where the operator opens the dashboard: the first DASHBOARD_EXTRA_ORIGINS
# entry (a reverse-proxy name such as https://library.example.localhost) when
# set, else localhost on the dashboard port.
_origins = [o.strip().rstrip("/") for o in os.environ.get("DASHBOARD_EXTRA_ORIGINS", "").split(",") if o.strip()]
CONSOLE_URL = (_origins[0] if _origins else
               f"http://localhost:{os.environ.get('DASHBOARD_PORT', '7341')}") + "/currency"

# integration-design.md thresholds
FLAGS_PER_WEEK_TARGET = 5
RECAL_MIN_ADDED, RECAL_MIN_NEGATIVE = 5, 20
BACKUP_STALE_HOURS = 36

CACHE_SECONDS = 600
_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _git_date(repo: Path | None, *paths: str) -> str | None:
    if not repo or not (repo / ".git").exists():
        return None
    out = _run(["git", "log", "-1", "--format=%cs", "--", *paths], cwd=repo).stdout.strip()
    return out or None


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days


# --- gatherers ------------------------------------------------------------------

def gather_health() -> dict:
    script = REFBOOK / "scripts/pipeline-health.py" if REFBOOK else None
    if not script or not script.exists():
        return {"error": "scripts/pipeline-health.py is not in the refbook checkout yet "
                         "(it arrives with dskemp/ai-refbook#28)."}
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        res = _run([sys.executable if sys.executable else "python3", str(script),
                    "--json", tmp.name, "--quiet"], cwd=REFBOOK, timeout=300)
        if res.returncode != 0:
            return {"error": f"pipeline-health.py failed: {res.stderr.strip()[-300:]}"}
        return json.loads(Path(tmp.name).read_text())


def gather_gate_c() -> dict:
    script = REFBOOK / ".claude/skills/orchestrate/scripts/detect-state.py" if REFBOOK else None
    if not script or not script.exists():
        return {"error": "detect-state.py not found in the refbook checkout."}
    res = _run(["python3", str(script)], cwd=REFBOOK)
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"error": "detect-state.py did not return JSON."}
    done = [r for r in rows if r["state"] in ("human-reviewed", "appendix-human-reviewed")]
    waiting = [r for r in rows if r["next_action"] == "human-review"]
    return {"total": len(rows), "done": len(done),
            "waiting": [{"id": r["id"], "file": r.get("final_file") or f"appendix {r['id']}"}
                        for r in waiting]}


def gather_digest_flags() -> dict:
    def fetch(qs: str) -> dict:
        req = urllib.request.Request(f"{DIGEST_URL}/api/library-flagged{qs}",
                                     headers={"User-Agent": "currency-console"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    try:
        open_rows = fetch("")["items"]
        all_rows = fetch("?include_closed=true")["items"]
    except Exception as e:  # network, DNS, JSON
        return {"error": f"digest unreachable: {e}"}

    flagged = [r for r in open_rows if r["library_status"] == "flagged"]
    downloaded = [r for r in open_rows if r["library_status"] == "downloaded"]

    # Downloaded candidates that the library already holds (matched on the
    # discovered_via stamp) are ready to be confirmed as "added" by hand.
    in_library = _library_discovered_via()
    landed = [r for r in downloaded if f"digest:{r['kind']}/{r['id']}" in in_library]

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    def recent(r):
        ts = r.get("ts")
        try:
            return ts and datetime.fromisoformat(ts) >= week_ago
        except ValueError:
            return False
    flagged_this_week = sum(1 for r in all_rows if recent(r))

    recal_date = _git_date(DIGEST_REPO, "library-profile.md")
    since = datetime.fromisoformat(recal_date).replace(tzinfo=timezone.utc) if recal_date else None
    def labeled_since(r, statuses):
        if r["library_status"] not in statuses or not r.get("library_reviewed_at"):
            return False
        return since is None or datetime.fromisoformat(r["library_reviewed_at"]) > since
    added = sum(1 for r in all_rows if labeled_since(r, ("added",)))
    # A bulk dismissal (one migration, one NOW()) is one decision about a
    # pattern, not N labels (digest CHANGELOG 2026-08-24): count distinct
    # review timestamps.
    negative = len({r["library_reviewed_at"] for r in all_rows if labeled_since(r, ("dismissed",))})
    return {
        "flagged": len(flagged), "downloaded": len(downloaded),
        "landed": [{"kind": r["kind"], "id": r["id"], "title": r["title"]} for r in landed],
        "flagged_this_week": flagged_this_week,
        "recalibration": {"last": recal_date, "added_since": added, "negative_since": negative,
                          "ready": added >= RECAL_MIN_ADDED and negative >= RECAL_MIN_NEGATIVE},
        "oldest_open": [{"kind": r["kind"], "id": r["id"], "title": r["title"], "ts": r.get("ts")}
                        for r in sorted(flagged, key=lambda r: r.get("ts") or "")[:5]],
    }


def _library_discovered_via() -> set[str]:
    out: set[str] = set()
    if not LIBRARY or not LIBRARY.exists():
        return out
    for path in LIBRARY.glob("*/*/*.summary.md"):
        if path.relative_to(LIBRARY).parts[0].startswith((".", "_")):
            continue
        head = path.read_text(errors="replace")[:4000]
        m = re.search(r'^discovered_via:\s*"?([^"\n]+)"?', head, re.M)
        if m:
            out.add(m.group(1).strip())
    return out


def gather_backups(health: dict) -> dict:
    digest = {"error": "no backup has run yet (~/.config/digest-backup/status.json missing)"}
    if DIGEST_BACKUP_STATUS.exists():
        try:
            s = json.loads(DIGEST_BACKUP_STATUS.read_text())
            age_h = None
            if s.get("last_success"):
                last = datetime.fromisoformat(s["last_success"].replace("Z", "+00:00"))
                age_h = round((datetime.now(timezone.utc) - last).total_seconds() / 3600, 1)
            digest = {**s, "age_hours": age_h,
                      "stale": age_h is None or age_h > BACKUP_STALE_HOURS or s.get("outcome") != "ok"}
        except (ValueError, OSError) as e:
            digest = {"error": f"unreadable status file: {e}"}

    lib = (health or {}).get("library_git") or {}
    last_sync_line = ""
    if LIBRARY_SYNC_LOG.exists():
        lines = [l for l in LIBRARY_SYNC_LOG.read_text(errors="replace").splitlines() if l.strip()]
        last_sync_line = lines[-1] if lines else ""
    return {"digest": digest, "library": {**lib, "last_daily_sync": last_sync_line,
            "stale": bool(lib.get("uncommitted_paths") or lib.get("unpushed_commits"))}}


def gather_rhythm(health: dict, digest: dict) -> list[dict]:
    synth_updated = None
    if REFBOOK and (REFBOOK / SYNTHESIS).exists():
        m = re.search(r"^last_updated:\s*(\S+)", (REFBOOK / SYNTHESIS).read_text()[:2000], re.M)
        synth_updated = m.group(1) if m else None
    drift = (health or {}).get("synthesis_drift") or []
    guides_behind = [d for d in drift if d["file"].startswith("derived/")]
    rows = [
        ("Clear digest library flags", "weekly", None,
         f"{digest.get('flagged', '?')} open" if "error" not in digest else "digest unreachable"),
        ("Review proposed triage reports", "weekly", None,
         f"{len((health or {}).get('triage_proposed') or [])} proposed, "
         f"{len((health or {}).get('triage_stale') or [])} over 14 days"),
        ("Library audit of new sources", "monthly", _git_date(AUDIT_DIR), None),
        ("Currency map and drift review", "monthly", _git_date(REFBOOK, "CURRENCY-MAP.md"), None),
        ("Vendor-docs pass on the synthesis", "quarterly", synth_updated, None),
        ("Guide syncs", "when the synthesis moves", None,
         f"{len(guides_behind)} guide(s) behind" if guides_behind else "in sync"),
        ("Digest judge recalibration", "when labels allow",
         ((digest.get("recalibration") or {}).get("last")),
         "ready" if (digest.get("recalibration") or {}).get("ready") else "not enough labels yet"),
    ]
    cadence_days = {"weekly": 7, "monthly": 31, "quarterly": 92}
    out = []
    for task, cadence, last, note in rows:
        age = _days_since(last)
        due = cadence in cadence_days and age is not None and age > cadence_days[cadence]
        out.append({"task": task, "cadence": cadence, "last": last, "age_days": age,
                    "note": note, "due": due})
    return out


# --- cards ------------------------------------------------------------------------

def build_cards(data: dict) -> list[dict]:
    """Turn raw signals into cards: count, severity, why, what to do, items.

    severity: bad (act now) > warn (needs a decision) > ok."""
    h, d, b, g = data["health"], data["digest"], data["backups"], data["gate_c"]
    cards = []

    def card(key, title, severity, count, why, todo, items=(), commands=(), links=(), error=None):
        cards.append({"key": key, "title": title, "severity": severity, "count": count,
                      "why": why, "todo": todo, "entries": list(items), "commands": list(commands),
                      "links": list(links), "error": error})

    # Backups first: silent loss is the one failure nothing else catches.
    dg, lb = b["digest"], b["library"]
    if "error" in dg:
        card("backup-digest", "Digest database backup", "bad", "—",
             "The digest database exists only on the droplet without this.",
             "Install the backup job from the digest repo's ops/ folder.", error=dg["error"])
    else:
        card("backup-digest", "Digest database backup", "bad" if dg["stale"] else "ok",
             f"{dg['age_hours']}h" if dg.get("age_hours") is not None else "never",
             "Nightly pg_dump to the NAS. Stale means the Mac slept through it or the job failed.",
             "Nothing to do if fresh. If stale, run it now and read the log.",
             items=[f"last run {dg.get('last_run')} — {dg.get('outcome')}: {dg.get('detail')}",
                    f"file {dg.get('file')} ({(dg.get('bytes') or 0) // 1_000_000} MB)"],
             commands=["tail -20 /tmp/digest-backup.out.log /tmp/digest-backup.err.log"])
    card("backup-library", "Library committed and on the NAS", "bad" if lb.get("stale") else "ok",
         (lb.get("uncommitted_paths") or 0) + (lb.get("unpushed_commits") or 0),
         "Intake commits and pushes after every run; this catches anything it missed.",
         "If non-zero, run the sync now.",
         items=[f"uncommitted paths: {lb.get('uncommitted_paths')}",
                f"unpushed commits: {lb.get('unpushed_commits')} (upstream {lb.get('upstream')})",
                f"last daily sync: {lb.get('last_daily_sync') or 'none logged yet'}"])

    # Digest → library
    if "error" in d:
        card("digest-flags", "Digest library candidates", "warn", "—",
             "The digest flags candidate sources for the library.", "", error=d["error"])
    else:
        sev = "warn" if d["flagged"] or d["landed"] else "ok"
        items = [f"{d['flagged']} flagged, {d['downloaded']} downloaded awaiting confirmation",
                 f"{d['flagged_this_week']} flagged in the last 7 days (target ≤ {FLAGS_PER_WEEK_TARGET})"]
        items += [f"in the library already — mark added: {r['kind']}/{r['id']} {r['title']}" for r in d["landed"]]
        items += [f"oldest open: {r['title']} ({(r['ts'] or '')[:10]})" for r in d["oldest_open"]]
        card("digest-flags", "Digest library candidates", sev, d["flagged"] + len(d["landed"]),
             "Each flag is a possible library source. Only you decide what enters the library.",
             "Download the keepers' stubs and drop them in the library inbox, dismiss the rest. "
             "Mark downloaded ones 'Added' once intake has filed them.",
             items=items, links=[(f"{DIGEST_URL}/?view=library", "Open the digest's Library view")])
        rc = d["recalibration"]
        card("recalibrate", "Digest judge recalibration", "warn" if rc["ready"] else "ok",
             f"{rc['added_since']}/{rc['negative_since']}",
             f"Recalibrating needs at least {RECAL_MIN_ADDED} 'added' and {RECAL_MIN_NEGATIVE} "
             "negative labels since the last one, or it learns from a lopsided sample.",
             "When ready, run the recalibration skill in the digest repo and approve its profile diff."
             if rc["ready"] else "Not yet. Keep labeling flags.",
             items=[f"last recalibration: {rc['last']}",
                    f"since then: {rc['added_since']} added, {rc['negative_since']} dismissed"],
             commands=["cd ~/Developer/digest && claude \"recalibrate the library judge\""] if rc["ready"] else [])

    # Library → refbook
    if "error" in h:
        card("health", "Pipeline health report", "warn", "—", "", "", error=h["error"])
    else:
        proposed = h.get("triage_proposed", h["triage_stale"])
        stale_files = {t["file"] for t in h["triage_stale"]}
        uncommitted = [t for t in proposed if not t.get("committed", True)]
        card("triage", "Triage reports awaiting a decision", "warn" if proposed else "ok", len(proposed),
             "New library sources the intake pipeline judged relevant to the book. A report is a "
             "proposal; nothing acts on it until you decide.",
             "Read each report and set its status: to approved, actioned or dismissed, then commit "
             "it (the triage contract keeps reports in git, including dismissed ones).",
             items=[f"{t['file']} — {t['verdict']}, triaged {t['triaged']}"
                    + (" · over 14 days" if t["file"] in stale_files else "")
                    + ("" if t.get("committed", True) else " · not committed") for t in proposed],
             commands=[f"cd {REFBOOK} && git add triage/ && git commit -m \"Commit pending triage reports\""]
                      if uncommitted else [])

        retired = h["cites_inactive_source"]
        card("retired-cites", "Book sections citing retired sources", "warn" if retired else "ok",
             len(retired),
             "A section leaning on a superseded or retracted source may be teaching something obsolete.",
             "For each, decide: is the old source cited on purpose (e.g. a trend comparison), or "
             "should the claim move to the successor? Then edit through a reviewed PR.",
             items=[f"{r['file']} cites {r['source']} ({r['state']}"
                    + (f" → {r['superseded_by']}" if r.get("superseded_by") else "") + ")" for r in retired])

        drift = h["synthesis_drift"]
        guides = [x for x in drift if x["file"].startswith("derived/")]
        others = [x for x in drift if not x["file"].startswith("derived/")]
        cmds = []
        if any("harvey" in x["file"] for x in guides):
            cmds.append("cd ~/Developer/ai-refbook && claude \"run prompts/derive-harvey-guide.md\"")
        if any("practitioner" in x["file"] for x in guides):
            cmds.append("cd ~/Developer/ai-refbook && claude \"run prompts/derive-practitioner-guide.md\"")
        card("drift-guides", "Guides behind the synthesis", "warn" if guides else "ok", len(guides),
             "The colleague-facing guides derive from the prompt-engineering synthesis. When it "
             "changes, the guides may be giving outdated advice.",
             "Run each guide's derivation prompt; review the diff as a PR.",
             items=[f"{x['file']} — {x['behind']} commit(s) behind {x['synced_to']}" for x in guides],
             commands=cmds)
        card("drift-sections", "Book sections behind the synthesis", "warn" if others else "ok", len(others),
             "These sections restate synthesis content. Behind means the synthesis changed since "
             "the section was last checked against it; it may or may not matter.",
             "Read the synthesis diff for each; edit if needed, then set the file's "
             "synthesis_synced_to to the synthesis head.",
             items=[f"{x['file']} — {x['behind']} behind ({x.get('diff', '')})" for x in others])

        un = h["unaudited_sources"] or []
        card("unaudited", "Library sources not yet audited", "warn" if un else "ok", len(un),
             "Summaries that haven't been checked against their PDFs may misstate claims the "
             "book then repeats.",
             "Run the library audit; review its worklist.", items=un,
             commands=["claude \"audit the library\""] if un else [])

        due, bad = h["currency_check_due"], h["currency_check_invalid"]
        card("currency", "Library currency checks", "warn" if due else "ok", len(due),
             "Fast-moving sources need re-confirming every 6 months, stable ones yearly.",
             "Check each due source is still current; set currency_check to today, or mark it "
             "superseded/retracted with status:.",
             items=[f"{x['source']} — last confirmed {x['currency_check']}" for x in due]
                   + ([f"{len(bad)} malformed currency_check values (legacy; one cleanup pass fixes them)"] if bad else []))

        od = h["sections_overdue"]
        card("overdue", "Sections past their refresh cadence", "warn" if od else "ok", len(od),
             "Each section has a currency tier (stable 12 mo, semi-stable 6, volatile 3).",
             "Refresh the overdue sections' time-sensitive passages.",
             items=[f"{x['file']} — {x['tier']}, last reviewed {x['last_reviewed']}" for x in od])

    # Gate C
    if "error" in g:
        card("gate-c", "Human review (Gate C)", "warn", "—", "", "", error=g["error"])
    else:
        flagged_files = set()
        if "error" not in h:
            flagged_files = {x["file"] for x in h["synthesis_drift"] + h["cites_inactive_source"]}
        def review_priority(r):
            # 0: has an open signal above; 1: prompting sections §§10–29; 2: the rest
            if r["file"] in flagged_files:
                return (0, r["file"])
            m = re.match(r"docs/(\d+)-", r["file"])
            return (1 if m and 10 <= int(m.group(1)) <= 29 else 2, r["file"])
        queue = sorted(g["waiting"], key=review_priority)
        card("gate-c", "Human review (Gate C)", "warn" if g["waiting"] else "ok",
             f"{g['done']}/{g['total']}",
             ("Every section passed the automated pipeline, but none has been read and signed off "
              "by a person yet. Colleagues are reading machine-reviewed text.") if g["done"] == 0 else
             (f"{len(g['waiting'])} section(s) passed the automated pipeline but haven't been read "
              "and signed off by a person."),
             "Read a section end to end; if it holds up, set it to Human-Reviewed in STATUS.md. "
             "Suggested order: sections with open signals above first (fix, then review), then "
             "the prompting sections (§§10–29) that colleagues use most.",
             items=[q["file"] for q in queue[:8]] + ([f"…and {len(queue) - 8} more"] if len(queue) > 8 else []))

    rank = {"bad": 0, "warn": 1, "ok": 2}
    cards.sort(key=lambda c: rank[c["severity"]])
    return cards


def gather(force: bool = False) -> dict:
    with _cache_lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            return {"error": f"{fn.__name__}: {e}"}
    health = safe(gather_health)
    digest = safe(gather_digest_flags)
    data = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "health": health,
        "digest": digest,
        "gate_c": safe(gather_gate_c),
        "backups": safe(gather_backups, health if "error" not in health else {}),
    }
    if "error" in data["backups"]:
        data["backups"] = {"digest": data["backups"], "library": {}}
    data["rhythm"] = gather_rhythm(health if "error" not in health else {}, digest)
    data["cards"] = build_cards(data)
    with _cache_lock:
        _cache.update(at=time.time(), data=data)
    return data


def summary_text(data: dict) -> str:
    need = [c for c in data["cards"] if c["severity"] != "ok"]
    lines = [f"Currency console — {data['generated']}", CONSOLE_URL, ""]
    if not need:
        lines.append("Nothing needs you this week.")
    for c in need:
        lines.append(f"[{c['severity'].upper()}] {c['title']}: {c['count']}")
    due = [r for r in data["rhythm"] if r["due"]]
    if due:
        lines += ["", "Overdue routines: " + ", ".join(r["task"] for r in due)]
    return "\n".join(lines)


def notify(data: dict) -> None:
    need = [c for c in data["cards"] if c["severity"] != "ok"]
    bad = [c for c in need if c["severity"] == "bad"]
    title = "Currency console"
    if not need:
        msg = "Nothing needs you this week."
    else:
        msg = f"{len(need)} item(s) need you" + (f", {len(bad)} urgent" if bad else "") + \
              f" — open {CONSOLE_URL}"
    script = f'display notification {json.dumps(msg)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], check=False)


if __name__ == "__main__":
    data = gather(force=True)
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(summary_text(data))
    if "--notify" in sys.argv:
        notify(data)
