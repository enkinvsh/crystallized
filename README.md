<p align="center">
  <code>c r y s t a l l i z e d</code>
  <br><br>
  <strong>Memory that grows. Identity that forms. Auth that works.</strong>
  <br><br>
  <a href="#quick-start">Quick Start</a> ·
  <a href="#why-this-exists">Why</a> ·
  <a href="#how-it-works">How</a> ·
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

Persistent memory, growing identity, and first-party Anthropic authentication for [opencode](https://opencode.ai). One command setup.

## What you get

- **Three-layer memory**: Redis for instant facts, ChromaDB for semantic search across sessions, filesystem for structured documents
- **Automatic memory injection**: every prompt gets enriched with relevant context from previous conversations
- **Agent identity**: beliefs, focus areas, and observations start empty and crystallize over time through work
- **Memory decay**: power-law fading; important things stay loud, old noise goes quiet, nothing is deleted
- **First-party auth**: OAuth tokens extracted from Claude.app; your Max plan, not the $200 third-party credit pool
- **Sisyphus orchestration**: oh-my-openagent with parallel agents, skill loading, structured delegation

## Requirements

- **macOS** (primary) or Linux
- **Python 3.11+**
- **[Claude.app](https://claude.ai/download)**: installed and logged in with your Max account
- **[Homebrew](https://brew.sh)** on macOS

## Quick start

```sh
# Quit Claude.app first (Cmd+Q)
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
opencode
```

The installer handles Redis, Python deps, opencode CLI, memory server, config, and auth extraction.

## Reproducible installs

The repository tracks `memory/uv.lock` so every install resolves to the same Python dependency versions. To refresh the lockfile after editing `memory/pyproject.toml`, run `uv lock` inside the `memory/` directory and commit the updated lockfile.

## What install.sh does

- Checks prerequisites (Git, Python 3.11+, Homebrew on macOS, jq).
- Installs and starts Redis via Homebrew (or `apt` on Linux), or shares an existing local Redis on port 6379.
- Installs the `uv` Python package manager from astral.sh.
- Installs the `opencode` CLI from GitHub releases if it is not already on PATH.
- Deploys the memory MCP server scripts to `~/.config/opencode/memory/`.
- Installs Python dependencies into `~/.config/opencode/memory/.venv` via `uv sync --frozen`.
- Seeds identity templates (beliefs, focus, observations, journal) into `~/.config/opencode/memory/notes/`.
- Writes or merges `~/.config/opencode/opencode.json` with the memory MCP entry and pre-prompt hooks. Before merging, it backs up the existing file.
- On macOS, extracts your Claude.app OAuth tokens via Keychain and writes them to `~/.local/share/opencode/auth.json`.

## What install.sh does NOT do

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

## Optional runtime environment variables

All runtime environment variables are optional. If you do not set them, the defaults preserve v1.0 behavior.

| Variable | Purpose |
|---|---|
| `REDIS_URL` | Full Redis connection URL. |
| `REDIS_HOST` | Redis host when `REDIS_URL` is not set. |
| `REDIS_PORT` | Redis port when `REDIS_URL` is not set. |
| `REDIS_DB` | Redis database number. |
| `OPENCODE_MEMORY_SOCKET` | MCP socket path for memory hooks. |
| `OPENCODE_MEMORY_NOTES_DIR` | Notes directory for saved documents and identity files. |
| `OPENCODE_MEMORY_CHROMA_DIR` | ChromaDB persistence directory. |

## Upgrading from v1.0

Pull the new code, then re-run `./install.sh`. The installer backs up `opencode.json` before merging the new memory hooks and MCP config.

The new environment variables are optional. If you do not set them, the defaults preserve v1.0 behavior.

If something breaks, run `./uninstall.sh`, then re-run `./install.sh`.

See [CHANGELOG.md](CHANGELOG.md) for full release notes.

## Why this exists

Anthropic detects third-party clients and routes their API calls to a separate $200 credit pool instead of your Max subscription. Community auth plugins obtain OAuth tokens with a third-party `client_id`, so every request gets flagged.

Crystallized extracts tokens directly from Claude.app. These carry Claude's own `client_id`, so Anthropic treats your opencode sessions as first-party. Max plan limits apply normally.

## How it works

### Memory

`memory-inject.py` runs as a pre-prompt hook on every message. It searches all three layers for relevant context and prepends it:

| Layer | Engine | Purpose |
|---|---|---|
| Facts | Redis | Names, decisions, preferences, instant key/value lookups |
| Semantic | ChromaDB | Vector similarity across everything the agent ever remembered |
| Documents | Filesystem | Architecture notes, checklists, session summaries |

Decay runs on a power-law schedule. Memories are never deleted, they get quieter.

### Identity

`own-voice.py` injects the agent's evolving identity into each prompt. Beliefs, focus areas, and observations start as empty files and fill up as the agent works. The personality is earned through experience, not configured upfront.

### Authentication

`auth/extract_token.py` decrypts Claude.app's Electron safeStorage (AES-128-CBC via macOS Keychain), extracts OAuth tokens, and writes them to opencode's `auth.json`. Token refresh is handled by opencode internally, no auth plugin at runtime.

## Architecture

```
~/.config/opencode/
├── opencode.json              # MCP servers, plugins
└── memory/
    ├── server.py              # MCP memory server (Redis + ChromaDB + fs)
    ├── memory-inject.py       # Pre-prompt hook: context injection
    ├── own-voice.py           # Pre-prompt hook: identity injection
    ├── pyproject.toml         # Python dependencies
    ├── chroma_db/             # Vector database (generated)
    ├── notes/self/            # Agent identity (generated)
    │   ├── beliefs.md
    │   ├── focus.md
    │   └── observations.md
    └── identity.json          # Volume map (generated)

~/.local/share/opencode/
└── auth.json                  # OAuth tokens (from Claude.app)
```

## Troubleshooting

**"Third-party apps" error**, wrong token. Quit Claude.app, then:
```sh
python3 auth/extract_token.py
```
Try each index if you have multiple workspaces.

**Memory MCP is red**, `redis-cli ping` should return PONG. Also verify the `uv` path in `opencode.json` is absolute. The installer handles this, but manual edits can break it.

**Keychain access denied**, needs GUI terminal, not pure SSH. Or unlock first:
```sh
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

**Linux**, auth extraction is macOS-only (Claude.app). Bring tokens from a Mac, use an API key directly, or accept third-party routing.

## License

[MIT](LICENSE)
