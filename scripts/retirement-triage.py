#!/usr/bin/env python3
"""Write a retirement triage report for every retired library source.

A source is retired when its `status:` is superseded, retracted or withdrawn
(or, for summaries predating the field, when `superseded_by:` is set). For
each retired source without a report yet, this writes
$REFBOOK_PATH/triage/<category>--<slug>.retirement.md listing where the
reference book cites it, so obsolescence reaches the book as a proposal
instead of persisting silently.

Same contract as stage-2 relevance triage (refbook triage/README.md): reports
are proposals. This script only ever adds report files; it never edits the
book, REFERENCES.md, STATUS.md, or an existing report. Deterministic, no
model call: citing sections come from REFERENCES.md (`Library path` and
`Cited in`) and from section frontmatter (`sources_used`).

Idempotent: a source that already has a report is skipped. Run by
library-sync.sh after each sync when REFBOOK_PATH is set.

Usage: retirement-triage.py [--dry-run]
Env:   LIBRARY_PATH (required), REFBOOK_PATH (required; exits 0 if unset)
"""
import datetime
import os
import re
import sys
from pathlib import Path

INACTIVE = {"superseded", "retracted", "withdrawn"}


def frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    return text[4:end] if end != -1 else ""


def scalar(fm: str, key: str) -> str:
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    if not m:
        return ""
    return re.sub(r"\s+#.*$", "", m.group(1)).strip().strip('"').strip("'")


def references_index(refbook: Path) -> dict:
    """library 'category/slug' -> {short_cite, cited_in: [subsections]}"""
    out = {}
    path = refbook / "REFERENCES.md"
    if not path.exists():
        return out
    for block in re.split(r"^### ", path.read_text(), flags=re.M)[1:]:
        short_cite = block.splitlines()[0].strip()
        lp = re.search(r"\*\*Library path:\*\*\s*`library/([^/`]+/[^/`]+)/?`", block)
        if not lp:
            continue
        ci = re.search(r"\*\*Cited in:\*\*\s*(.+)", block)
        cited = re.findall(r"\b(\d+(?:\.\d+)?|[A-K](?:\.\d+)?)\b",
                           re.sub(r"^Sections?\s*", "", ci.group(1))) if ci else []
        out[lp.group(1)] = {"short_cite": short_cite, "cited_in": cited}
    return out


def sections_using(refbook: Path, key: str) -> list[str]:
    files = []
    for doc in sorted((refbook / "docs").glob("*.md")):
        if doc.name.endswith(".draft.md"):
            continue
        if re.search(rf'library_path:\s*"?library/{re.escape(key)}/?"?', frontmatter(doc.read_text())):
            files.append(f"docs/{doc.name}")
    return files


def render(key: str, fm: str, ref: dict | None, docs: list[str], successor_ref: dict | None,
           today: str) -> str:
    category, slug = key.split("/")
    status = scalar(fm, "status").lower() or "superseded"
    successor = scalar(fm, "superseded_by")
    cited = (ref or {}).get("cited_in", [])
    short_cite = (ref or {}).get("short_cite", "")
    verdict = "review-citations" if (cited or docs) else "no-effect"
    lines = [
        "---",
        "report_type: source-retirement",
        f'source_slug: "{slug}"',
        f'source_path: "{key}/{slug}.summary.md"',
        f'source_title: "{scalar(fm, "title").replace(chr(34), "")}"',
        f'short_cite: "{short_cite}"',
        f"source_status: {status}",
        f'superseded_by: "{successor}"',
        f'successor_short_cite: "{(successor_ref or {}).get("short_cite", "")}"',
        f'detected: "{today}"',
        f"cited_in: [{', '.join(repr(c).replace(chr(39), chr(34)) for c in cited)}]",
        f"verdict: {verdict}",
        "status: proposed                         # user-managed: proposed | approved | actioned | dismissed",
        'resolution: ""                           # user sets with approval: replace-with-successor |',
        "                                         # keep-as-historical | remove-claims | mixed (say how below)",
        "---",
        "",
        f"# Retired source: {short_cite or slug}",
        "",
        f"The library marked `{key}` as **{status}**."
        + (f" Its successor is `{successor}`" + (f" ({successor_ref['short_cite']})" if successor_ref else "")
           + "." if successor else ""),
        "",
    ]
    if verdict == "no-effect":
        lines += ["## Why no effect", "",
                  "The reference book does not cite this source (no `Cited in` entry in REFERENCES.md "
                  "and no section lists it in `sources_used`). Nothing to change; this report is the "
                  "audit trail.", ""]
        return "\n".join(lines)
    lines += ["## Where the book relies on it", ""]
    if cited:
        lines.append(f"REFERENCES.md `Cited in`: {', '.join('§' + c for c in cited)}")
    if docs:
        lines.append("Sections listing it in `sources_used`: " + ", ".join(f"`{d}`" for d in docs))
    lines += ["",
              "## Decide", "",
              "For each citing passage, one of:",
              "",
              "- **replace-with-successor:** the claim should now rest on the newer source (check the "
              "successor actually supports it; figures often change between editions).",
              "- **keep-as-historical:** the passage cites this source on purpose, e.g. a year-over-year "
              "comparison. Make sure the prose says it is the earlier edition.",
              "- **remove-claims:** the claim no longer holds (typical for a retraction).",
              "",
              "Set `resolution:` and `status: approved` to have `apply-triage` draft the edits as a PR, "
              "or `status: dismissed` to leave the book as it is.",
              ""]
    return "\n".join(lines)


def main() -> int:
    dry = "--dry-run" in sys.argv
    library = Path(os.environ.get("LIBRARY_PATH", "")).expanduser()
    refbook_env = os.environ.get("REFBOOK_PATH", "")
    if not refbook_env:
        return 0
    refbook = Path(refbook_env).expanduser()
    if not library.is_dir() or not (refbook / "docs").is_dir():
        print(f"retirement-triage: library or refbook missing ({library}, {refbook}); skipping")
        return 0
    refs = references_index(refbook)
    today = datetime.date.today().isoformat()
    written = 0
    for summary in sorted(library.glob("*/*/*.summary.md")):
        rel = summary.relative_to(library)
        if rel.parts[0].startswith((".", "_")):
            continue
        key = f"{rel.parts[0]}/{rel.parts[1]}"
        fm = frontmatter(summary.read_text(errors="replace"))
        status = scalar(fm, "status").lower()
        successor = scalar(fm, "superseded_by")
        if not (status in INACTIVE or (not status and successor)):
            continue
        report = refbook / "triage" / f"{rel.parts[0]}--{rel.parts[1]}.retirement.md"
        if report.exists():
            continue
        successor_key = next((k for k in refs if k.split("/")[1] == successor), None) if successor else None
        text = render(key, fm, refs.get(key), sections_using(refbook, key),
                      refs.get(successor_key) if successor_key else None, today)
        if dry:
            print(f"would write {report}")
        else:
            report.parent.mkdir(exist_ok=True)
            report.write_text(text)
            print(f"retirement-triage: wrote {report}")
        written += 1
    if written == 0:
        print("retirement-triage: no new retired sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
