#!/usr/bin/env python3
"""Regression tests for preprint→publication matching and promotion.

Guards against the false-positive promotion that archived the
"Abductive Commonsense Reasoning" (Bhagavatula et al., 2020) preprint when the
unrelated "Abductive Commonsense Reasoning: Exploiting Mutually Exclusive
Explanations" (Zhao et al., 2023) paper was dropped: the two share a title stem
but no authors. OpenAlex conflation let the later paper's published location be
recorded as the preprint's "published version," and a DOI match then triggered a
destructive swap.

Two layers are covered:
  1. check-preprints' arXiv-id fast path must require author corroboration, so a
     conflated OpenAlex record can't be accepted as the published version.
  2. detect-promotion must refuse a DOI-based promotion when the incoming PDF's
     title doesn't resemble the preprint it would overwrite.

Pure-logic; no network or PDF access. Run: python3 tests/test_preprint_matching.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("LIBRARY_PATH", tempfile.gettempdir())
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(filename, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

cp = _load("check-preprints.py", "check_preprints")
dp = _load("detect-promotion.py", "detect_promotion")

BHAGAVATULA_AUTHORS = [
    "Bhagavatula, Chandra", "Le Bras, Ronan", "Malaviya, Chaitanya",
    "Sakaguchi, Keisuke", "Holtzman, Ari", "Choi, Yejin",
]
ZHAO_AUTHORSHIPS = [
    {"author": {"display_name": "Wenting Zhao"}},
    {"author": {"display_name": "Justin T. Chiu"}},
    {"author": {"display_name": "Claire Cardie"}},
    {"author": {"display_name": "Alexander M. Rush"}},
]
ZHAO_DOI = "10.18653/v1/2023.acl-long.831"
ZHAO_TITLE = "Abductive Commonsense Reasoning: Exploiting Mutually Exclusive Explanations"


_failures = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _failures.append(name)


# --- check-preprints: conflated arXiv-id record must be rejected ----------------
# A single OpenAlex hit that (via conflation) carries the Bhagavatula preprint's
# arXiv id and even its title, but whose authors and published location belong to
# the Zhao paper. The OLD fast path accepted this on title sanity alone (>=0.6)
# and would have stored Zhao's ACL DOI as the preprint's published version.
CONFLATED_HIT = {
    "title": "Abductive Commonsense Reasoning",
    "doi": "https://doi.org/10.48550/arxiv.1908.05739",
    "authorships": ZHAO_AUTHORSHIPS,
    "locations": [
        {"landing_page_url": "https://arxiv.org/abs/1908.05739",
         "source": {"type": "repository", "display_name": "arXiv"}},
        {"landing_page_url": f"https://doi.org/{ZHAO_DOI}",
         "is_published": True, "version": "publishedVersion",
         "source": {"type": "conference", "display_name": "ACL 2023"}},
    ],
}

cp.openalex_get = lambda path, params=None: {"results": [CONFLATED_HIT]}
res = cp.check_by_title("Abductive Commonsense Reasoning", BHAGAVATULA_AUTHORS,
                        expected_arxiv_id="1908.05739")
check("conflated record is NOT accepted as published", res.get("status") != "published")
check("Zhao's ACL DOI is not surfaced as the preprint's published_doi",
      res.get("published_doi") != ZHAO_DOI and res.get("doi") != ZHAO_DOI)

# --- check-preprints: a genuine arXiv-id match is still detected -----------------
LEGIT_HIT = {
    "title": "Attention Is All You Need",
    "doi": "https://doi.org/10.48550/arxiv.1706.03762",
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {"display_name": "Noam Shazeer"}},
    ],
    "locations": [
        {"landing_page_url": "https://arxiv.org/abs/1706.03762",
         "source": {"type": "repository", "display_name": "arXiv"}},
        {"landing_page_url": "https://doi.org/10.5555/3295222.3295349",
         "is_published": True, "version": "publishedVersion",
         "source": {"type": "conference", "display_name": "NeurIPS"}},
    ],
}
cp.openalex_get = lambda path, params=None: {"results": [LEGIT_HIT]}
res = cp.check_by_title("Attention Is All You Need",
                        ["Vaswani, Ashish", "Shazeer, Noam"],
                        expected_arxiv_id="1706.03762")
check("genuine same-paper arXiv-id match is still published", res.get("status") == "published")

# --- check-preprints: missing authorships don't over-reject the fast path --------
NO_AUTHORS_HIT = dict(LEGIT_HIT, authorships=[])
cp.openalex_get = lambda path, params=None: {"results": [NO_AUTHORS_HIT]}
res = cp.check_by_title("Attention Is All You Need", ["Vaswani, Ashish"],
                        expected_arxiv_id="1706.03762")
check("arXiv-id match still accepted when OpenAlex lists no authors",
      res.get("status") == "published")

# --- detect-promotion: poisoned DOI promotion is refused ------------------------
REL = "ai-capabilities/bhagavatula-2020-abductive-commonsense-reasoning/bhagavatula-2020-abductive-commonsense-reasoning.summary.md"
POISONED_ENTRY = {
    "status": "published",
    "title": "Abductive Commonsense Reasoning",
    "published_title": ZHAO_TITLE,
    "published_doi": ZHAO_DOI,
    "published_url": f"https://doi.org/{ZHAO_DOI}",
}
candidates = [(REL, POISONED_ENTRY)]
check("DOI still matches the (poisoned) cache entry",
      dp.match_by_doi(ZHAO_DOI, candidates) == REL)
check("title guard rejects the mismatched promotion",
      dp.title_corroborates_preprint(ZHAO_TITLE, POISONED_ENTRY) is False)

# detect-promotion.main(): no PROMOTE line emitted for the poisoned match.
dp.published_entries = lambda cache: candidates
dp.load_cache = lambda: {}
dp.extract_doi_from_pdf = lambda p: ZHAO_DOI
dp.extract_title_from_pdf = lambda p: ZHAO_TITLE
with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
    tf.write(b"%PDF-1.4 stub")
    pdf_stub = tf.name
sys.argv = ["detect-promotion.py", pdf_stub]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    dp.main()
os.unlink(pdf_stub)
check("detect-promotion emits no PROMOTE for the mismatched paper",
      "PROMOTE:" not in buf.getvalue())

# --- detect-promotion: a genuine promotion still corroborates -------------------
LEGIT_ENTRY = {
    "status": "published",
    "title": "Attention Is All You Need",
    "published_title": "Attention Is All You Need",
    "published_doi": "10.5555/3295222.3295349",
}
check("title guard accepts a genuine same-paper promotion",
      dp.title_corroborates_preprint("Attention Is All You Need", LEGIT_ENTRY) is True)

if _failures:
    print(f"\n{len(_failures)} test(s) FAILED: {_failures}")
    sys.exit(1)
print("\nAll preprint-matching regression tests passed.")
