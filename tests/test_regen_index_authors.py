#!/usr/bin/env python3
"""Tests for institutional-author detection in scripts/regen-index.py.

The INDEX author column renders an org name in full, but shortens a person
name to a surname. `is_institutional_author()` decides which path a name takes.
This is a SEPARATE marker list from validate.py's ORG_MARKERS (and a third copy
lives in dashboard.py) — keep them in sync.

Regression: the GDPR source ("European Parliament" / "Council of the European
Union") rendered as "Parliament, Council of the European Union" because
"Parliament" was absent from _ORG_KEYWORDS, so the two-word name fell through to
surname shortening. These tests pin the governmental-body keywords.

regen-index.py reads LIBRARY_PATH at import, so we set it before loading.
Pure-logic; no library access. Run: python3 tests/test_regen_index_authors.py
"""
import importlib.util
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
os.environ.setdefault("LIBRARY_PATH", "/tmp")  # satisfies the import-time guard


def _load(filename, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


r = _load("regen-index.py", "regen_index")

failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


def test_gdpr_authors_render_in_full():
    # Both must be institutional, so _display_author returns the full name.
    for name in ["European Parliament", "Council of the European Union"]:
        check(f"'{name}' is institutional", r.is_institutional_author(name))
        check(f"'{name}' renders in full", r._display_author(name) == name)


def test_other_governmental_bodies_are_institutional():
    for name in ["United States Senate", "General Assembly",
                 "Directorate-General for Justice", "European Data Protection Board"]:
        check(f"'{name}' is institutional", r.is_institutional_author(name))


def test_person_names_still_shortened():
    # A two-word person name is not institutional -> shortened to surname.
    check("'Ashish Vaswani' not institutional", not r.is_institutional_author("Ashish Vaswani"))
    check("'Ashish Vaswani' -> 'Vaswani'", r._display_author("Ashish Vaswani") == "Vaswani")
    check("'Vaswani, Ashish' -> 'Vaswani'", r._display_author("Vaswani, Ashish") == "Vaswani")


if __name__ == "__main__":
    fns = [val for k, val in sorted(globals().items())
           if k.startswith("test_") and callable(val)]
    print(f"running {len(fns)} test groups for regen-index.py author display")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
