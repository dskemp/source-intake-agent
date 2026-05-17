#!/usr/bin/env python3
"""One-shot migration: copy institutional `publication:` values into `authors:`.

Some early summaries left `authors: []` and stored the issuing organization
only in `publication:` (ABA / NYCBA / CalBar / Anthropic provider docs). The
GAO summary, by contrast, puts the org in both fields. This script normalizes
the data so every institutional source has its org in `authors:`.

Defaults to a dry-run. Pass --apply to write changes.

Gate: only acts when YAML-parsed `authors` is empty (or missing) AND
`publication` is a non-empty string that passes the same institutional-author
heuristic used by the renderers. The `authors:` line is rewritten in-place
as a block list (matching the GAO file's style); `publication:` is left as-is.
Idempotent: a second --apply run finds nothing to do.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import yaml

LIBRARY = Path(os.environ.get("LIBRARY_PATH") or (Path.home() / "source-library"))


# Heuristics kept in sync with scripts/regen-index.py and scripts/dashboard.py.
_ORG_KEYWORDS = re.compile(
    r"\b("
    r"Office|Agency|Bureau|Committee|Commission|Council|Board|Authority|"
    r"Department|Ministry|Administration|"
    r"Association|Foundation|Society|Federation|Alliance|Coalition|Union|Trust|"
    r"Institute|Institution|Center|Centre|Forum|Initiative|Programme|"
    r"University|College|Law School|"
    r"Corporation|Company|Lab|Laboratory|"
    r"Court|Tribunal|"
    r"State Bar|"
    r"Inc\.?|LLC|Ltd\.?|PLC|GmbH"
    r")\b"
)
_ORG_PHRASES = re.compile(r"\b(School of|Bar Association)\b")
_KNOWN_ORGS = {
    "GAO", "OECD", "IMF", "WHO", "UN", "EU",
    "NIST", "NASA", "NIH", "FDA", "EPA", "FBI", "DOJ", "CDC", "FCC", "SEC",
    "ABA", "NYCBA", "ACLU", "USPTO", "OPM", "BLS",
    "Anthropic", "OpenAI", "Google DeepMind", "DeepMind", "Microsoft Research",
}
_VENUE_HINTS = re.compile(
    r"\b("
    r"arXiv|preprint|SSRN|"
    r"Journal|Review|Transactions|Letters|Proceedings|Conference|Workshop|"
    r"Nature|Cell|Science|JAMA|Lancet|"
    r"ICML|ICLR|NeurIPS|EMNLP|ACL|TACL|PMLR|COLM"
    r")\b"
)


def is_institutional_author(name: str) -> bool:
    n = name.strip()
    if not n:
        return False
    if n in _KNOWN_ORGS:
        return True
    if _VENUE_HINTS.search(n):
        return False
    if _ORG_KEYWORDS.search(n) or _ORG_PHRASES.search(n):
        return True
    if "," not in n and len(n.split()) >= 4:
        return True
    return False


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Matches the `authors:` declaration line whether it's `authors:`, `authors: `,
# `authors: []`, or `authors: null` — i.e. forms that parse to an empty value.
_EMPTY_AUTHORS_LINE = re.compile(
    r"^authors:[ \t]*(\[\s*\]|null|~)?[ \t]*$", re.IGNORECASE
)


def yaml_quote(s: str) -> str:
    """Quote a value for inline YAML using double quotes."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def find_candidates(library: Path):
    """Yield (summary_path, publication_value) for files that need migration."""
    for summary in library.glob("*/*/*.summary.md"):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        try:
            text = summary.read_text()
        except OSError:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        authors = fm.get("authors")
        if authors:  # non-empty list or non-empty string -> skip
            continue
        publication = fm.get("publication")
        if not isinstance(publication, str) or not publication.strip():
            continue
        if not is_institutional_author(publication):
            continue
        yield summary, publication.strip()


def rewrite_authors_line(text: str, org: str) -> tuple[str, str | None]:
    """Replace the empty `authors:` line in the frontmatter with a block list.

    Returns (new_text, error_or_none). If we can't find a safe line to rewrite
    (e.g. authors is followed by `  - ` continuation lines), returns the
    original text with an explanation.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text, "no frontmatter"
    fm_start, fm_end = m.start(1), m.end(1)
    fm_body = text[fm_start:fm_end]
    lines = fm_body.split("\n")
    target_idx = None
    for i, line in enumerate(lines):
        if _EMPTY_AUTHORS_LINE.match(line):
            target_idx = i
            break
    if target_idx is None:
        return text, "no rewritable `authors:` line found"
    # Refuse to clobber a block-style list that yaml happened to parse as empty.
    next_line = lines[target_idx + 1] if target_idx + 1 < len(lines) else ""
    if next_line.startswith(("  -", "\t-")):
        return text, "authors: is followed by block continuation; refusing"
    replacement = f"authors:\n  - {yaml_quote(org)}"
    new_lines = lines[:target_idx] + [replacement] + lines[target_idx + 1:]
    new_fm = "\n".join(new_lines)
    return text[:fm_start] + new_fm + text[fm_end:], None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually write changes. Without this flag, runs as a dry-run.",
    )
    args = ap.parse_args()

    if not LIBRARY.exists():
        print(f"LIBRARY_PATH does not exist: {LIBRARY}", file=sys.stderr)
        return 2

    candidates = list(find_candidates(LIBRARY))
    if not candidates:
        print("No migration candidates found.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {len(candidates)} candidate(s):\n")
    written = 0
    skipped = []
    for summary, org in candidates:
        rel = summary.relative_to(LIBRARY).as_posix()
        print(f"  {rel}")
        print(f"    publication: {org!r}")
        print(f"    authors:     [] -> [{org!r}]")
        if not args.apply:
            continue
        original = summary.read_text()
        new_text, err = rewrite_authors_line(original, org)
        if err is not None:
            print(f"    SKIPPED: {err}")
            skipped.append((rel, err))
            continue
        if new_text == original:
            print("    SKIPPED: text unchanged")
            skipped.append((rel, "text unchanged"))
            continue
        summary.write_text(new_text)
        written += 1
        print("    written")

    print()
    if args.apply:
        print(f"Wrote {written} file(s); skipped {len(skipped)}.")
        if skipped:
            print("Skipped:")
            for rel, err in skipped:
                print(f"  {rel}: {err}")
    else:
        print("Dry-run only. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
