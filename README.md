<p align="center">
  <code>c r y s t a l l i z e d</code>
  <br><br>
  <strong>Memory that grows. Identity that forms. Auth that works.</strong>
  <br><br>
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-opencode-flags-third-party-clients-and-how-crystallized-fixes-it">Why</a> ·
  <a href="#how-it-works">How</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="#troubleshooting">Troubleshooting</a> ·
  <a href="README.ru.md">Русский</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/MCP-compatible-00CED1.svg" alt="MCP">
</p>

---

# Crystallized — persistent memory and identity for opencode

Crystallized is a persistent memory MCP server for [opencode](https://opencode.ai), the AI coding agent — it gives your agent long-term memory across sessions. It combines three storage layers — a SQLite fact store, ChromaDB semantic vector search, and markdown documents — under power-law memory decay, so important context stays loud while old noise fades and nothing is deleted. On top of memory it forms an evolving agent identity and ships first-party Anthropic authentication, letting you use your Claude Max plan in opencode without third-party client detection.

**v2.0 runs with no external services.** There is no Redis, no broker, and no daemon to babysit; the whole store is a single SQLite database in WAL mode plus plain files under your home directory.

## What you get

- **Three-layer memory**: SQLite for instant facts and beliefs, ChromaDB for semantic search across sessions, filesystem for structured documents
- **Zero external services**: one SQLite file in WAL mode, no Redis, no server to keep running
- **Automatic memory injection**: every prompt gets enriched with relevant context from previous conversations
- **Causal observation**: tool results and session transcripts are mined for friction signals and consolidated into beliefs
- **Agent identity**: beliefs, focus areas, and observations start empty and crystallize over time through work
- **Memory decay**: power-law fading; important things stay loud, old noise goes quiet, nothing is deleted
- **First-party auth**: OAuth tokens extracted from Claude.app; your Max plan, not the $200 third-party credit pool
- **Sisyphus orchestration**: oh-my-openagent with parallel agents, skill loading, structured delegation

## Requirements

- **macOS** (primary) or Linux
- **Python 3.11+**, **git**, **curl**
- **[Claude.app](https://claude.ai/download)**: installed and logged in with your Max account (macOS, for first-party token extraction)

Homebrew is only used as a fallback when an optional extraction tool such as `unzip` is missing. Nothing else needs it.

## Quick start

```sh
# Quit Claude.app first (Cmd+Q)
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
opencode
```

The installer handles Python deps, the opencode CLI, the memory server, the SQLite schema, config merging, hooks, the nightly job, and auth extraction.

Preview everything without touching your machine:

```sh
./install.sh --dry-run
```

## Reproducible installs

The repository tracks `memory/uv.lock` so every install resolves to the same Python dependency versions. To refresh the lockfile after editing `memory/pyproject.toml`, run `uv lock` inside the `memory/` directory and commit the updated lockfile.

## What install.sh does

1. Checks prerequisites: `git`, `python3` 3.11+, and `curl`.
2. Installs the `uv` Python package manager from astral.sh if it is missing.
3. Installs the `opencode` CLI from GitHub releases if it is not already on PATH.
4. Deploys the memory server modules to `~/.config/opencode/memory/` and records exactly what it copied in `.crystallized-manifest`.
5. Installs Python dependencies into `~/.config/opencode/memory/.venv` via `uv sync --frozen`.
6. Initializes the SQLite schema at `~/.config/opencode/memory/memory.db` (WAL mode) by running `python -c "import db; db.init_schema()"`, then prints the resulting `PRAGMA user_version`.
7. Seeds identity templates (beliefs, focus, observations, journal) into `~/.config/opencode/memory/notes/`.
8. Merges `~/.config/opencode/opencode.json` with the memory MCP entry and the plugin entry. The existing file is backed up first, and entries you already defined are never overwritten.
9. Merges the hooks from `config/claude-settings.json` into `~/.claude/settings.json`, creating the file if it does not exist. Only the `hooks` tree is touched; every other key and every foreign hook is preserved, and re-running the installer does not duplicate entries.
10. Installs the nightly consolidation job: a launchd agent at `~/Library/LaunchAgents/com.crystallized.dream.plist` on macOS, or a marked crontab entry on Linux. It runs daily at 04:00 and logs to `~/.config/opencode/memory/dream.log`.
11. On macOS, extracts your Claude.app OAuth tokens via Keychain and writes them to `~/.local/share/opencode/auth.json`.

### Installer flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print every action; change nothing on disk |
| `--no-hooks` | Skip Claude Code hook registration in `~/.claude/settings.json` |
| `--no-daemon` | Skip the nightly consolidation job (launchd / cron) |
| `--offline` | Never reach the network; fail instead of downloading |
| `--help`, `-h` | Show usage and exit |

### Hooks the installer registers

| Event | Command | Purpose |
|---|---|---|
| `UserPromptSubmit` | `memory-inject.py` | Prepends relevant memory to the prompt |
| `UserPromptSubmit` | `own-voice.py` | Prepends the agent's evolving identity |
| `PostToolUse` | `observer.py --post-tool` | Captures friction signals from tool results |
| `Stop` | `observer.py --session-end` | Mines the finished transcript for observations |

## What install.sh does NOT do

- Does not install, start, or require Redis. v2.0 removed it entirely.
- Does not modify Claude.app or its files.
- Does not change your shell rc files. If `opencode` is not on PATH after install, the installer prints a one-line `export PATH=...` hint for you to add yourself.
- Does not phone home. No telemetry, no analytics, no remote logging.
- Does not work on Windows. WSL is not tested.
- Does not detect Claude.app outside `/Applications/Claude.app`. If you installed Claude to a non-default location, run `python3 auth/extract_token.py` manually.

## Caveats

- The Keychain may prompt for your macOS login password during auth extraction. Pick "Always Allow" to skip future prompts.
- Linux skips the automatic auth step. You need to extract tokens from a Mac, or use an API key directly, or accept third-party routing.
- The installer assumes a single-user Mac. Multi-user shared installs are not supported.
- ChromaDB cold start can take 10 to 30 seconds on the first MCP call while the sentence-transformer model downloads.
- The memory directory is shared with anything else you keep there. `uninstall.sh` removes only what the install manifest lists, so your own scripts in that folder are left alone.

## Uninstalling

```sh
./uninstall.sh            # prompts before deleting memory data
./uninstall.sh --keep-data
./uninstall.sh --purge
./uninstall.sh --dry-run
```

`uninstall.sh` unloads and deletes the launchd agent (or removes the cron entry), unregisters the Crystallized hooks from `~/.claude/settings.json` while leaving foreign hooks intact, removes the memory MCP entry from `opencode.json`, deletes the deployed modules and `.venv`, and — only if you agree — removes `memory.db` together with its `-wal` and `-shm` siblings, plus `notes/`, `chroma_db/`, `identity.json`, and any leftover `vault/` from a pre-2.0 install.

## Optional runtime environment variables

All runtime environment variables are optional. The defaults are the paths the installer uses.

| Variable | Purpose |
|---|---|
| `OPENCODE_MEMORY_DB` | Path to the SQLite memory database. |
| `OPENCODE_MEMORY_NOTES_DIR` | Notes directory for saved documents and identity files. |
| `OPENCODE_MEMORY_CHROMA_DIR` | ChromaDB persistence directory. |
| `OPENCODE_MEMORY_IDENTITY` | Path to the exported volume map. |
| `OPENCODE_MEMORY_SOCKET` | Unix socket the hooks use to reach the warm encoder. |
| `OPENCODE_MEMORY_DISABLE_CHROMA_API` | Set to `0` to re-enable the in-process ChromaDB API. Defaults to disabled. |
| `CRYSTALLIZED_OBSERVER_BUDGET_MS` | Observer hook deadline in milliseconds. |

## Upgrading from v1.x

v2.0 replaces the Redis fact layer with SQLite. Pull the new code and re-run the installer:

```sh
git pull
./install.sh
```

The installer backs up `opencode.json` and `~/.claude/settings.json` before merging, so nothing you configured is lost. After the upgrade you can stop Redis if you started it only for Crystallized:

```sh
brew services stop redis     # macOS
sudo systemctl stop redis    # Linux
```

The `REDIS_URL`, `REDIS_HOST`, `REDIS_PORT`, and `REDIS_DB` variables are no longer read by anything.

If something breaks, run `./uninstall.sh --keep-data`, then re-run `./install.sh`.

See [CHANGELOG.md](CHANGELOG.md) for full release notes.

## Why opencode flags third-party clients (and how Crystallized fixes it)

Anthropic detects third-party clients and routes their API calls to a separate $200 credit pool instead of your Max subscription. Community auth plugins obtain OAuth tokens with a third-party `client_id`, so every request gets flagged.

Crystallized extracts tokens directly from Claude.app. These carry Claude's own `client_id`, so Anthropic treats your opencode sessions as first-party. Max plan limits apply normally.

## How it works

### Three-layer memory architecture (SQLite + ChromaDB + filesystem)

`memory-inject.py` runs as a pre-prompt hook on every message. It searches the layers for relevant context and prepends it:

| Layer | Engine | Purpose |
|---|---|---|
| Facts and beliefs | SQLite (WAL) | Names, decisions, preferences, instant key/value lookups |
| Semantic | ChromaDB | Vector similarity across everything the agent ever remembered |
| Documents | Filesystem | Architecture notes, checklists, session summaries |

Decay runs on a power-law schedule. Memories are never deleted, they get quieter.

### Observation and nightly consolidation

`observer.py` runs on `PostToolUse` and `Stop`. It applies regex patterns to tool output and finished transcripts, and writes raw `layer=0` rows into `causal_memories`. It is deliberately cheap: it has a millisecond budget, always exits `0`, and never prints on the tool path, so a hook can never abort or slow down a turn.

The expensive half runs while you are not working. The nightly job promotes raw observations upward — raw signal to episode to pattern to principle — and resolves contradictions into `belief_state`, a bi-temporal table where exactly one belief per subject and predicate can be active at a time. Superseded beliefs are marked, not deleted, so the history of what the agent used to think stays queryable.

### Identity

`own-voice.py` injects the agent's evolving identity into each prompt. Beliefs, focus areas, and observations start as empty files and fill up as the agent works. The personality is earned through experience, not configured upfront.

### Authentication

`auth/extract_token.py` decrypts Claude.app's Electron safeStorage (AES-128-CBC via macOS Keychain), extracts OAuth tokens, and writes them to opencode's `auth.json`. Token refresh is handled by opencode internally, no auth plugin at runtime.

## Architecture

```
~/.config/opencode/
├── opencode.json              # MCP servers, plugins
└── memory/
    ├── server.py              # MCP memory server (SQLite + ChromaDB + fs)
    ├── db.py                  # SQLite storage layer, schema migrations
    ├── volume.py              # Power-law decay math
    ├── observer.py            # Hook: PostToolUse / Stop signal capture
    ├── patterns.py            # Regex patterns the observer applies
    ├── dream.py               # Nightly consolidation pass
    ├── memory-inject.py       # Hook: context injection
    ├── own-voice.py           # Hook: identity injection
    ├── pyproject.toml         # Python dependencies
    ├── .crystallized-manifest # What install.sh deployed (used by uninstall.sh)
    ├── memory.db              # SQLite store, WAL mode (generated)
    ├── chroma_db/             # Vector database (generated)
    ├── notes/self/            # Agent identity (generated)
    │   ├── beliefs.md
    │   ├── focus.md
    │   └── observations.md
    ├── identity.json          # Volume map (generated)
    └── dream.log              # Nightly job output (generated)

~/.claude/
└── settings.json              # Hook registrations (merged, never overwritten)

~/Library/LaunchAgents/
└── com.crystallized.dream.plist   # Nightly consolidation (macOS)

~/.local/share/opencode/
└── auth.json                  # OAuth tokens (from Claude.app)
```

## Troubleshooting

**"Third-party apps" error**, wrong token. Quit Claude.app, then:
```sh
python3 auth/extract_token.py
```
Try each index if you have multiple workspaces.

**Memory MCP is red**, verify the `uv` path in `opencode.json` is absolute. The installer writes it that way, but manual edits can break it. Then check the store opens:

```sh
cd ~/.config/opencode/memory && .venv/bin/python -c "import db; db.init_schema(); print('ok')"
```

**Hooks are not firing**, confirm they are registered and that the interpreter path exists:

```sh
python3 -c "import json;print(json.load(open('$HOME/.claude/settings.json'))['hooks'].keys())"
ls ~/.config/opencode/memory/.venv/bin/python
```

Re-run `./install.sh` to re-register; it will not duplicate existing entries.

**Nightly job never runs**, check it is loaded and read the log:

```sh
launchctl list | grep crystallized      # macOS
crontab -l | grep crystallized-dream    # Linux
tail ~/.config/opencode/memory/dream.log
```

**Keychain access denied**, needs GUI terminal, not pure SSH. Or unlock first:
```sh
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

**Linux**, auth extraction is macOS-only (Claude.app). Bring tokens from a Mac, use an API key directly, or accept third-party routing.

## Development

The memory server is a standalone Python package managed with [uv](https://astral.sh). Run the test suite:

```sh
cd memory && uv run pytest tests/ -q
```

Lint with ruff before sending a change:

```sh
uv run ruff check .
```

The shell scripts are checked with `bash -n`, and `--dry-run` on both scripts is the fastest way to review a change end to end:

```sh
bash -n install.sh && bash -n uninstall.sh
./install.sh --dry-run
./uninstall.sh --dry-run
```

Release notes live in [CHANGELOG.md](CHANGELOG.md); the threat model and disclosure policy live in [SECURITY.md](SECURITY.md).

## FAQ

**Does opencode forget context between sessions?**
By default, yes — each session starts cold. Crystallized fixes this: it persists facts, semantic memories, and documents to disk and re-injects the relevant ones into every prompt, so the agent carries long-term memory across sessions.

**Will this work with other MCP clients, or only opencode?**
The memory server is a standard [Model Context Protocol](https://modelcontextprotocol.io) server, so any MCP client can connect to it. The installer, pre-prompt hooks, and identity injection are tailored to opencode, so other clients get the memory tools but not the automatic context injection.

**Is my data sent anywhere?**
No. Memory lives entirely on your machine — one SQLite file, ChromaDB, and markdown files on local disk. Nothing listens on a network port. There is no telemetry, no analytics, and no remote logging. See [SECURITY.md](SECURITY.md) for the full threat model.

**Do I still need Redis?**
No. v2.0 removed it. Facts, beliefs, volumes, and the event log all live in a single SQLite database in WAL mode, so there is no service to install, start, or keep alive.

**Why does Anthropic route opencode to a $200 credit pool?**
Anthropic detects third-party clients by their OAuth `client_id` and bills them against a separate credit pool instead of your subscription. Crystallized extracts first-party tokens from Claude.app, so opencode is treated as first-party and your Claude Max plan limits apply normally.

**Does memory grow unbounded?**
No. Memory decays on a power-law schedule: entries get quieter over time unless they are reinforced. Nothing is ever deleted — old noise just stops surfacing while important context stays loud.

**Is there Windows support?**
No. Crystallized targets macOS (primary) and Linux. WSL is untested, and auth extraction depends on macOS Keychain.

## License

[MIT](LICENSE)
