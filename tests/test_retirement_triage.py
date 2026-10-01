#!/usr/bin/env python3
"""Tests for scripts/retirement-triage.py.

Retired sources (status superseded/retracted/withdrawn, or a legacy
superseded_by with no status) get exactly one report each, listing the
book's citing passages from REFERENCES.md and sections' sources_used. Active
sources get none, existing reports are never rewritten, and an uncited
retired source gets a no-effect report as an audit trail.

Writes only to a temp dir. Run: python3 tests/test_retirement_triage.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "retirement-triage.py"
failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


def summary(lib, key, extra):
    cat, slug = key.split("/")
    d = lib / cat / slug
    d.mkdir(parents=True)
    (d / f"{slug}.summary.md").write_text(f'---\ntitle: "{slug} title"\n{extra}---\n\nbody\n')


def setup(root):
    lib, book = root / "library", root / "book"
    summary(lib, "cat/old-2025", 'superseded_by: "new-2026"\n')             # legacy form
    summary(lib, "cat/new-2026", 'superseded_by: ""\nstatus: active\n')
    summary(lib, "cat/pulled-2024", 'superseded_by: ""\nstatus: retracted\n')
    summary(lib, "cat/uncited-2023", 'superseded_by: ""\nstatus: withdrawn\n')
    (book / "docs").mkdir(parents=True)
    (book / "docs" / "31-adoption.md").write_text(
        '---\ntitle: "S31"\nsources_used:\n  - short_cite: "Old 2025"\n'
        '    library_path: "library/cat/old-2025/"\n---\n\nText (Old 2025).\n')
    (book / "REFERENCES.md").write_text(
        "## Sources\n\n### Old 2025\n\n- **Library path:** `library/cat/old-2025/`\n"
        "- **Cited in:** Sections 31.1, 32.5\n\n"
        "### New 2026\n\n- **Library path:** `library/cat/new-2026/`\n- **Cited in:** Sections 29.1\n\n"
        "### Pulled 2024\n\n- **Library path:** `library/cat/pulled-2024/`\n- **Cited in:** Section 12.3\n")
    return lib, book


def run(lib, book):
    env = {**os.environ, "LIBRARY_PATH": str(lib), "REFBOOK_PATH": str(book)}
    return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)


def test_reports():
    with tempfile.TemporaryDirectory() as d:
        lib, book = setup(Path(d))
        run(lib, book)
        reports = {p.name: p.read_text() for p in (book / "triage").glob("*.md")}
        check("three retired sources -> three reports", len(reports) == 3)
        check("active source gets no report", "cat--new-2026.retirement.md" not in reports)
        old = reports.get("cat--old-2025.retirement.md", "")
        check("legacy superseded_by counts as superseded", "source_status: superseded" in old)
        check("cited_in from REFERENCES", 'cited_in: ["31.1", "32.5"]' in old)
        check("sources_used section listed", "docs/31-adoption.md" in old)
        check("successor short cite resolved", 'successor_short_cite: "New 2026"' in old)
        check("report is a proposal", "status: proposed" in old and 'resolution: ""' in old)
        pulled = reports.get("cat--pulled-2024.retirement.md", "")
        check("retraction reported with its citation", "source_status: retracted" in pulled
              and 'cited_in: ["12.3"]' in pulled)
        uncited = reports.get("cat--uncited-2023.retirement.md", "")
        check("uncited retired source -> no-effect audit trail",
              "verdict: no-effect" in uncited and "## Why no effect" in uncited)


def test_idempotent_and_never_rewrites():
    with tempfile.TemporaryDirectory() as d:
        lib, book = setup(Path(d))
        run(lib, book)
        report = book / "triage" / "cat--old-2025.retirement.md"
        report.write_text(report.read_text().replace("status: proposed", "status: dismissed"))
        out = run(lib, book).stdout
        check("second run writes nothing", "no new retired sources" in out)
        check("user's decision survives a rerun", "status: dismissed" in report.read_text())


def test_disabled_without_refbook():
    with tempfile.TemporaryDirectory() as d:
        lib, _ = setup(Path(d))
        env = {k: v for k, v in os.environ.items() if k != "REFBOOK_PATH"}
        env["LIBRARY_PATH"] = str(lib)
        res = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
        check("no REFBOOK_PATH -> exit 0, nothing written", res.returncode == 0 and not res.stdout)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(fns)} test groups for retirement triage")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
