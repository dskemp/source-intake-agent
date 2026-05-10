# Source-Intake Agent

A macOS automation that watches a folder, runs Claude Code's `source-intake`
skill on each dropped file in headless mode, and files the resulting summary
into a personal research library — no human in the loop.

Drop a PDF (or a `.txt` containing a URL, or a saved `.md`/`.html` snapshot)
into your inbox folder → wait → it shows up in
`<library>/<category>/<author-year-slug>/` with a structured `.summary.md`,
the index regenerates, and the input file is removed. Failures land in
`_failed/` with a log sidecar.

> **You choose where the library and inbox live.** Defaults are
> `~/source-library` and `~/source-library-inbox`, but you'll almost certainly
> want to override them — either with a local `.env` (gitignored) or via env
> vars on the install command. See **Configure** below.

## Architecture

```
$INBOX/                                ← drop files here
  ├── .staged/                         (worker-managed working copies)
  └── _failed/                         (quarantined inputs + .log sidecars)

~/.config/claude-source-intake/
  ├── env                              (your ANTHROPIC_API_KEY, mode 0600)
  ├── prompt.txt                       (autonomy prompt — editable from UI)
  ├── runs.jsonl                       (append-only run history)
  ├── current-run.log, last-run.log    (claude stream-json output)
  └── venv/                            (Flask + PyYAML)

~/Library/Scripts/                     (deployed by install.sh)
  ├── claude-source-intake.sh
  ├── claude-source-intake-ui.py
  └── claude-source-intake-regen-index.py

~/Library/LaunchAgents/
  ├── <prefix>.claude-source-intake.plist        (WatchPaths trigger)
  └── <prefix>.claude-source-intake-ui.plist     (dashboard server)
```

**Flow per file drop:**

1. launchd's `WatchPaths` on the inbox fires the worker.
2. **Dedup check** (before staging): SHA-256 the input bytes (and, for
   `.txt`/`.url` inputs, extract the URL). Compare against every existing
   summary's `source_hash:` and `url:` frontmatter fields. On match: move the
   input to `_duplicate/` with a `.log` listing the matched summary, append a
   `duplicate` entry to `runs.jsonl`, **skip claude entirely** (no cost), and
   continue.
3. Otherwise: stage the file into `.staged/<uuid>-<basename>` (atomic).
4. Invoke `claude -p "<autonomy prompt>" --model claude-sonnet-4-6
   --permission-mode acceptEdits --output-format stream-json --verbose`,
   redirecting output to `current-run.log` so the dashboard can tail it live.
5. On success (claude exit 0 + a new `*.summary.md` appears under a category
   folder): inject `source_hash: "<sha256>"` into the new summary's
   frontmatter, delete the staged file, regenerate `INDEX.md`, append a JSONL
   entry with cost + duration to `runs.jsonl`.
6. On failure: move the staged file to `_failed/` with its log next to it.

## Prerequisites

