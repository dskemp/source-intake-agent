#!/usr/bin/env python3
"""Check preprint sources in the library for publication in peer-reviewed venues.

Walks the library, finds summaries whose URL points to arXiv or SSRN (or whose
frontmatter advertises a preprint via `source_type:` / `publication:`), and asks
OpenAlex whether each one now has a journal/conference version. Results are
cached at $CONFIG/preprint-checks.json keyed by the summary's library-relative
path.

This script does NOT mutate summary frontmatter — findings are surfaced in the
dashboard for the user to review. False positives from title-search (SSRN)
mean auto-writing `superseded_by:` would risk corrupting curated metadata.

Invoked two ways:
  - Weekly by launchd (default args: refresh stale entries only)
  - On demand from the dashboard ("Check now" button)

CLI:
  check-preprints.py             # check stale entries (default cadence)
  check-preprints.py --force     # re-check everything regardless of freshness
  check-preprints.py --rel-path foo/bar.summary.md
                                 # check exactly one source

Env:
  LIBRARY_PATH       (required) — same convention as the rest of the agent
  PREPRINT_CONFIG    optional override for the cache directory
  OPENALEX_EMAIL     optional — opts into OpenAlex's polite pool (higher rate
                     limit, friendlier 503 behavior). Highly recommended.
  PREPRINT_REFRESH_DAYS  default 7. Skip entries checked more recently than this.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

import yaml

_library_path = os.environ.get("LIBRARY_PATH")
if not _library_path:
    raise SystemExit("ERROR: LIBRARY_PATH must be set (see the installed launchd plist).")
LIBRARY = Path(_library_path)
CONFIG = Path(os.environ.get("PREPRINT_CONFIG") or (Path.home() / ".config/claude-source-intake"))
CACHE_FILE = CONFIG / "preprint-checks.json"
REFRESH_DAYS = int(os.environ.get("PREPRINT_REFRESH_DAYS", "7"))
OPENALEX_EMAIL = os.environ.get("OPENALEX_EMAIL", "").strip()
USER_AGENT = "source-intake-agent (preprint-checker; +https://github.com/dskemp/source-intake-agent)"

# OpenAlex is rate-limited; sleep a beat between requests so a library with
# many preprints doesn't tank our reputation in the shared pool.
RATE_LIMIT_SLEEP = float(os.environ.get("PREPRINT_RATE_LIMIT_SLEEP", "0.4"))

# arXiv accepts both new ("2310.06825") and old ("math.AG/0506203") id formats.
# The pdf/abs path is identical for both; we strip a trailing version (`v2`)
# before turning the id into the DOI 10.48550/arXiv.<id>.
ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf|html)/([a-z\-\.]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)
SSRN_RE = re.compile(
    r"ssrn\.com/(?:abstract|sol3/papers\.cfm\?abstract_id|delivery\.cfm/SSRN_ID)[=/]?(\d+)",
    re.IGNORECASE,
)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        return yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return None


def classify(fm: dict) -> tuple[str, str] | None:
    """Return (venue, identifier) if the summary looks like a preprint, else None.

    venue is "arXiv" | "SSRN" | "preprint" (the catch-all for source_type-only
    matches with no identifying URL). identifier is the arXiv/SSRN id when we
    have one, else "".
    """
    url = (fm.get("url") or "").strip()
    if m := ARXIV_RE.search(url):
        return "arXiv", m.group(1)
    if m := SSRN_RE.search(url):
        return "SSRN", m.group(1)
    source_type = (fm.get("source_type") or "").strip().lower()
    publication = (fm.get("publication") or "").strip()
    if source_type == "preprint" or publication.lower().startswith(("arxiv", "ssrn", "preprint")):
        # No identifier; fall back to title search.
        if publication.lower().startswith("arxiv"):
            return "arXiv", ""
        if publication.lower().startswith("ssrn"):
            return "SSRN", ""
        return "preprint", ""
    return None


def author_surnames(authors) -> list[str]:
    if isinstance(authors, str):
        authors = [authors]
    out = []
    for a in authors or []:
        a = a.strip() if isinstance(a, str) else ""
        if not a:
            continue
        # YAML schema convention is "Last, First" — also handle "First Last".
        # `a` is non-empty and stripped, so a.split() can't be empty here.
        surname = a.split(",")[0].strip() if "," in a else a.split()[-1]
        if surname:
            out.append(surname.lower())
    return out


def normalize_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def openalex_get(path: str, params: dict | None = None) -> dict | None:
    """GET an OpenAlex endpoint. Returns parsed JSON or None on 404.

    Raises on other HTTP errors so the caller can record a transient failure.
    """
    p = dict(params or {})
    if OPENALEX_EMAIL and "mailto" not in p:
        p["mailto"] = OPENALEX_EMAIL
    url = f"https://api.openalex.org{path}"
    if p:
        url += "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>)\],]+", re.IGNORECASE)


def _clean_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.replace("https://doi.org/", "").replace("http://doi.org/", "")


def _doi_from_url(url: str | None) -> str:
    """Extract a bare DOI from a landing-page URL, stripping query/fragment."""
    if not url:
        return ""
    m = _DOI_RE.search(url)
    if not m:
        return ""
    doi = m.group(0)
    for sep in ("?", "#"):
        if sep in doi:
            doi = doi.split(sep, 1)[0]
    return doi.rstrip("/").rstrip(".,;)]>\"'").lower()


def _is_preprint_location(loc: dict) -> bool:
    """True if a location entry points to arXiv/SSRN/biorxiv/etc."""
    src = loc.get("source") or {}
    venue_type = (src.get("type") or "").lower()
    if venue_type == "repository":
        return True
    landing = (loc.get("landing_page_url") or "").lower()
    return (
        "arxiv.org" in landing
        or "10.48550/arxiv" in landing
        or "ssrn.com" in landing
        or "biorxiv.org" in landing
        or "medrxiv.org" in landing
    )


def find_published_location(work: dict) -> dict | None:
    """Pick the strongest peer-reviewed venue from a work's locations[].

    OpenAlex's location data is messy — especially for ML/CS conference papers,
    `source` is often null even when the paper appeared in a proceedings. So we
    score each non-preprint location and return the best one (or None).
    Scoring weights peer-review signals: `version: publishedVersion`, explicit
    journal/conference classification, and `is_published: true`.
    """
    locations = work.get("locations") or []
    candidates = []
    for loc in locations:
        if _is_preprint_location(loc):
            continue
        version = (loc.get("version") or "").lower()
        if version == "submittedversion":
            # A "submitted" non-preprint location is just a submission, not acceptance.
            continue
        src = loc.get("source") or {}
        venue_type = (src.get("type") or "").lower()
        display_name = (src.get("display_name") or "").strip()
        score = 0
        if version == "publishedversion":
            score += 3
        elif version == "acceptedversion":
            score += 2
        if venue_type in ("journal", "conference", "book series"):
            score += 3
        elif venue_type in ("ebook platform",):
            score += 1
        if loc.get("is_published"):
            score += 1
        candidates.append((score, {
            "publication": display_name or "(venue unknown to OpenAlex)",
            "venue_type": venue_type or "unknown",
            "published_url": loc.get("landing_page_url") or "",
            "version": version or "",
        }))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]
    published_title = (work.get("title") or work.get("display_name") or "").strip()
    # OpenAlex's work-level `doi` is often the preprint server's DOI (SSRN,
    # arXiv) for works first deposited there. The journal DOI shows up only in
    # the chosen location's landing URL, so surface it as `published_doi`.
    return {
        **best,
        "doi": _clean_doi(work.get("doi")),
        "published_doi": _doi_from_url(best.get("published_url")),
        "published_title": published_title,
    }


def _hit_arxiv_id(hit: dict) -> str | None:
    """Extract the arXiv id from a work's locations, if any."""
    for loc in hit.get("locations") or []:
        landing = loc.get("landing_page_url") or ""
        if m := ARXIV_RE.search(landing):
            return m.group(1).lower()
    return None


