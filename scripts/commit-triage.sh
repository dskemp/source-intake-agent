#!/bin/bash
# Commit pending triage reports in the reference-book checkout.
#
# The triage contract (refbook triage/README.md) keeps reports in git, so
# verdicts and the user's decisions survive. Stage 2 and retirement triage
# write reports into the working tree; this commits them. Run by
# library-sync.sh after every sync.
#
# What it commits, as separate commits:
#   - new report files                 "Add triage report(s): <names>"
#   - edits to tracked reports, which in practice are the user's status /
#     resolution / Decision edits      "Record triage decision(s): <names>"
#
# Safety: only triage/*.md (never README.md, never anything outside triage/),
# via `git commit --only`, so nothing else staged or modified in the checkout is
# touched. It commits only when the checkout is on `main` and no merge, rebase,
# cherry-pick or revert is in progress. It never pushes: the commits travel with
# the user's next push or PR. Non-fatal: it prints what it skipped and exits 0.
set -uo pipefail

REFBOOK="${REFBOOK_PATH:-}"
[[ -n "$REFBOOK" && -d "$REFBOOK/triage" ]] || exit 0
cd "$REFBOOK" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

say() { echo "commit-triage: $*"; }

branch=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
if [[ "$branch" != "main" ]]; then
  say "refbook checkout is on '${branch:-detached HEAD}', not main; leaving reports uncommitted"
  exit 0
fi
gitdir=$(git rev-parse --git-dir)
for marker in MERGE_HEAD rebase-merge rebase-apply CHERRY_PICK_HEAD REVERT_HEAD; do
  if [[ -e "$gitdir/$marker" ]]; then
    say "a git operation is in progress ($marker); leaving reports uncommitted"
    exit 0
  fi
done

GIT_IDENT=(-c user.name="source-intake-agent" -c user.email="source-intake-agent@localhost")

names() { sed -e 's|^triage/||' -e 's|\.md$||' | paste -sd, - | sed 's/,/, /g'; }

new=()
while IFS= read -r f; do new+=("$f"); done < <(
  git ls-files --others --exclude-standard -- 'triage/*.md' | grep -v '^triage/README\.md$')
if (( ${#new[@]} )); then
  git add -- "${new[@]}"
  if git "${GIT_IDENT[@]}" commit -q --only -m "Add triage report(s): $(printf '%s\n' "${new[@]}" | names)" -- "${new[@]}"; then
    say "committed ${#new[@]} new report(s)"
  else
    say "WARNING: commit of new reports failed"
  fi
fi

changed=()
while IFS= read -r f; do changed+=("$f"); done < <(
  git diff --name-only -- 'triage/*.md' | grep -v '^triage/README\.md$')
if (( ${#changed[@]} )); then
  # The edits are the user's, made by hand in the working tree; the pipeline
  # only records them. Author it as the user (repo's configured identity).
  if git commit -q --only -m "Record triage decision(s): $(printf '%s\n' "${changed[@]}" | names)" \
       -m "Status/resolution edits made in the working tree, committed by the intake pipeline's sync." \
       -- "${changed[@]}"; then
    say "committed decision edits to ${#changed[@]} report(s)"
  else
    say "WARNING: commit of report edits failed"
  fi
fi
exit 0
