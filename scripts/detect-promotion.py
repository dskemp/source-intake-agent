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

When no single-paper match is found but the PDF looks like a proceedings
volume (an LNCS book, conference proceedings, or similar multi-paper
collection), the script sweeps the volume's outline/contents for the
published titles of tracked preprints and prints one

    VOLUME-CONTAINS:<library-relative-summary-path>

line per hit. worker.sh holds such a volume instead of intaking it as a
new source: promoting would swap a whole volume into a single paper's
slot, and plain intake would silently bury the pending promotion — the
exact failure that filed ECCV 2024 Part XXIII as a source while
fu-2024-blink's promotion sat unnoticed inside it.

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

_library_path = os.environ.get("LIBRARY_PATH")
if not _library_path:
    raise SystemExit("ERROR: LIBRARY_PATH must be set (see the installed launchd plist).")
LIBRARY = Path(_library_path)
CONFIG = Path(os.environ.get("PREPRINT_CONFIG") or (Path.home() / ".config/claude-source-intake"))
CACHE_FILE = CONFIG / "preprint-checks.json"

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>)\],]+", re.IGNORECASE)

TITLE_MATCH_THRESHOLD = 0.90
TITLE_MATCH_MARGIN = 0.15
PDF_TEXT_PAGES = 3

# Proceedings-volume detection. A volume is only ever *held*, never promoted,
# so these gates err toward under-detection: a false negative just means the
# old fall-through-to-intake behavior, a false positive would block a normal
# single-paper intake.
VOLUME_MIN_PAGES = 60
VOLUME_MARKER_PAGES = 8      # front-matter pages scanned for volume markers
VOLUME_TOC_PAGES = 40        # front-matter pages scanned for contents text
VOLUME_MARKERS = (
    "lecture notes in computer science",
    "lecture notes in artificial intelligence",
    "communications in computer and information science",
    "conference proceedings",
    "proceedings, part",
    "proceedings of the",
)


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    # Strip URL query/fragment that DOI_RE may have slurped on a landing URL.
    for sep in ("?", "#"):
        if sep in doi:
            doi = doi.split(sep, 1)[0]
    return doi.rstrip("/").rstrip(".,;)]>\"'").strip()


def doi_from_url(url: str) -> str:
    if not url:
        return ""
    m = DOI_RE.search(url)
    return normalize_doi(m.group(0)) if m else ""


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
    # metadata is resolved lazily and can raise on encrypted/damaged PDFs
    # (e.g. pypdf's DependencyError on AES files) — a bad document must mean
    # "no DOI", not a crash that silently disables promotion for this input.
    try:
        meta = reader.metadata or {}
    except Exception as e:
        print(f"pypdf failed to read metadata of {pdf_path}: {e}", file=sys.stderr)
        meta = {}
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
    try:
        meta = reader.metadata or {}
    except Exception:
        meta = {}
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
    if any(m in low for m in junk_markers):
        return True
    # Conference/journal front matter that PDFs frequently render as the first
    # line(s) of page 1, ahead of the actual paper title (ACL Anthology et al.).
    # Without this the title fallback grabs the proceedings banner instead of the
    # title and the similarity match never clears the threshold.
    if "©" in s or "(c)" in low:
        return True  # copyright / dateline line, e.g. "... ©2024 Association ..."
    if low.startswith(("proceedings of", "findings of the", "in proceedings")):
        return True
    if "annual meeting of the association" in low or "conference on empirical methods" in low:
        return True
    if re.match(r"^pages?\s+\d", low) or re.match(r"^vol(\.|ume)\s", low):
        return True
    return False


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


def candidate_dois(entry: dict) -> set[str]:
    """All DOIs a cache entry can be matched against.

    OpenAlex returns the preprint-server DOI as the work's canonical `doi` for
    SSRN/arXiv-registered works, so the journal DOI lives only in the chosen
    location's `landing_page_url` (stored as `published_url`). Newer cache
    entries also expose it as `published_doi`. Match against the full set.
    """
    dois: set[str] = set()
    for key in ("doi", "published_doi"):
        d = normalize_doi(entry.get(key) or "")
        if d:
            dois.add(d)
    url_doi = doi_from_url(entry.get("published_url") or "")
    if url_doi:
        dois.add(url_doi)
    return dois


def match_by_doi(input_doi: str, candidates: list[tuple[str, dict]]) -> str | None:
    input_doi = normalize_doi(input_doi)
    if not input_doi:
        return None
    hits = [rel for rel, entry in candidates if input_doi in candidate_dois(entry)]
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
        # Prefer the journal title; fall back to the preprint summary title
        # (often identical post-publication). Gate the fallback on
        # `published_url` so we never match against a preprint title for an
        # entry that wasn't actually confirmed published somewhere.
        compare_title = entry.get("published_title") or ""
        if not compare_title and entry.get("published_url"):
            compare_title = entry.get("title") or ""
        if not compare_title:
            continue
        score = SequenceMatcher(None, norm, normalize_title(compare_title)).ratio()
        scored.append((score, rel))
    if not scored:
        return None
    scored.sort(reverse=True)
    top_score, top_rel = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if top_score >= TITLE_MATCH_THRESHOLD and (top_score - runner_up) >= TITLE_MATCH_MARGIN:
        return top_rel
    return None


