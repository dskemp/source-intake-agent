#!/usr/bin/env python3
"""Tests for scripts/commit-triage.sh.

New triage reports and edits to existing ones are committed (separately);
README.md, other staged files and other modified files are left alone; nothing
happens off main. Builds a throwaway repo in a temp dir.
Run: python3 tests/test_commit_triage.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "commit-triage.sh"
failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def run(repo):
    env = {**os.environ, "REFBOOK_PATH": str(repo)}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True).stdout


def setup(d):
    repo = Path(d)
    (repo / "triage").mkdir()
    (repo / "docs").mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.name", "Owner")
    sh(repo, "git", "config", "user.email", "owner@example.com")
    (repo / "triage" / "README.md").write_text("readme\n")
    (repo / "triage" / "old.md").write_text("---\nstatus: proposed\n---\n")
    (repo / "docs" / "a.md").write_text("a\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "init")
    return repo


def test_commits_reports_only():
    with tempfile.TemporaryDirectory() as d:
        repo = setup(d)
        (repo / "triage" / "new.md").write_text("---\nstatus: proposed\n---\n")
        (repo / "triage" / "old.md").write_text("---\nstatus: approved\n---\n")
        (repo / "triage" / "README.md").write_text("readme edited\n")
        (repo / "docs" / "b.md").write_text("staged\n")
        sh(repo, "git", "add", "docs/b.md")
        (repo / "docs" / "a.md").write_text("a edited\n")
        run(repo)
        log = sh(repo, "git", "log", "--format=%an|%s").splitlines()
        check("new report committed by the pipeline", "source-intake-agent|Add triage report(s): new" in log)
        check("decision edit committed under the repo identity", "Owner|Record triage decision(s): old" in log)
        status = sh(repo, "git", "status", "--short")
        check("README edit left alone", " M triage/README.md" in status)
        check("unrelated staged file left staged", "A  docs/b.md" in status)
        check("unrelated modified file left modified", " M docs/a.md" in status)


def test_skips_off_main():
    with tempfile.TemporaryDirectory() as d:
        repo = setup(d)
        sh(repo, "git", "checkout", "-q", "-b", "feature")
        (repo / "triage" / "new.md").write_text("x\n")
        out = run(repo)
        check("off main: says so and commits nothing", "not main" in out
              and sh(repo, "git", "rev-list", "--count", "HEAD").strip() == "1")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(fns)} test groups for commit-triage.sh")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
