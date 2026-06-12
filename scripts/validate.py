#!/usr/bin/env python3
"""Normalize-then-validate frontmatter of freshly produced .summary.md files.

Usage: claude-source-intake-validate.py --paths-file <file-with-one-path-per-line>
       claude-source-intake-validate.py <summary.md> [...]

Phase 1 (normalize, in place, targeted line edits — never a full YAML rewrite):
  - superseded_by: null/missing      -> ""
  - snapshot:                        -> true/false to match <slug>.snapshot.md existence
  - retrieved: null/empty/missing    -> today (UTC)
  - currency_check: null/empty/bad   -> retrieved date (semantics: last date confirmed current)
  - source_type: known synonym       -> template enum value
  - authors: inline scalar string    -> single-entry block list

Phase 2 (validate; any hard failure exits 1 and the worker quarantines):
  - frontmatter parses; required fields present and non-empty
  - source_type in the template enum
  - date formats (date: YYYY[-MM[-DD]]; retrieved/currency_check: YYYY-MM-DD)
  - category field equals the parent category folder; slug/folder/filename agree
  - authors: block list; no "X et al." placeholder entries; entries that look
    like person names must be "Last, First"
  - arXiv cross-check (best effort): when url is an arxiv.org/abs link, the
    first author's surname and the author count must match the arXiv API.
    Network failure skips the check with a warning; it never blocks intake.

Exit codes: 0 = all files pass (possibly after normalization), 1 = at least
one hard failure, 2 = usage/internal error.
"""
import re
import sys
import datetime
from pathlib import Path

ENUM = {"paper", "documentation", "article", "report", "book-chapter", "opinion", "book"}
# 'book' is accepted ahead of the template: kahneman-2011 already needs it and
# SUMMARY-TEMPLATE.md is expected to gain it (proposed 2026-06-11).

SOURCE_TYPE_MAP = {
    "empirical-study": "paper", "empirical_study": "paper", "empirical-paper": "paper",
    "empirical paper": "paper", "preprint": "paper", "research-article": "paper",
    "journal-article": "paper", "journal_article": "paper", "working-paper": "paper",
    "working_paper": "paper", "review": "paper", "conference-paper": "paper",
    "law-review-article": "paper", "position paper": "paper", "position-paper": "paper",
    "study": "paper",
    "blog-post": "article", "blog post": "article", "essay": "article", "post": "article",
    "news": "article", "magazine-article": "article",
    "policy-report": "report", "policy-brief": "report", "white-paper": "report",
    "whitepaper": "report", "technical-report": "report",
    "docs": "documentation", "doc": "documentation",
    "ethics-opinion": "opinion", "formal-opinion": "opinion", "court-order": "opinion",
}

ORG_MARKERS = re.compile(
    r"(Association|Council|Committee|Commission|Bureau|Office|Institute|Institution|"
    r"University|Center|Centre|Project|Group|Foundation|Laborator|Society|Agency|"
    r"Department|Ministry|Bar of|State Bar|Corporation|Inc\.|LLC|Ltd|GmbH|"
    r"OWASP|Anthropic|OpenAI|LexisNexis|NIST|GAO|\bABA\b|\(.*\))", re.I)

DATE_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_LOOSE = re.compile(r"^\d{4}(-\d{2})?(-\d{2})?$")
ET_AL = re.compile(r"\bet\s+al\.?\s*$", re.I)


def split_frontmatter(text):
    if not text.startswith("---\n"):
        return None, None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, None
    return text[4:end + 1], text[end + 5:]  # fm includes trailing \n


def get_scalar(fm, key):
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return None  # key absent
    return m.group(1).strip()


def set_scalar(fm, key, rendered_value):
    """Replace the key's whole line (or append the line if absent)."""
    line = f"{key}: {rendered_value}"
    if re.search(rf"^{key}:", fm, re.M):
        return re.sub(rf"^{key}:[ \t]*.*$", line, fm, count=1, flags=re.M)
    return fm + line + "\n"