# A DOI-based promotion overwrites a tracked preprint with the incoming PDF and
# archives the original, so it is only safe when the PDF really is the published
# version of THAT preprint — the same paper — whose title should resemble the
# preprint's. A shared journal DOI alone can be wrong if the preprint cache was
# poisoned upstream (e.g. OpenAlex conflating two papers with the same title
# stem). This is the last gate before a destructive swap. When no title can be
# read from the PDF, don't block — the DOI match stands on its own.
PROMOTION_TITLE_FLOOR = 0.70


def title_corroborates_preprint(pdf_title: str, entry: dict) -> bool:
    norm = normalize_title(pdf_title)
    if len(norm) < 12:
        return True
    preprint_title = normalize_title(entry.get("title") or "")
    if not preprint_title:
        return True
    return SequenceMatcher(None, norm, preprint_title).ratio() >= PROMOTION_TITLE_FLOOR


def volume_chapter_index(pdf_path: Path) -> tuple[list[str], str] | None:
    """(outline titles, normalized front-matter text) if the PDF looks like a
    proceedings volume; None for anything single-paper-shaped.

    Volume test: enough pages to be a collection AND a proceedings marker in
    the front matter. Both signals must fire — a long monograph or report has
    the pages but not the markers, a paper's "Proceedings of the ..." dateline
    has the marker but not the pages.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        if len(reader.pages) < VOLUME_MIN_PAGES:
            return None
    except Exception:
        return None

    def page_text(i: int) -> str:
        try:
            return reader.pages[i].extract_text() or ""
        except Exception:
            return ""

    front = " ".join(page_text(i) for i in range(min(VOLUME_MARKER_PAGES, len(reader.pages))))
    front_low = " ".join(front.lower().split())
    if not any(m in front_low for m in VOLUME_MARKERS):
        return None

    titles: list[str] = []

    def walk(items) -> None:
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                t = str(getattr(item, "title", "") or "").strip()
                if t:
                    titles.append(t)

    try:
        walk(reader.outline)
    except Exception:
        pass
    # Contents pages as a fallback identity signal for volumes without
    # bookmarks. Normalized to one blob because TOC entries wrap across lines.
    toc = " ".join(page_text(i) for i in range(min(VOLUME_TOC_PAGES, len(reader.pages))))
    return titles, normalize_title(toc)


def match_volume_candidates(
    outline_titles: list[str],
    toc_norm: str,
    candidates: list[tuple[str, dict]],
) -> list[str]:
    """Rel-paths of tracked published preprints whose published title appears
    in the volume — as a bookmark (fuzzy) or in the contents text (exact
    normalized substring). Pure logic, separated from PDF access for tests.
    """
    outline_norms = [normalize_title(t) for t in outline_titles]
    hits: list[str] = []
    for rel, entry in candidates:
        # Same gating as match_by_title: never match on a bare preprint title
        # for an entry that wasn't confirmed published somewhere.
        compare = entry.get("published_title") or ""
        if not compare and entry.get("published_url"):
            compare = entry.get("title") or ""
        norm = normalize_title(compare)
        if len(norm) < 12:
            continue
        if any(
            SequenceMatcher(None, norm, o).ratio() >= TITLE_MATCH_THRESHOLD
            for o in outline_norms
        ) or (toc_norm and norm in toc_norm):
            hits.append(rel)
    return hits


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
    by_rel = dict(candidates)
    pdf_title = extract_title_from_pdf(pdf_path)
    doi = extract_doi_from_pdf(pdf_path)
    rel = match_by_doi(doi, candidates)
    if not rel:
        rel = match_by_title(pdf_title, candidates)
    # Final gate before a destructive swap, applied however `rel` was matched:
    # the published PDF must be the same paper as the preprint it would
    # overwrite. Both the DOI route and the title route key off cache fields
    # (`published_doi`, `published_title`) that an upstream metadata mix-up can
    # poison with a *different* paper, so corroborate against the preprint's own
    # title — the value taken straight from the user's summary.
    if rel and not title_corroborates_preprint(pdf_title, by_rel[rel]):
        print(
            f"refusing promotion to {rel}: PDF title {pdf_title!r} does not "
            f"resemble the tracked preprint title {by_rel[rel].get('title')!r}",
            file=sys.stderr,
        )
        rel = None
    if rel:
        print(f"PROMOTE:{rel}")
        return 0
    # No single-paper match: check whether this is a proceedings volume that
    # contains the published version of tracked preprints. Held, not promoted —
    # the worker parks the volume with instructions to fetch the chapter PDFs.
    vol = volume_chapter_index(pdf_path)
    if vol is not None:
        vol_hits = match_volume_candidates(vol[0], vol[1], candidates)
        if vol_hits:
            print(
                f"proceedings volume contains published versions of "
                f"{len(vol_hits)} tracked preprint(s): {vol_hits}",
                file=sys.stderr,
            )
            for vrel in vol_hits:
                print(f"VOLUME-CONTAINS:{vrel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
