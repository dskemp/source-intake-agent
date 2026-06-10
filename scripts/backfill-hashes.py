#!/usr/bin/env python3
"""One-shot backfill: add `source_hash:` to every existing summary that lacks it.

Walks the library, finds each `*.summary.md`, locates its sibling source file
(`<slug>.pdf` or `<slug>.snapshot.md`), hashes it, and inserts a
`source_hash: "<sha256>"` line into the summary's YAML frontmatter.

Idempotent: summaries that already have a source_hash are skipped. Run once
after upgrading to the dedup-aware worker.

    LIBRARY_PATH=~/source-library \
      ~/.config/claude-source-intake/venv/bin/python scripts/backfill-hashes.py
"""
import hashlib
import os
import re
import sys
from pathlib import Path

import yaml

_library_path = os.environ.get("LIBRARY_PATH")
if not _library_path:
    raise SystemExit("ERROR: LIBRARY_PATH must be set (see the installed launchd plist).")
LIBRARY = Path(_library_path)


def parse_frontmatter(text: str):
    if not text.startswith("---\n"):
        return None, -1
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, -1
    try:
        return yaml.safe_load(text[4:end]) or {}, end
    except yaml.YAMLError:
        return None, -1


def find_source_file(summary: Path) -> Path | None:
    """Return sibling `<slug>.pdf` or `<slug>.snapshot.md` that the summary describes."""
    # summary stem looks like "<slug>.summary"; we want "<slug>"
    slug = summary.stem
    if slug.endswith(".summary"):
        slug = slug[: -len(".summary")]
    for ext in (".pdf", ".snapshot.md"):
        candidate = summary.parent / f"{slug}{ext}"
        if candidate.exists():
            return candidate
    return None


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def inject_source_hash(summary: Path, hash_val: str) -> bool:
    """Insert `source_hash: "..."` into frontmatter. Returns True if changed."""
    text = summary.read_text()
    fm, end = parse_frontmatter(text)
    if fm is None:
        return False
    # Check the frontmatter only — a body that merely mentions the literal
    # string "source_hash:" must not block injection.
    if "source_hash:" in text[: end + 1]:
        return False
    new_line = f'source_hash: "{hash_val}"\n'
    m = re.search(r"^tldr:[^\n]*\n", text[: end + 1], re.MULTILINE)
    insert_at = m.end() if m else end + 1
    summary.write_text(text[:insert_at] + new_line + text[insert_at:])
    return True


def main():
    if not LIBRARY.exists():
        print(f"library not found: {LIBRARY}", file=sys.stderr)
        return 1
    added = skipped = no_source = 0
    for summary in sorted(LIBRARY.glob("*/*/*.summary.md")):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        text = summary.read_text()
        fm, end = parse_frontmatter(text)
        if fm is not None and "source_hash:" in text[: end + 1]:
            skipped += 1
            continue
        src = find_source_file(summary)
        if src is None:
            print(f"  no source file for {summary.relative_to(LIBRARY)}", file=sys.stderr)
            no_source += 1
            continue
        h = hash_file(src)
        if inject_source_hash(summary, h):
            print(f"  + {summary.relative_to(LIBRARY)}  (hashed {src.name})")
            added += 1
    print(f"backfill: {added} added, {skipped} already had source_hash, {no_source} missing source files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
