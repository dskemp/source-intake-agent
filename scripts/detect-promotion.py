#!/usr/bin/env python3
"""Detect whether an incoming PDF is the published version of a tracked preprint.

The worker invokes this after its hash/url/fuzzy dedup passes find nothing.
It reads the preprint-checks.json cache (populated by check-preprints.py),
inspects the input PDF for a DOI (metadata first, then a regex sweep of the
first few pages), and asks: does any "published" cache entry have that DOI?

If exactly one match is found, the script prints

    PROMOTE:<library-relative-summary-path>

to stdout — the signal worker.sh uses to swap the preprint for the published
version. If no DOI can be extracted, falls back to a strict title-similarity
match against each candidate's `published_title`.

Conservative by design: refuses to fire on ambiguous DOI matches (logs to
stderr), and the title fallback requires a ≥0.90 score with a ≥0.15 margin
over the runner-up — mirroring worker.sh's fuzzy-dedup discipline.

Exit codes: 0 on clean exit (with or without a match). Non-zero only on
unrecoverable error (bad args, unreadable cache).
"""
from __future__ import annotations

import json
import os
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

LIBRARY = Path(os.environ.get("LIBRARY_PATH") or (Path.home() / "source-library"))
CONFIG = Path(os.environ.get("PREPRINT_CONFIG") or (Path.home() / ".config/claude-source-intake"))
CACHE_FILE = CONFIG / "preprint-checks.json"

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>)\],]+", re.IGNORECASE)

TITLE_MATCH_THRESHOLD = 0.90
TITLE_MATCH_MARGIN = 0.15
PDF_TEXT_PAGES = 3


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.rstrip(".,;)]>\"'").strip()


def extract_doi_from_pdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("pypdf not installed; cannot extract DOI", file=sys.stderr)
        return ""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        print(f"pypdf failed to open {pdf_path}: {e}", file=sys.stderr)
        return ""
    meta = reader.metadata or {}
    for key in ("/doi", "/DOI", "/Doi"):
        val = meta.get(key) if hasattr(meta, "get") else None
        if val:
            doi = normalize_doi(str(val))
            if doi:
                return doi
    for key, val in (dict(meta).items() if meta else []):
        if val and "10." in str(val):
            m = DOI_RE.search(str(val))
            if m:
                return normalize_doi(m.group(0))
    pages_to_scan = min(PDF_TEXT_PAGES, len(reader.pages))
    for i in range(pages_to_scan):
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception:
            continue
        m = DOI_RE.search(text)
        if m:
            return normalize_doi(m.group(0))
    return ""


def extract_title_from_pdf(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:
        return ""
    meta = reader.metadata or {}
    title = ""
    for key in ("/Title", "/title"):
        val = meta.get(key) if hasattr(meta, "get") else None
        if val:
            title = str(val).strip()
            if title:
                break
    if title and not _looks_like_junk_title(title):
        return title
    if reader.pages:
        try:
            text = reader.pages[0].extract_text() or ""
        except Exception:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if len(line) >= 12 and not _looks_like_junk_title(line):
                return line
    return ""


def _looks_like_junk_title(s: str) -> bool:
    if not s:
        return True
    low = s.lower()
    junk_markers = ("untitled", "microsoft word", "untitled document", ".dvi", ".tex", "preprint.pdf")
    return any(m in low for m in junk_markers)


def normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"failed to read {CACHE_FILE}: {e}", file=sys.stderr)
        return {}


def published_entries(cache: dict) -> list[tuple[str, dict]]:
    """(rel-path, entry) for cache entries whose summary file still exists."""
    out = []
    for rel, entry in cache.items():
        if not isinstance(entry, dict) or entry.get("status") != "published":
            continue
        if not (LIBRARY / rel).exists():
            continue
        out.append((rel, entry))
    return out


def match_by_doi(input_doi: str, candidates: list[tuple[str, dict]]) -> str | None:
    input_doi = normalize_doi(input_doi)
    if not input_doi:
        return None
    hits = [rel for rel, entry in candidates if normalize_doi(entry.get("doi") or "") == input_doi]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"ambiguous DOI match ({input_doi}): {hits}", file=sys.stderr)
    return None


def match_by_title(input_title: str, candidates: list[tuple[str, dict]]) -> str | None:
    if not input_title:
        return None
    norm = normalize_title(input_title)
    if len(norm) < 12:
        return None
    scored = []
    for rel, entry in candidates:
        published_title = entry.get("published_title") or ""
        if not published_title:
            continue
        score = SequenceMatcher(None, norm, normalize_title(published_title)).ratio()
        scored.append((score, rel))
    if not scored:
        return None
    scored.sort(reverse=True)
    top_score, top_rel = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= TITLE_MATCH_THRESHOLD and (top_score - runner_up) >= TITLE_MATCH_MARGIN:
        return top_rel
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: detect-promotion.py <pdf-path>", file=sys.stderr)
        return 2
    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        return 0
    cache = load_cache()
    candidates = published_entries(cache)
    if not candidates:
        return 0
    doi = extract_doi_from_pdf(pdf_path)
    rel = match_by_doi(doi, candidates)
    if not rel:
        title = extract_title_from_pdf(pdf_path)
        rel = match_by_title(title, candidates)
    if rel:
        print(f"PROMOTE:{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
