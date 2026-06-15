#!/usr/bin/env python3
"""Tests for the dashboard's CHANGELOG-on-delete logging.

`delete_source` removes a source folder and regenerates INDEX.md but used to
leave no trace in CHANGELOG.md — unlike intake, which the worker logs. These
tests cover `append_changelog_deletion`, which closes that gap while matching
the worker's date-header/prepend convention.

Run with the project venv (has flask/markdown/nh3/yaml):
  ~/.config/claude-source-intake/venv/bin/python tests/test_dashboard_changelog.py
"""
import importlib.util
import os
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
# dashboard.py requires both of these at import time.
os.environ["LIBRARY_PATH"] = str(tmp)
os.environ.setdefault("INBOX_PATH", str(tmp))
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

spec = importlib.util.spec_from_file_location("dashboard", SCRIPTS / "dashboard.py")
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

cl = tmp / "CHANGELOG.md"
_failures = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _failures.append(name)


# Case 1: no CHANGELOG yet -> header + dated (latest) heading + entry created.
ok, msg = dash.append_changelog_deletion(
    "ai-capabilities", "zhao-2023-lipor",
    "Abductive Commonsense Reasoning: Exploiting Mutually Exclusive Explanations",
)
c = cl.read_text()
check("returns ok", ok is True)
check("creates '# Changelog' header", c.startswith("# Changelog"))
check("creates a dated (latest) heading", c.count("## ") == 1 and "(latest)" in c)
check("entry names the title and folder",
      "- Removed *Abductive Commonsense Reasoning: Exploiting Mutually Exclusive Explanations* "
      "from `ai-capabilities/zhao-2023-lipor/`." in c)

# Case 2: a second same-day removal stacks under the same heading, newest first.
dash.append_changelog_deletion("ai-capabilities", "foo-2024-bar", "Foo Bar Study")
c = cl.read_text()
check("still one date heading (same day)", c.count("## ") == 1)
check("still exactly one (latest) marker", c.count("(latest)") == 1)
check("newest removal sits above the earlier one",
      c.index("Foo Bar Study") < c.index("Abductive Commonsense"))

# Case 3: an empty title falls back to a slug code span.
dash.append_changelog_deletion("legal-ethics", "baz-2025-qux", "")
c = cl.read_text()
check("empty title -> slug fallback",
      "- Removed `legal-ethics/baz-2025-qux` from `legal-ethics/baz-2025-qux/`." in c)

# Case 4: when the latest heading is an older day, a fresh today heading is
# created and the stale "(latest)" marker is demoted off the old one.
cl.write_text("# Changelog\n\n## 2020-01-01 (latest)\n\n- Filed something.\n")
dash.append_changelog_deletion("x", "y", "Z")
c = cl.read_text()
before_old = c.split("## 2020-01-01")[0]
check("new today's heading precedes the old one and is (latest)", "(latest)" in before_old)
check("old heading demoted (kept, no longer latest)",
      "## 2020-01-01 (latest)" not in c and "## 2020-01-01" in c)
check("exactly one (latest) after day rollover", c.count("(latest)") == 1)

if _failures:
    print(f"\n{len(_failures)} test(s) FAILED: {_failures}")
    raise SystemExit(1)
print("\nAll dashboard CHANGELOG-deletion tests passed.")