def get_authors(fm):
    """Return (list_of_entries, form) where form is block|flow|scalar|missing."""
    m = re.search(r"^authors:[ \t]*(.*)$", fm, re.M)
    if not m:
        return [], "missing"
    inline = m.group(1).strip()
    if inline.startswith("["):
        items = [a or b for a, b in re.findall(r'"([^"]*)"|\'([^\']*)\'', inline)]
        if not items:
            items = [x.strip() for x in inline.strip("[]").split(",") if x.strip()]
        return items, "flow"
    if inline:
        return [inline.strip().strip('"').strip("'")], "scalar"
    bm = re.search(r"^authors:[ \t]*\n((?:[ \t]+-[^\n]*\n)+)", fm, re.M)
    if not bm:
        return [], "missing"
    items = [re.sub(r"^[ \t]+-[ \t]*", "", l).strip().strip('"').strip("'")
             for l in bm.group(1).splitlines() if l.strip()]
    return items, "block"


def looks_like_person(name):
    """Heuristic: a 2-4 word Title-Case string with no comma and no org marker."""
    if "," in name or ORG_MARKERS.search(name):
        return False
    words = name.split()
    if not 2 <= len(words) <= 4:
        return False
    return all(w[:1].isupper() or w.lower() in ("van", "von", "der", "de", "da", "del", "di", "la", "le")
               for w in words)


def arxiv_id_from_url(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url or "")
    return m.group(1) if m else None


def arxiv_authors(arxiv_id):
    """Best-effort fetch of the arXiv author list. Returns list or None on any failure."""
    import urllib.request
    import xml.etree.ElementTree as ET
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            tree = ET.fromstring(r.read())
        ns = {"a": "http://www.w3.org/2005/Atom"}
        names = [n.text.strip() for n in tree.findall(".//a:entry/a:author/a:name", ns) if n.text]
        return names or None
    except Exception:
        return None


