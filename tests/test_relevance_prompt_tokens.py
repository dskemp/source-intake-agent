#!/usr/bin/env python3
"""Contract test for config/relevance-prompt.txt (stage-2 refbook triage).

Three parties depend on exact strings in this template: install.sh
substitutes the install-time tokens, worker.sh's run_triage substitutes the
runtime tokens, and a future refbook-side consumer skill parses the report
frontmatter the prompt dictates. A silently dropped token means the deployed
prompt ships with a literal "<SUMMARY_PATH>" (or paths from another machine),
and a renamed frontmatter key breaks every downstream consumer. These checks
pin that contract.

Pure-text; no network or file writes. Run: python3 tests/test_relevance_prompt_tokens.py
"""
import sys
from pathlib import Path

PROMPT = Path(__file__).resolve().parent.parent / "config" / "relevance-prompt.txt"

failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


text = PROMPT.read_text()

# Install-time tokens (substituted by install.sh's sed pass).
for token in ("__LIBRARY__", "__REFBOOK__"):
    check(f"install token {token} present", token in text)

# Runtime tokens (substituted by worker.sh run_triage).
for token in ("<SUMMARY_PATH>", "<REPORT_PATH>", "<INTAKE_OUTCOME>", "<TODAY>"):
    check(f"runtime token {token} present", token in text)

# Report frontmatter contract (parsed by the refbook-side consumer).
for key in (
    "report_type: source-triage",
    "source_slug:",
    "source_path:",
    "source_title:",
    "short_cite_guess:",
    "triaged:",
    "intake_outcome:",
    "already_cited:",
    "cited_in:",
    "verdict:",
    "findings:",
    "status: proposed",
):
    check(f"frontmatter key '{key}' present", key in text)

# Verdict and classification enums must be spelled out for the model.
for term in (
    "no-effect",
    "updates-recommended",
    "additions-recommended",
    "mixed",
    "changes",
    "adds",
):
    check(f"enum value '{term}' present", term in text)

# The report filename convention lives in worker.sh (category--slug), but the
# prompt must direct all writes to the wrapper-chosen <REPORT_PATH> and forbid
# writes elsewhere.
check("prompt forbids editing docs/", "docs/" in text and "REFERENCES.md" in text)
check("prompt mentions exit non-zero on unreadable input", "exit non-zero" in text)

if failures:
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1)
print("\nall checks passed")
