#!/usr/bin/env python3
"""Tests for the source lifecycle and provenance fields.

- validate.py normalizes a missing `status:` (superseded_by set -> superseded,
  else active), rejects unknown statuses and a `superseded` status with no
  successor, and checks the `discovered_via:` format when present.
- regen-index.py marks non-active sources in INDEX.md instead of dropping them,
  since the reference book may still cite them.

Writes only to a temp dir; no network (no arxiv.org URLs in the fixtures).
Run: python3 tests/test_source_status_provenance.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(filename, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


os.environ.setdefault("LIBRARY_PATH", "/tmp")  # regen-index.py's import-time guard
v = _load("validate.py", "validate")
ri = _load("regen-index.py", "regen_index")

failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


def summary(library, slug, extra="", superseded_by='""', category="ai-policy"):
    folder = library / category / slug
    folder.mkdir(parents=True)
    path = folder / f"{slug}.summary.md"
    path.write_text(f"""---
title: "A Report"
authors:
  - "Example Institute"
source_type: report
publication: "Example"
date: "2025-01-01"
url: "https://example.org/report"
retrieved: "2026-01-01"
snapshot: false
category: {category}
tldr: "A short tldr."
tags:
  - policy
currency_check: "2026-01-01"
superseded_by: {superseded_by}
{extra}---

## Citation
""")
    return path


def test_status_normalization():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d)
        p = summary(lib, "plain-2025")
        errors, notes = v.process(p)
        check("missing status validates", errors == [])
        check("missing status -> active", "status: active" in p.read_text())

        p = summary(lib, "old-2025", superseded_by='"new-2026"')
        errors, _ = v.process(p)
        check("missing status with successor validates", errors == [])
        check("missing status with successor -> superseded", "status: superseded" in p.read_text())


def test_status_validation():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d)
        errors, _ = v.process(summary(lib, "bad-2025", extra="status: obsolete\n"))
        check("unknown status rejected", any("status 'obsolete'" in e for e in errors))

        errors, _ = v.process(summary(lib, "orphan-2025", extra="status: superseded\n"))
        check("superseded without successor rejected", any("superseded_by is empty" in e for e in errors))

        errors, _ = v.process(summary(lib, "gone-2025", extra="status: retracted\n"))
        check("retracted needs no successor", errors == [])


def test_discovered_via():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d)
        for i, good in enumerate(["digest:item/123", "digest:scout/7", "manual",
                                  "candidate-note:foo.candidate.md"]):
            errors, _ = v.process(summary(lib, f"good-{i}-2025", extra=f'discovered_via: "{good}"\n'))
            check(f"discovered_via {good!r} accepted", errors == [])
        errors, _ = v.process(summary(lib, "bad-dv-2025", extra='discovered_via: "twitter"\n'))
        check("discovered_via 'twitter' rejected", any("discovered_via" in e for e in errors))


def test_index_marks_retired_sources():
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d)
        summary(lib, "current-2026")
        summary(lib, "old-2025", superseded_by='"current-2026"')
        summary(lib, "pulled-2024", extra="status: retracted\n")
        index = ri.render_index(ri.collect_sources(lib), lib)
        row = lambda slug: next((l for l in index.splitlines() if f"/{slug}/" in l), "")
        check("active source unmarked", "*" not in row("current-2026").split("|")[1])
        check("superseded source marked with successor",
              "*superseded → current-2026*" in row("old-2025"))
        check("retracted source marked", "*retracted*" in row("pulled-2024"))


if __name__ == "__main__":
    fns = [val for k, val in sorted(globals().items())
           if k.startswith("test_") and callable(val)]
    print(f"running {len(fns)} test groups for source status and provenance")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