def process(path):
    """Returns (errors, notes). Normalizes the file in place when needed."""
    errors, notes = [], []
    p = Path(path)
    try:
        text = p.read_text()
    except Exception as e:
        return [f"unreadable: {e}"], notes
    fm, body = split_frontmatter(text)
    if fm is None:
        return ["no YAML frontmatter block"], notes

    folder = p.parent
    slug = p.name[: -len(".summary.md")]
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    new_fm = fm

    # --- Phase 1: normalize ---
    sb = get_scalar(new_fm, "superseded_by")
    if sb is None or sb.strip('"').strip("'").lower() in ("null", "~", "none"):
        new_fm = set_scalar(new_fm, "superseded_by", '""')
        notes.append("superseded_by -> \"\"")

    snap_exists = (folder / f"{slug}.snapshot.md").is_file()
    snap = get_scalar(new_fm, "snapshot")
    want = "true" if snap_exists else "false"
    if (snap or "").strip('"').lower() != want:
        new_fm = set_scalar(new_fm, "snapshot", want)
        notes.append(f"snapshot -> {want} (matches file presence)")

    retrieved = (get_scalar(new_fm, "retrieved") or "").strip('"').strip("'")
    if retrieved.lower() in ("", "null", "~", "none") or not DATE_FULL.match(retrieved):
        new_fm = set_scalar(new_fm, "retrieved", f'"{today}"')
        retrieved = today
        notes.append(f"retrieved -> {today}")

    cc = (get_scalar(new_fm, "currency_check") or "").strip('"').strip("'")
    if cc.lower() in ("", "null", "~", "none") or not DATE_FULL.match(cc) or cc > today:
        # Semantics: last date confirmed current == the intake date. A future
        # date is a due-date misreading of the field; rewrite it.
        new_fm = set_scalar(new_fm, "currency_check", f'"{retrieved}"')
        notes.append(f"currency_check -> {retrieved} (last-confirmed semantics)")

    st = (get_scalar(new_fm, "source_type") or "").strip('"').strip("'")
    if st in SOURCE_TYPE_MAP:
        new_fm = set_scalar(new_fm, "source_type", SOURCE_TYPE_MAP[st])
        notes.append(f"source_type {st} -> {SOURCE_TYPE_MAP[st]}")
        st = SOURCE_TYPE_MAP[st]

    authors, form = get_authors(new_fm)
    if form == "scalar" and authors:
        entry = authors[0]
        block = f'authors:\n  - "{entry}"'
        new_fm = re.sub(r"^authors:[ \t]*.*$", block, new_fm, count=1, flags=re.M)
        notes.append("authors scalar -> block list")
    elif form == "flow" and authors:
        block = "authors:\n" + "\n".join(f'  - "{a}"' for a in authors)
        new_fm = re.sub(r"^authors:[ \t]*.*$", block, new_fm, count=1, flags=re.M)
        notes.append("authors flow list -> block list")

    if new_fm != fm:
        p.write_text("---\n" + new_fm + "---\n" + body)

    # --- Phase 2: validate ---
    for key in ("title", "tldr", "category", "date"):
        v = (get_scalar(new_fm, key) or "").strip('"').strip("'")
        if not v or v.lower() in ("null", "~", "none"):
            errors.append(f"{key}: missing or empty")

    if st not in ENUM:
        errors.append(f"source_type '{st}' not in enum {sorted(ENUM)}")

    date = (get_scalar(new_fm, "date") or "").strip('"').strip("'")
    if date and not DATE_LOOSE.match(date):
        errors.append(f"date malformed: '{date}' (want YYYY[-MM[-DD]])")

    cat = (get_scalar(new_fm, "category") or "").strip('"').strip("'")
    actual_cat = folder.parent.name
    if cat and cat != actual_cat:
        errors.append(f"category '{cat}' != folder '{actual_cat}'")
    if folder.name != slug:
        errors.append(f"folder '{folder.name}' != summary slug '{slug}'")

    if not authors:
        errors.append("authors: missing or empty (use the org name for institutional sources)")
    for a in authors:
        if ET_AL.search(a):
            errors.append(f"authors: placeholder entry '{a}' — full author list required; "
                          "verify against the publisher/arXiv page instead of filing a shortcut")
        elif looks_like_person(a):
            errors.append(f"authors: '{a}' looks like a person name not in \"Last, First\" form")

    # arXiv cross-check (advisory fetch, hard verdict on mismatch)
    url = (get_scalar(new_fm, "url") or "").strip('"').strip("'")
    aid = arxiv_id_from_url(url)
    if aid and authors and not any(ET_AL.search(a) for a in authors):
        api_names = arxiv_authors(aid)
        if api_names is None:
            notes.append(f"arXiv check skipped (fetch failed for {aid})")
        else:
            fm_surnames = [a.split(",")[0].strip().lower() for a in authors]
            first_api = api_names[0].lower()
            if fm_surnames and fm_surnames[0] not in first_api:
                errors.append(f"authors: first author '{authors[0]}' does not match "
                              f"arXiv {aid} first author '{api_names[0]}'")
            if len(api_names) != len(authors):
                errors.append(f"authors: count {len(authors)} != arXiv {aid} count "
                              f"{len(api_names)} ({'; '.join(api_names[:6])}...)")
            if not errors:
                notes.append(f"arXiv check OK ({len(api_names)} authors)")
    return errors, notes


def main(argv):
    paths = []
    if len(argv) >= 2 and argv[0] == "--paths-file":
        try:
            with open(argv[1]) as f:
                paths = [l.strip() for l in f if l.strip()]
        except OSError as e:
            print(f"validate: cannot read paths file: {e}", file=sys.stderr)
            return 2
    else:
        paths = argv
    if not paths:
        print("validate: no summary paths given", file=sys.stderr)
        return 2
    failed = False
    for path in paths:
        errors, notes = process(path)
        for n in notes:
            print(f"validate[{path}]: normalized: {n}")
        for e in errors:
            print(f"validate[{path}]: FAIL: {e}")
        if errors:
            failed = True
        else:
            print(f"validate[{path}]: OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
