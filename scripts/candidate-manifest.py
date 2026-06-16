#!/usr/bin/env python3
"""Parse a multi-source "manifest" candidate note into fetch targets.

The draft-section skill stages intake REQUESTS as candidate notes: markdown
files whose frontmatter carries `status: candidate-for-intake`. The original
contract is one note = one source (a scalar `pdf_url:`/`url:`), handled inline
by worker.sh. This helper adds the multi-source form: a `sources:` LIST, so a
single note can request several documents in one drop.

A `sources:` entry is either:
  - a bare URL string, or
  - a mapping with `pdf_url:` (preferred) or `url:`, plus an optional `slug:`.

Example frontmatter:

    ---
    status: candidate-for-intake
    sources:
      - slug: ocrbench-v2
        pdf_url: https://arxiv.org/pdf/2501.00321
      - slug: claude-vision-docs
        url: https://docs.claude.com/en/docs/build-with-claude/vision
      - https://arxiv.org/pdf/2406.18521
    ---

The worker fetches each target into the inbox (naming the file after the slug
so dedup/backfill match strongly) and archives the note to _notes/. The note
itself is never summarized: filing it as a snapshot would fake-satisfy the
originals audit and block backfill of the real documents.

CLI contract (consumed by worker.sh):
  - prints nothing, exit 0   not a manifest (no `sources:` list, or not a
                             candidate-for-intake note) -> worker falls back
                             to the single-URL candidate-note path.
  - prints "NONE"            a `sources:` list is present but yields no
                             fetchable http(s) URL -> worker routes to _failed/.
  - prints "<slug>\t<url>"   one tab-separated line per fetchable entry.
    per line

Pure parsing; no network or fetching. Importable for tests: `manifest_targets`.
"""
import re
import sys
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - the venv always has PyYAML
    yaml = None


def parse_frontmatter(text):
    """Return the YAML frontmatter as a dict, or None if absent/invalid."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    if yaml is None:
        return None
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _kebab(s):
    """Lowercase kebab-case; dots kept so arXiv ids survive (2501.00321)."""
    return re.sub(r"[^a-z0-9.]+", "-", s.lower()).strip("-.")


def derive_slug(url, idx):
    """Best-effort slug from a URL when an entry supplies none."""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]+)", url)
    if m:
        return "arxiv-" + m.group(1)
    seg = urlparse(url).path.rstrip("/").split("/")[-1]
    seg = re.sub(r"\.(pdf|html?|md|txt)$", "", seg, flags=re.I)
    seg = _kebab(seg)
    return seg or "candidate-%d" % idx


def manifest_targets(text):
    """Resolve a manifest note's `sources:` list into (slug, url) targets.

    Returns:
      None                 not a manifest -> caller tries the single-URL path
      []                   a manifest, but no entry has a fetchable http(s) URL
      [(slug, url), ...]   one tuple per fetchable entry, in document order
    """
    fm = parse_frontmatter(text)
    if not fm:
        return None
    if str(fm.get("status") or "").strip() != "candidate-for-intake":
        return None
    sources = fm.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    targets = []
    for idx, entry in enumerate(sources, 1):
        slug = ""
        url = ""
        if isinstance(entry, str):
            url = entry.strip()
        elif isinstance(entry, dict):
            url = str(entry.get("pdf_url") or entry.get("url") or "").strip()
            slug = str(entry.get("slug") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        slug = _kebab(slug) if slug else derive_slug(url, idx)
        if not slug:
            slug = "candidate-%d" % idx
        targets.append((slug, url))
    return targets


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: candidate-manifest.py <note-path>\n")
        return 2
    try:
        with open(argv[1], errors="replace") as f:
            text = f.read()
    except OSError:
        return 0
    targets = manifest_targets(text)
    if targets is None:
        return 0
    if not targets:
        sys.stdout.write("NONE\n")
        return 0
    sys.stdout.write("".join("%s\t%s\n" % (slug, url) for slug, url in targets))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