def check_by_title(
    title: str,
    authors: Iterable[str],
    expected_arxiv_id: str = "",
) -> dict:
    """Search OpenAlex by title; require an author surname match to accept.

    When `expected_arxiv_id` is set, a hit whose own locations include the same
    arXiv id is treated as high-confidence regardless of title similarity (the
    arXiv id is a definitive identifier). Otherwise we require ≥0.85 title
    similarity and ≥1 shared author surname — conservative, biased toward false
    negatives over false positives.
    """
    if not title:
        return {"status": "unknown", "note": "no title to search"}
    try:
        result = openalex_get("/works", {"search": title, "per-page": "10"})
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        return {"status": "error", "error": f"openalex search failed: {e}"}
    if not result or not result.get("results"):
        return {"status": "unknown", "note": "no OpenAlex match"}
    # author_surnames accepts a scalar string or a list. Don't list()-wrap it —
    # that explodes a scalar "First Last, ..." string into characters.
    surnames = set(author_surnames(authors))
    expected_arxiv_id = (expected_arxiv_id or "").lower()
    best = None
    best_score = 0.0
    matched_by_arxiv_id = False
    for hit in result["results"]:
        # Fast path: matching arXiv id is a strong identifier, BUT OpenAlex
        # occasionally conflates unrelated papers into one record (locations[]
        # ends up with arXiv ids it shouldn't). Require at least a sanity-check
        # title overlap to avoid being fooled by those polluted records.
        if expected_arxiv_id and _hit_arxiv_id(hit) == expected_arxiv_id:
            sanity = title_similarity(title, hit.get("title") or hit.get("display_name") or "")
            if sanity >= 0.6:
                best, best_score, matched_by_arxiv_id = hit, max(sanity, 0.95), True
                break
        score = title_similarity(title, hit.get("title") or hit.get("display_name") or "")
        if score < 0.85:
            continue
        hit_surnames = set()
        for a in hit.get("authorships") or []:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                hit_surnames.add(name.split()[-1].lower())
        if surnames and not (surnames & hit_surnames):
            continue
        if score > best_score:
            best, best_score = hit, score
    if not best:
        return {"status": "unknown", "note": "no high-confidence match"}
    pub = find_published_location(best)
    if not pub:
        return {
            "status": "preprint-only",
            "note": "OpenAlex match but no peer-reviewed location",
            "match_score": round(best_score, 3),
        }
    if matched_by_arxiv_id:
        confidence = "high"
    elif best_score >= 0.97:
        confidence = "high"
    elif best_score >= 0.90:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "status": "published",
        "confidence": confidence,
        "match_score": round(best_score, 3),
        **pub,
    }


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(CACHE_FILE)


