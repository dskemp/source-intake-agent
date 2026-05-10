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
2. Worker stages each file into `.staged/<uuid>-<basename>` (atomic).
3. Invokes `claude -p "<autonomy prompt>" --model claude-sonnet-4-6
   --permission-mode acceptEdits --output-format stream-json --verbose`,
   redirecting output to `current-run.log` so the dashboard can tail it live.
4. On success (claude exit 0 + a new `*.summary.md` appears under a category
   folder): deletes the staged file, regenerates `INDEX.md` from frontmatter,
   appends a JSONL entry with cost + duration to `runs.jsonl`.
5. On failure: moves the staged file to `_failed/` with its log next to it.

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
│   └── regen-index.py
├── launchd/
│   ├── worker.plist.template
│   └── dashboard.plist.template
└── config/
    └── prompt.txt               ← default autonomy prompt; __LIBRARY__ token
                                    is substituted on first install
```
