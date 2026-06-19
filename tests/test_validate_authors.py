#!/usr/bin/env python3
"""Tests for the author-format gate in scripts/validate.py.

`looks_like_person()` is a heuristic that flags an author entry as a person
name (which must then be in "Last, First" form) UNLESS it carries a comma or an
org marker. Institutional authors must classify as orgs, or a correct summary
gets quarantined to _failed/_rejected/.

Regression: the GDPR intake (Regulation (EU) 2016/679, authored by "European
Parliament" / "Council of the European Union") was rejected because
"Parliament" was missing from ORG_MARKERS, so "European Parliament" looked like
a two-word person name. These tests pin the governmental-body markers so that
class of source validates.

Pure-logic; no network or file access. Run: python3 tests/test_validate_authors.py
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(filename, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v = _load("validate.py", "validate")

failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


# --- Institutional authors must NOT look like person names --------------------

def test_gdpr_authors_are_orgs():
    # The exact entries that caused the GDPR intake to be rejected.
    check("'European Parliament' is an org",
          not v.looks_like_person("European Parliament"))
    check("'Council of the European Union' is an org",
          not v.looks_like_person("Council of the European Union"))


def test_other_governmental_bodies_are_orgs():
    orgs = [
        "European Data Protection Board",
        "European Banking Authority",
        "European Court of Justice",
        "United States Senate",
        "General Assembly",
        "Directorate General",
        "European Union",
    ]
    for name in orgs:
        check(f"'{name}' is an org", not v.looks_like_person(name))


# --- Real person names must still be flagged when not "Last, First" -----------

def test_person_names_still_flagged():
    # Two-to-four Title-Case words, no comma, no org marker -> still a person.
    people = ["Ashish Vaswani", "Daniel Kahneman", "Yann LeCun"]
    for name in people:
        check(f"'{name}' still flagged as a person", v.looks_like_person(name))


def test_last_first_form_passes():
    # A comma means it is already in "Last, First" form -> not flagged.
    check("'Vaswani, Ashish' is accepted",
          not v.looks_like_person("Vaswani, Ashish"))


if __name__ == "__main__":
    fns = [val for k, val in sorted(globals().items())
           if k.startswith("test_") and callable(val)]
    print(f"running {len(fns)} test groups for validate.py author gate")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
