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
# cherry-pick or revert is in progress. It then pushes the commits, but only
# if every unpushed commit is report-only, and never forced (see the end of this
# file). Non-fatal: it prints what it skipped and exits 0.
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

# Push report-only commits so the checkout doesn't diverge from the remote
# every time a PR merges there. Reports are proposals, not book content, so
# they go straight to main, but only when EVERY unpushed commit touches
# triage/*.md and nothing else. Never forced. If the remote has moved on, the
# report-only commits are rebased first, and only on a clean working tree.
# TRIAGE_PUSH=0 turns this off.
[[ "${TRIAGE_PUSH:-1}" == "1" ]] || exit 0
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) || exit 0
remote=${upstream%%/*}
GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15" git fetch -q "$remote" 2>/dev/null \
  || { say "fetch from $remote failed; not pushing"; exit 0; }
ahead=$(git rev-list "$upstream..HEAD")
[[ -n "$ahead" ]] || exit 0
for c in $ahead; do
  if git diff-tree --no-commit-id --name-only -r "$c" | grep -qv '^triage/.*\.md$'; then
    say "unpushed commit $(git rev-parse --short "$c") touches files outside triage/; leaving it for the user to push"
    exit 0
  fi
done
if [[ -n "$(git rev-list "HEAD..$upstream")" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    say "$upstream has moved on and the working tree has changes; not rebasing (run: git pull --rebase)"
    exit 0
  fi
  if ! git rebase -q "$upstream" 2>/dev/null; then
    git rebase --abort 2>/dev/null
    say "rebase of report commits onto $upstream failed; aborted, nothing pushed"
    exit 0
  fi
  say "rebased $(git rev-list --count "$upstream..HEAD") report commit(s) onto $upstream"
fi
if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15" git push -q "$remote" "HEAD:${upstream#*/}" 2>/dev/null; then
  say "pushed $(echo "$ahead" | wc -l | tr -d ' ') report commit(s) to $upstream"
else
  say "push to $upstream failed; will retry on the next sync"
fi
exit 0
