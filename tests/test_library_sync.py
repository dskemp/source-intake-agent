#!/usr/bin/env python3
"""Tests for scripts/library-sync.sh: the status file, failure notifications,
and the mirror's delete-with-history behaviour.

Builds a throwaway library (a git repo with a bare remote) and a local mirror
destination in a temp dir. HOME points into the temp dir so the real
~/.config/claude-source-intake/env is never read, and osascript is replaced by
a shim that records its arguments instead of posting a notification.
Run: python3 tests/test_library_sync.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "library-sync.sh"
failures = []
# macOS's openrsync skips deletions when --backup is set, so the deletion test
# needs rsync 3.x on PATH (the NAS receiver is 3.x in production).
OPENRSYNC = "openrsync" in subprocess.run(["rsync", "--version"], capture_output=True,
                                          text=True).stdout


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


class Env:
    def __init__(self, d):
        self.root = Path(d)
        self.lib = self.root / "library"
        self.remote = self.root / "remote.git"
        self.dest = self.root / "backups" / "library"
        self.history = self.root / "backups" / "library-history"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.notes = self.root / "notifications.txt"
        self.status = self.home / ".config/claude-source-intake/library-sync-status.json"
        for p in (self.lib, self.dest, self.home, self.bin):
            p.mkdir(parents=True)
        shim = self.bin / "osascript"
        shim.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" >> "{self.notes}"\n')
        shim.chmod(0o755)
        sh(self.root, "git", "init", "-q", "--bare", str(self.remote))
        sh(self.lib, "git", "init", "-q", "-b", "main")
        sh(self.lib, "git", "config", "user.name", "Owner")
        sh(self.lib, "git", "config", "user.email", "owner@example.com")
        sh(self.lib, "git", "remote", "add", "nas", str(self.remote))
        (self.lib / ".gitignore").write_text("*.pdf\n")
        for i in range(5):
            src = self.lib / "cat" / f"s{i}"
            src.mkdir(parents=True)
            (src / f"s{i}.summary.md").write_text(f"summary {i}\n")
            (src / f"s{i}.pdf").write_text(f"pdf {i}\n")
        sh(self.lib, "git", "add", "-A")
        sh(self.lib, "git", "commit", "-qm", "init")

    def run(self, **extra):
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "LIBRARY_PATH": str(self.lib),
            "LIBRARY_AUTOCOMMIT": "1",
            "LIBRARY_GIT_REMOTE": "nas",
            "LIBRARY_BACKUP_DEST": str(self.dest),
            **extra,
        }
        r = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        return r, json.loads(self.status.read_text())

    def history_files(self):
        if not self.history.exists():
            return []
        return sorted(str(p.relative_to(self.history)) for p in self.history.rglob("*") if p.is_file())


def test_success_mirrors_pdfs_and_records_ok():
    print("success")
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        r, s = e.run()
        check("exit 0", r.returncode == 0)
        check("outcome ok", s["outcome"] == "ok" and s["consecutive_failures"] == 0)
        check("last_success set", bool(s["last_success"]) and s["last_success"] == s["last_run"])
        check("gitignored PDF mirrored", (e.dest / "cat/s0/s0.pdf").exists())
        check("pushed", "init" in sh(e.lib, "git", "log", "--oneline", "nas/main"))
        check("no notification", not e.notes.exists())
        check("no history folder when nothing is lost", e.history_files() == [])
        check("status names no history folder", s["history_dir"] == "")


def test_deletion_propagates_and_is_kept():
    print("deletion")
    if OPENRSYNC:
        print("  skipped: only openrsync is installed (brew install rsync to run this)")
        return
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        e.run()
        (e.lib / "cat/s1/s1.pdf").unlink()
        (e.lib / "cat/s2/s2.summary.md").write_text("summary 2 edited\n")
        r, s = e.run()
        check("exit 0", r.returncode == 0)
        check("deleted PDF gone from mirror", not (e.dest / "cat/s1/s1.pdf").exists())
        check("edited file updated in mirror",
              (e.dest / "cat/s2/s2.summary.md").read_text() == "summary 2 edited\n")
        hist = e.history_files()
        check("deleted PDF kept in history", any(h.endswith("cat/s1/s1.pdf") for h in hist))
        old = [p for p in e.history.rglob("s2.summary.md")]
        check("overwritten version kept in history", bool(old) and old[0].read_text() == "summary 2\n")
        check("status counts the deletion", s["mirror_deleted"] == 1)
        check("status names the history folder", s["history_dir"].startswith("library-history/"))
        check("log lists the removed file", "removed from mirror: cat/s1/s1.pdf" in r.stdout)


def test_max_delete_stops_and_alerts():
    print("max-delete guard")
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        e.run()
        for i in range(4):
            (e.lib / f"cat/s{i}/s{i}.pdf").unlink()
        (e.lib / "cat/s4/s4.summary.md").write_text("summary 4 edited\n")
        r, s = e.run(LIBRARY_BACKUP_MAX_DELETE="2")
        check("changed files still copied",
              (e.dest / "cat/s4/s4.summary.md").read_text() == "summary 4 edited\n")
        check("exit non-zero", r.returncode != 0)
        check("outcome failed on backup", s["outcome"] == "failed" and s["failed_steps"] == "backup")
        check("detail explains the limit", "4 files would be deleted" in s["detail"]
              and "deleted none" in s["detail"])
        left = sum((e.dest / f"cat/s{i}/s{i}.pdf").exists() for i in range(4))
        check("nothing deleted from the mirror", left == 4 and s["mirror_deleted"] == 0)
        check("notification posted",
              e.notes.exists() and "Library backup failed" in e.notes.read_text())
        check("notification names the step", e.notes.exists() and "Steps: backup" in e.notes.read_text())


def test_push_failure_counts_and_keeps_last_success():
    print("push failure")
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        _, ok = e.run()
        sh(e.lib, "git", "remote", "set-url", "nas", str(e.root / "missing.git"))
        (e.lib / "new.md").write_text('has "quotes" and \\ backslash\n')
        r1, s1 = e.run()
        r2, s2 = e.run()
        check("exit non-zero", r1.returncode != 0)
        check("push named as failed step", s1["failed_steps"] == "push")
        check("backup still ran", (e.dest / "new.md").exists())
        check("consecutive failures counted", s1["consecutive_failures"] == 1
              and s2["consecutive_failures"] == 2)
        check("last success preserved", s2["last_success"] == ok["last_success"])
        notes = e.notes.read_text() if e.notes.exists() else ""
        check("second notification says how many in a row", "failed 2 runs in a row" in notes)
        sh(e.lib, "git", "remote", "set-url", "nas", str(e.remote))
        _, s3 = e.run()
        check("recovery resets the count", s3["outcome"] == "ok" and s3["consecutive_failures"] == 0)


def test_json_survives_awkward_error_text():
    print("awkward error text")
    with tempfile.TemporaryDirectory() as d:
        e = Env(d)
        locked = e.root / 'read "only" dir'
        locked.mkdir()
        locked.chmod(0o500)
        try:
            r, s = e.run(LIBRARY_BACKUP_DEST=str(locked / "y"))
        finally:
            locked.chmod(0o700)
        check("status parsed as JSON", isinstance(s, dict))
        check("backup failure recorded", s["failed_steps"] == "backup" and s["detail"].startswith("backup:"))


if __name__ == "__main__":
    test_success_mirrors_pdfs_and_records_ok()
    test_deletion_propagates_and_is_kept()
    test_max_delete_stops_and_alerts()
    test_push_failure_counts_and_keeps_last_success()
    test_json_survives_awkward_error_text()
    if failures:
        print(f"\n{len(failures)} failure(s)")
        sys.exit(1)
    print("\nall passed")
