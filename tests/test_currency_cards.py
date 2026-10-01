#!/usr/bin/env python3
"""Tests for the currency console's card builder (scripts/currency.py).

build_cards() must turn every gatherer outcome, including failures, into
cards without raising: the page is where the operator learns something is
broken, so it can't be the thing that breaks. Pure logic, synthetic data.
Run: python3 tests/test_currency_cards.py
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("currency", SCRIPTS / "currency.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


HEALTH = {
    "synthesis_drift": [
        {"file": "derived/practitioner-prompting-guide.md", "synced_to": "aaa", "behind": 1},
        {"file": "docs/19-prompting-fundamentals.md", "synced_to": "aaa", "behind": 2, "diff": "git diff"},
    ],
    "cites_inactive_source": [{"file": "docs/31-x.md", "source": "cat/old", "state": "superseded",
                               "superseded_by": "new"}],
    "triage_proposed": [{"file": "triage/a.md", "verdict": "adds", "triaged": "2026-08-01",
                         "age_days": 60, "committed": False}],
    "triage_stale": [{"file": "triage/a.md", "verdict": "adds", "triaged": "2026-08-01", "age_days": 60}],
    "triage_approved": [{"file": "triage/b.retirement.md", "kind": "retirement", "resolution": None}],
    "gate_c_pr_coverage": {"since": "2026-10-01", "sections": 1, "subsections": 2, "by_file": {}},
    "sections_overdue": [], "unaudited_sources": [], "currency_check_due": [],
    "currency_check_invalid": [], "library_git": {"uncommitted_paths": 0, "unpushed_commits": 0},
}
DIGEST = {"flagged": 3, "downloaded": 1, "landed": [], "flagged_this_week": 2, "oldest_open": [],
          "recalibration": {"last": "2026-08-24", "added_since": 0, "negative_since": 4, "ready": False}}
GATE = {"total": 3, "done": 0, "waiting": [{"id": "31", "file": "docs/31-x.md"},
                                           {"id": "19", "file": "docs/19-prompting-fundamentals.md"},
                                           {"id": "40", "file": "docs/40-y.md"}]}
BACKUPS = {"digest": {"outcome": "ok", "age_hours": 50, "stale": True, "last_run": "x", "detail": "d",
                      "file": "f", "bytes": 1},
           "library": {"uncommitted_paths": 0, "unpushed_commits": 0, "stale": False}}


def cards_for(**over):
    data = {"health": HEALTH, "digest": DIGEST, "gate_c": GATE, "backups": BACKUPS}
    data.update(over)
    return {k["key"]: k for k in c.build_cards(data)}


def test_normal_data():
    cards = cards_for()
    check("stale digest backup is urgent", cards["backup-digest"]["severity"] == "bad")
    check("urgent cards sort first", c.build_cards({"health": HEALTH, "digest": DIGEST, "gate_c": GATE,
                                                    "backups": BACKUPS})[0]["severity"] == "bad")
    check("guide drift card offers the derive command",
          any("derive-practitioner-guide" in cmd for cmd in cards["drift-guides"]["commands"]))
    check("uncommitted triage gets a commit command", bool(cards["triage"]["commands"]))
    check("triage entry marks age and commit state",
          "over 14 days" in cards["triage"]["entries"][0] and "not committed" in cards["triage"]["entries"][0])
    check("retired citation card warns", cards["retired-cites"]["severity"] == "warn")
    q = cards["gate-c"]["entries"]
    # docs/31 (retired cite) and docs/19 (synthesis drift) both carry open signals.
    q = [e for e in q if e.startswith("docs/")]
    check("Gate C queue: flagged sections first",
          set(q[:2]) == {"docs/31-x.md", "docs/19-prompting-fundamentals.md"})
    check("Gate C queue: unflagged section last", q[-1] == "docs/40-y.md")
    check("Gate C count reads done/total", cards["gate-c"]["count"] == "0/3")
    check("approved retirement without resolution is called out",
          "needs resolution" in cards["triage-approved"]["entries"][0])
    check("approved card offers apply-triage", any("apply triage" in x for x in cards["triage-approved"]["commands"]))
    check("Gate C card shows PR coverage", "2 subsection(s) in 1 section(s)" in cards["gate-c"]["entries"][0])


def test_failures_become_cards():
    cards = cards_for(health={"error": "no script"}, digest={"error": "offline"},
                      gate_c={"error": "no detect-state"},
                      backups={"digest": {"error": "never ran"}, "library": {}})
    check("health error shown", cards["health"]["error"] == "no script")
    check("digest error shown", cards["digest-flags"]["error"] == "offline")
    check("gate C error shown", cards["gate-c"]["error"] == "no detect-state")
    check("missing backup is urgent", cards["backup-digest"]["severity"] == "bad")


def test_summary_text():
    data = {"generated": "now", "cards": c.build_cards({"health": HEALTH, "digest": DIGEST,
                                                         "gate_c": GATE, "backups": BACKUPS}),
            "rhythm": [{"task": "Library audit", "due": True}]}
    text = c.summary_text(data)
    check("summary lists urgent card", "[BAD] Digest database backup" in text)
    check("summary lists overdue routine", "Overdue routines: Library audit" in text)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(fns)} test groups for the currency console")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