def is_fresh(entry: dict, refresh_days: int) -> bool:
    ts = entry.get("checked_at", "")
    if not ts:
        return False
    try:
        when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400
    return age_days < refresh_days


def discover_preprints() -> list[dict]:
    """Walk the library and return one dict per preprint summary."""
    out = []
    if not LIBRARY.exists():
        return out
    for summary in LIBRARY.glob("*/*/*.summary.md"):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        fm = parse_frontmatter(summary)
        if fm is None:
            continue
        cls = classify(fm)
        if not cls:
            continue
        venue, ident = cls
        rel_path = summary.relative_to(LIBRARY).as_posix()
        out.append({
            "rel_path": rel_path,
            "category": category,
            "title": fm.get("title") or summary.stem,
            "authors": fm.get("authors") or [],
            "date": str(fm.get("date") or ""),
            "url": fm.get("url") or "",
            "preprint_venue": venue,
            "preprint_id": ident,
        })
    return out


def check_one(p: dict) -> dict:
    """Run OpenAlex title search, passing the arXiv id for cross-validation."""
    arxiv_id = ""
    if p["preprint_venue"] == "arXiv" and p["preprint_id"]:
        arxiv_id = re.sub(r"v\d+$", "", p["preprint_id"])
    return check_by_title(p["title"], p["authors"], expected_arxiv_id=arxiv_id)


def run(force: bool = False, only_rel: str | None = None) -> dict:
    """Walk the library, check each preprint, persist the cache. Returns summary stats."""
    preprints = discover_preprints()
    if only_rel:
        preprints = [p for p in preprints if p["rel_path"] == only_rel]
        if not preprints:
            return {
                "checked": 0, "skipped": 0, "errors": 0, "found_published": 0,
                "pruned": 0, "total_preprints": 0,
                "note": f"no preprint at {only_rel}",
            }
    cache = load_cache()
    checked = skipped = errors = found = 0
    cache_rel_paths = set()
    for p in preprints:
        rel = p["rel_path"]
        cache_rel_paths.add(rel)
        prior = cache.get(rel) or {}
        if not force and not only_rel and is_fresh(prior, REFRESH_DAYS):
            skipped += 1
            # Refresh the display fields in case they changed in the summary.
            prior.update({
                "title": p["title"],
                "preprint_venue": p["preprint_venue"],
                "preprint_id": p["preprint_id"],
                "preprint_url": p["url"],
            })
            cache[rel] = prior
            continue
        result = check_one(p)
        entry = {
            "title": p["title"],
            "preprint_venue": p["preprint_venue"],
            "preprint_id": p["preprint_id"],
            "preprint_url": p["url"],
            "checked_at": iso_now(),
            **result,
        }
        cache[rel] = entry
        checked += 1
        if entry["status"] == "error":
            errors += 1
        elif entry["status"] == "published":
            found += 1
        # Polite-pool throttle. Skip the sleep on the last item.
        if p is not preprints[-1]:
            time.sleep(RATE_LIMIT_SLEEP)
    # Prune cache entries for summaries that were deleted since the last run.
    # Skip when --rel-path narrowed the run to one preprint, since
    # cache_rel_paths then only contains that single entry and pruning would
    # wipe every other tracked preprint from the cache.
    if not only_rel:
        stale = [k for k in cache if k not in cache_rel_paths]
        for k in stale:
            del cache[k]
    else:
        stale = []
    save_cache(cache)
    return {
        "checked": checked,
        "skipped": skipped,
        "errors": errors,
        "found_published": found,
        "pruned": len(stale),
        "total_preprints": len(preprints),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="re-check all preprints regardless of freshness")
    parser.add_argument("--rel-path", help="check exactly one summary by its library-relative path")
    args = parser.parse_args()
    stats = run(force=args.force, only_rel=args.rel_path)
    summary = (
        f"preprint check complete: "
        f"{stats['checked']} checked, {stats['skipped']} fresh-skipped, "
        f"{stats['found_published']} newly/still published, "
        f"{stats['errors']} errors, {stats['pruned']} pruned "
        f"(total preprints: {stats['total_preprints']})"
    )
    print(summary)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