- macOS (uses launchd, `launchctl`, `plutil`, `open`)
- [Claude Code CLI](https://github.com/anthropics/claude-code) on `PATH`
- `python3` (system or homebrew)
- An Anthropic API key

## Install

```sh
git clone <this repo> ~/Developer/source-intake-agent
cd ~/Developer/source-intake-agent
cp .env.example .env       # then edit .env with your real LIBRARY / INBOX
./install.sh               # or `./install.sh --link` (see Sync below)
```

Then set your API key (kept out of git, mode 0600):

```sh
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.config/claude-source-intake/env
chmod 600 ~/.config/claude-source-intake/env
```

Open <http://localhost:7341> to see the dashboard.

## Configure

Configuration is layered — later sources override earlier ones:

1. **Defaults** (placeholders shown below)
2. **`.env` at the repo root** — your personal config; gitignored
3. **Environment variables passed on the command line** to `install.sh`

| Var              | Default                       | Purpose                                       |
| ---------------- | ----------------------------- | --------------------------------------------- |
| `LIBRARY`        | `~/source-library`            | Where summaries are filed                     |
| `INBOX`          | `~/source-library-inbox`      | Watched folder                                |
| `LABEL_PREFIX`   | `com.user`                    | Reverse-DNS prefix for plist labels           |
| `CLAUDE_BIN`     | `$(command -v claude)`        | Path to the `claude` CLI binary               |
| `CATEGORY_ORDER` | *(empty — alphabetical)*      | Comma-separated preferred sort for categories |

Recommended workflow: keep your real paths in `.env` (which is `.gitignore`d)
so they never leak into commit history.

```sh
cat > .env <<'EOF'
LIBRARY=~/path/to/your/library
INBOX=~/path/to/your/inbox
LABEL_PREFIX=com.your-handle
EOF
./install.sh --link
```

`install.sh` is idempotent — re-running it refreshes the deployed scripts and
reloads the launchd agents without touching your API key, run history, or
custom prompt.

## Keeping the repo and the running system in sync

The deployed scripts live at `~/Library/Scripts/` and the launchd plists at
`~/Library/LaunchAgents/`. Two ways to keep them in lockstep with this repo:

### Option A — copy mode (default)

Edit files in the repo, then `./install.sh` to re-deploy. Standard, portable,
safe to delete the repo after install.

```sh
vim scripts/dashboard.py
./install.sh                    # redeploys + reloads launchd
```

### Option B — symlink mode (`--link`)

The deployed scripts become symlinks pointing back into this repo. Edits in
`scripts/*.{sh,py}` are live immediately on the next invocation. **The repo is
the single source of truth — don't move or delete it.**

```sh
./install.sh --link             # one-time setup
# from now on:
vim scripts/dashboard.py
launchctl kickstart -k gui/$(id -u)/<LABEL_PREFIX>.claude-source-intake-ui
                                # bounce dashboard to pick up Python changes
```

What needs reloading after each kind of edit:

| You edit | Action needed |
|---|---|
| `scripts/worker.sh` | nothing — invoked fresh per file drop |
| `scripts/dashboard.py` | `launchctl kickstart -k gui/$(id -u)/<prefix>.claude-source-intake-ui` |
| `scripts/regen-index.py` | nothing — invoked fresh after each successful run |
| `launchd/*.plist.template` | re-run `./install.sh` (re-renders + reloads agents) |
| `config/prompt.txt` | doesn't auto-propagate — see note below |
| `requirements.txt` | re-run `./install.sh` (re-runs pip install) |
| `.env` | re-run `./install.sh` (re-renders plists with new values) |

**Note on the prompt:** `~/.config/claude-source-intake/prompt.txt` is a
*user-customizable* file (editable from the dashboard's Settings panel) and is
deliberately *not* symlinked or overwritten on re-install. To start fresh from
the repo's template, delete the file and run `./install.sh`.

### Verifying which mode you're in

```sh
ls -l ~/Library/Scripts/claude-source-intake.sh
# symlink (→ <repo>/scripts/worker.sh)   ← --link mode
# regular file with byte count           ← copy mode
```

## Day-to-day use

- **Drop a file** in your inbox folder (PDF, `.txt` with a URL, `.md`, or
  `.html`). Watch progress at <http://localhost:7341>.
- **Dashboard pages:**
  - `/` — live status, queue, recent runs (cost + duration), failed items,
    last-run replay, prompt editor.
  - `/library` — searchable, sortable browser of all sources with click-to-open.
- **Pause/resume the watcher** from the dashboard, or:
  ```sh
  touch ~/.config/claude-source-intake/paused      # pause
  rm    ~/.config/claude-source-intake/paused      # resume
  ```
- **Tail logs from a terminal** (parallel to the dashboard view):
  ```sh
  tail -f ~/.config/claude-source-intake/current-run.log
  tail -f /tmp/claude-source-intake.out.log
  ```

## Customizing the autonomy prompt

The prompt at `~/.config/claude-source-intake/prompt.txt` is what gets passed
to `claude -p` for each run. Edit it from the dashboard's Settings panel or
directly. The token `<STAGED_PATH>` is substituted with the staged file's path
at run time. On first install, `__LIBRARY__` in the template is substituted
with your configured library path.

## Deduplication

The worker skips files that duplicate something already in the library, so
accidental re-drops don't cost time or money:

- **Byte-identical PDFs/MDs/HTMLs** are matched by `source_hash:` (SHA-256)
- **`.txt`/`.url` inputs** are matched by `url:` against existing summaries

A duplicate lands in `_duplicate/` with a `.log` sidecar identifying the
matched summary; you'll also see it in the dashboard's Duplicates section.
**No claude invocation, $0 cost** for dedup-rejected files.

To force re-processing of a file you really do want to re-summarize: delete
the matched summary first, then move the file from `_duplicate/` back to the
inbox.

### Backfilling existing summaries

If you're upgrading an installation that pre-dates dedup, run once to add
`source_hash:` to the YAML frontmatter of every existing summary:

```sh
LIBRARY_PATH=$LIBRARY ~/.config/claude-source-intake/venv/bin/python \
  scripts/backfill-hashes.py
```

The script walks the library, hashes each summary's sibling `<slug>.pdf` (or
`.snapshot.md`), and inserts the hash into the frontmatter. Idempotent — safe
to re-run.

## Library schema

Each summary has YAML frontmatter; the auto-generated `INDEX.md` is built from
these fields:

```yaml
---
title: "..."
authors:
  - "Last, First"
source_type: paper
publication: "..."
date: "YYYY-MM-DD"
url: "..."
retrieved: "YYYY-MM-DD"
snapshot: false
category: "your-category"
tldr: "One sentence (~25 words) that stands alone."
source_hash: "<sha256 of the source file>"   # auto-injected by worker; used for dedup
tags:
  - tag1
currency_check: "YYYY-MM-DD"
superseded_by: ""
---
```

The `tldr` field powers the Tl;dr column in `INDEX.md` and the dashboard's
Library page. The skill is instructed to include it on every intake.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Worker fails with `Not logged in · Please run /login` | launchd-spawned `claude` can't see your interactive auth | Add `ANTHROPIC_API_KEY=...` to `~/.config/claude-source-intake/env` (mode 0600) |
| File dropped but nothing happens | mtime guard (5s) is filtering a "still being written" file | Will fire on next event; or click "Run now" in the dashboard |
| Dashboard not at localhost:7341 | Port collision or agent crashed | `tail /tmp/claude-source-intake-ui.err.log`; `launchctl kickstart -k gui/$(id -u)/<prefix>.claude-source-intake-ui` |
| Library page shows zero sources | `LIBRARY` env not set in plist, or summaries lack `---` frontmatter | Re-run `install.sh`; verify summaries have YAML frontmatter |
| `INDEX.md` won't regenerate | PyYAML missing from venv | `~/.config/claude-source-intake/venv/bin/pip install -r requirements.txt` |

Logs to check:

- `/tmp/claude-source-intake.out.log` — worker stdout (per-file processing)
- `/tmp/claude-source-intake.err.log` — worker stderr
- `/tmp/claude-source-intake-ui.err.log` — Flask + binding info
- `~/.config/claude-source-intake/runs.jsonl` — structured run history
- `~/.config/claude-source-intake/last-run.log` — last `claude` invocation's
  stream-json output

## Uninstall

```sh
./uninstall.sh           # removes launchd agents + deployed files; keeps state
./uninstall.sh --purge   # also wipes ~/.config/claude-source-intake (api key,
                         # history, prompt, venv). Library/inbox NEVER touched.
```

## Repo layout

```
source-intake-agent/
├── README.md
├── install.sh
├── uninstall.sh
├── requirements.txt
├── .env.example                 ← copy to .env (gitignored) and customize
├── .gitignore
├── scripts/
│   ├── worker.sh
│   ├── dashboard.py
│   ├── regen-index.py
│   └── backfill-hashes.py    ← one-shot: add source_hash to existing summaries
├── launchd/
│   ├── worker.plist.template
│   └── dashboard.plist.template
└── config/
    └── prompt.txt               ← default autonomy prompt; __LIBRARY__ token
                                    is substituted on first install
```
