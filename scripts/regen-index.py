#!/usr/bin/env python3
"""Regenerate INDEX.md from each summary's YAML frontmatter.

Walks the library, reads `.summary.md` files, groups by their folder
category, and writes a single INDEX.md.
"""
import os
import re
import sys
from pathlib import Path

import yaml

LIBRARY = Path(os.environ.get("LIBRARY_PATH") or (Path.home() / "source-library"))

# Optional preferred display order for categories in INDEX.md.
# Set as comma-separated names via env: CATEGORY_ORDER="cat-a,cat-b,cat-c"
# Categories not listed here are appended alphabetically at the end.
PREFERRED_ORDER = [
    c.strip() for c in os.environ.get("CATEGORY_ORDER", "").split(",") if c.strip()
]


def parse_frontmatter(path: Path) -> dict | None:
    """Return frontmatter dict or None if no frontmatter found."""
    try:
        text = path.read_text()
    except Exception:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    body = text[4:end]
    try:
        return yaml.safe_load(body) or {}
    except yaml.YAMLError:
        return None


def collect_sources(library: Path):
    """Return {category: [source_dict, ...]} sorted within category."""
    by_cat: dict[str, list[dict]] = {}
    for summary in library.glob("*/*/*.summary.md"):
        category = summary.parent.parent.name
        if category.startswith(".") or category.startswith("_"):
            continue
        fm = parse_frontmatter(summary)
        if fm is None:
            continue
        rel_path = summary.relative_to(library).as_posix()
        authors = fm.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        date = str(fm.get("date") or "")
        year = date[:4] if len(date) >= 4 else ""
        by_cat.setdefault(category, []).append({
            "title": fm.get("title") or summary.stem,
            "tldr": (fm.get("tldr") or "").strip(),
            "authors": authors,
            "year": year,
            "date": date,
            "tags": fm.get("tags") or [],
            "rel_path": rel_path,
            "category": category,
        })
    for sources in by_cat.values():
        sources.sort(key=lambda s: (s["date"], s["title"]), reverse=True)
    return by_cat


def category_order(by_cat: dict) -> list[str]:
    seen = set(by_cat.keys())
    ordered = [c for c in PREFERRED_ORDER if c in seen]
    extras = sorted(c for c in seen if c not in PREFERRED_ORDER)
    return ordered + extras


# Heuristics for distinguishing institutional authors (e.g. "U.S. Government
# Accountability Office") from individuals ("Vaswani, Ashish"). Keep these
# definitions in sync with the copy in dashboard.py.
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
    """Return True if `name` looks like an organization, not an individual.

    Heuristic priority:
    1. Known-org acronym / short name -> True
    2. Contains a venue hint (arXiv, Nature, ICML...) -> False  (defensive: a
       venue string accidentally listed in authors: should not be treated as
       an institution)
    3. Contains an org keyword (Office, Bureau, Association...) -> True
    4. No comma AND >=4 words -> True (multi-word non-person names)
    5. Otherwise -> False
    """
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


def _display_author(name: str) -> str:
    if is_institutional_author(name):
        return name
    if "," in name:
        return name.split(",", 1)[0].strip()
    return name.split()[-1]


def render_authors(authors: list[str], max_shown: int = 4) -> str:
    if not authors:
        return ""
    displayed = [_display_author(a) for a in authors]
    if len(displayed) <= max_shown:
        return ", ".join(displayed)
    return ", ".join(displayed[:max_shown]) + " et al."


def render_index(by_cat: dict, library: Path) -> str:
    lines = [
        "# Source Index",
        "",
        "A browsable index of all sources in the library, organized by category. "
        "Auto-generated from each summary's YAML frontmatter on every successful intake — "
        "do not hand-edit this file.",
        "",
    ]
    cats = category_order(by_cat)
    if not cats:
        lines.append("*No sources yet.*")
        return "\n".join(lines) + "\n"
    # also show empty placeholder categories that exist as folders
    empty = sorted(
        p.name for p in library.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
        and p.name not in by_cat
    )
    for cat in cats:
        sources = by_cat[cat]
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Title | Tl;dr | Author(s) | Year |")
        lines.append("|-------|-------|-----------|------|")
        for s in sources:
            title_link = f"[{escape_pipe(s['title'])}]({s['rel_path']})"
            tldr = escape_pipe(s["tldr"]) or "—"
            authors = escape_pipe(render_authors(s["authors"]))
            year = s["year"] or "—"
            lines.append(f"| {title_link} | {tldr} | {authors} | {year} |")
        lines.append("")
    for cat in empty:
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("*No sources yet.*")
        lines.append("")
    return "\n".join(lines)


def escape_pipe(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def main():
    by_cat = collect_sources(LIBRARY)
    rendered = render_index(by_cat, LIBRARY)
    out = LIBRARY / "INDEX.md"
    out.write_text(rendered)
    total = sum(len(v) for v in by_cat.values())
    print(f"INDEX.md regenerated ({total} sources across {len(by_cat)} categories)")


if __name__ == "__main__":
    sys.exit(main())
