#!/usr/bin/env python3
"""Tests for multi-source manifest candidate notes (scripts/candidate-manifest.py).

A manifest note lets one inbox drop request several documents at once via a
`sources:` list. The worker fetches each entry and archives the note; the note
is never summarized. These tests pin the parser contract worker.sh relies on:

  - manifest_targets(text) returns None for a non-manifest (so the worker falls
    back to the legacy single-URL path), [] for a manifest with no fetchable
    URL (so the worker routes it to _failed/), or the ordered (slug, url) list.
  - bare-string and mapping entries both work; non-http entries are dropped.
  - slugs derive sensibly (arXiv id, URL basename) when an entry omits one.

Pure-logic; no network or PDF access. Run: python3 tests/test_candidate_manifest.py
"""
import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(filename, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("candidate-manifest.py", "candidate_manifest")


def _fm(body):
    return "---\n" + body + "\n---\n\n# notes\n"


failures = []


def check(name, cond):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name}")
        failures.append(name)


# --- Not a manifest -> None (worker falls back to the single-URL path) --------

def test_singular_note_is_not_a_manifest():
    text = _fm("status: candidate-for-intake\npdf_url: https://arxiv.org/pdf/2501.00321")
    check("singular pdf_url -> None", cm.manifest_targets(text) is None)


def test_missing_status_is_not_a_manifest():
    text = _fm("sources:\n  - https://arxiv.org/pdf/2501.00321")
    check("sources without candidate status -> None", cm.manifest_targets(text) is None)


def test_no_frontmatter_is_not_a_manifest():
    check("no frontmatter -> None", cm.manifest_targets("# just a memo\nhttps://x.com") is None)


def test_empty_sources_list_is_not_a_manifest():
    text = _fm("status: candidate-for-intake\nsources: []")
    check("empty sources list -> None", cm.manifest_targets(text) is None)


# --- Manifest parsing ---------------------------------------------------------

def test_mapping_entries_with_explicit_slug():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - slug: ocrbench-v2\n"
        "    pdf_url: https://arxiv.org/pdf/2501.00321\n"
        "  - slug: claude-vision-docs\n"
        "    url: https://docs.claude.com/en/docs/build-with-claude/vision\n"
    )
    got = cm.manifest_targets(text)
    check(
        "mapping entries keep explicit slugs + order",
        got == [
            ("ocrbench-v2", "https://arxiv.org/pdf/2501.00321"),
            ("claude-vision-docs", "https://docs.claude.com/en/docs/build-with-claude/vision"),
        ],
    )


def test_bare_string_entries_derive_slugs():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - https://arxiv.org/pdf/2406.18521\n"
        "  - https://ai.google.dev/gemini-api/docs/video-understanding\n"
    )
    got = cm.manifest_targets(text)
    check(
        "bare URLs derive arxiv-id and basename slugs",
        got == [
            ("arxiv-2406.18521", "https://arxiv.org/pdf/2406.18521"),
            ("video-understanding", "https://ai.google.dev/gemini-api/docs/video-understanding"),
        ],
    )


def test_non_http_entries_are_dropped():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - slug: good\n"
        "    pdf_url: https://arxiv.org/pdf/2404.12390\n"
        "  - slug: bad\n"
        "    pdf_url: /local/path/not-a-url.pdf\n"
        "  - ftp://example.com/x.pdf\n"
    )
    got = cm.manifest_targets(text)
    check("non-http entries dropped", got == [("good", "https://arxiv.org/pdf/2404.12390")])


def test_manifest_with_no_fetchable_url_returns_empty():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - slug: bad\n"
        "    pdf_url: notes-only\n"
    )
    check("manifest with no fetchable url -> []", cm.manifest_targets(text) == [])


def test_pdf_url_preferred_over_url():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - slug: s\n"
        "    pdf_url: https://example.com/a.pdf\n"
        "    url: https://example.com/landing\n"
    )
    got = cm.manifest_targets(text)
    check("pdf_url preferred over url", got == [("s", "https://example.com/a.pdf")])


def test_slug_is_sanitized():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        '  - slug: "OCRBench v2!"\n'
        "    pdf_url: https://arxiv.org/pdf/2501.00321\n"
    )
    got = cm.manifest_targets(text)
    check("explicit slug sanitized to kebab", got == [("ocrbench-v2", "https://arxiv.org/pdf/2501.00321")])


# --- derive_slug units --------------------------------------------------------

def test_derive_slug_arxiv_abs_and_pdf():
    check("derive arxiv /abs/", cm.derive_slug("https://arxiv.org/abs/2007.00398", 1) == "arxiv-2007.00398")
    check("derive arxiv /pdf/", cm.derive_slug("https://arxiv.org/pdf/2007.00398", 1) == "arxiv-2007.00398")


def test_derive_slug_basename_and_fallback():
    check("derive basename strips ext", cm.derive_slug("https://x.com/papers/My-Report.pdf", 1) == "my-report")
    check("derive fallback when empty path", cm.derive_slug("https://x.com/", 3) == "candidate-3")


# --- CLI contract (what worker.sh parses) -------------------------------------

def test_cli_prints_tab_lines():
    text = _fm(
        "status: candidate-for-intake\n"
        "sources:\n"
        "  - slug: a\n"
        "    pdf_url: https://example.com/a.pdf\n"
        "  - slug: b\n"
        "    url: https://example.com/b\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        p = f.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cm.main(["candidate-manifest.py", p])
        check("cli rc 0", rc == 0)
        check(
            "cli emits tab-separated slug\\turl lines",
            buf.getvalue() == "a\thttps://example.com/a.pdf\nb\thttps://example.com/b\n",
        )
    finally:
        os.unlink(p)


def test_cli_prints_NONE_for_unfetchable_manifest():
    text = _fm("status: candidate-for-intake\nsources:\n  - slug: x\n    pdf_url: nope")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        p = f.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cm.main(["candidate-manifest.py", p])
        check("cli prints NONE for unfetchable manifest", buf.getvalue() == "NONE\n")
    finally:
        os.unlink(p)


def test_cli_prints_nothing_for_non_manifest():
    text = _fm("status: candidate-for-intake\npdf_url: https://arxiv.org/pdf/2501.00321")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        p = f.name
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cm.main(["candidate-manifest.py", p])
        check("cli prints nothing for single-URL note", buf.getvalue() == "")
    finally:
        os.unlink(p)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(fns)} test groups for candidate-manifest.py")
    for fn in fns:
        print(fn.__name__)
        fn()
    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        sys.exit(1)
    print("\nall checks passed")
