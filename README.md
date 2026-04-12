# crystallized

Persistent memory, growing identity, and proper authentication for [opencode](https://opencode.ai) — ready in one command.

## What is this

Crystallized gives your opencode agent a three-layer memory system, an identity that develops over time, and a working authentication flow that bypasses Anthropic's third-party client detection. Clone, install, run.

## What you get

- **Three-layer memory** — Redis for fast key/value facts, ChromaDB for semantic search across past sessions, filesystem markdown for structured documents
- **Automatic memory injection** — every prompt gets enriched with relevant context from previous conversations, transparently
- **Agent identity system** — beliefs, focus areas, and observations start empty and grow as the agent works; the personality is earned, not configured
- **Volume-based memory decay** — memories fade via power-law decay; important things stay loud, old noise goes quiet, nothing is ever deleted
- **First-party authentication** — extracts OAuth tokens directly from Claude.app, so opencode runs on your Max plan without third-party restrictions
- **Sisyphus orchestration** — oh-my-openagent plugin with parallel agents, skill loading, and structured task execution

## Requirements

- **macOS** (primary) or Linux
- **Python 3.11+**
- **Claude.app** installed and logged in with your Anthropic Max account
- **Homebrew** on macOS ([brew.sh](https://brew.sh))

## Quick start

```sh
# 1. Make sure Claude.app is installed and you're logged in
# 2. Quit Claude.app fully (Cmd+Q)

git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh

# 3. Start working
opencode
```

The installer handles everything: Redis, Python dependencies, opencode CLI, memory server, agent config, and authentication.

## Why this exists

Anthropic detects third-party clients and routes their API calls to a separate $200 credit pool instead of your Max subscription. Community auth plugins like `@thehugeman/opencode-anthropic-auth-community` obtain OAuth tokens with a third-party client ID, which triggers this detection on every request.

Crystallized solves this by extracting OAuth tokens directly from Claude.app. These tokens carry Claude's own client ID, so Anthropic treats your opencode sessions as first-party. Your Max plan limits apply normally.

## How it works

### Memory

Every time you start a session, `memory-inject.py` runs as a pre-prompt hook. It searches all three memory layers for context relevant to your current task and prepends it to your prompt:

- **Redis** — structured facts (names, decisions, preferences, quick lookups)
- **ChromaDB** — vector similarity search across everything the agent has ever remembered
- **Filesystem** — longer documents like architecture notes, checklists, session summaries

Memory decay runs on a power-law schedule: a memory accessed yesterday stays strong, one from six months ago fades, but nothing is ever hard-deleted.

### Identity

The identity system lives in `own-voice.py`. As the agent works, it accumulates beliefs, sharpens its focus, and logs observations. These are injected alongside memory context, so the agent's responses reflect its history with you. The personality starts as an empty substrate and crystallizes over time.

### Authentication

`auth/extract_token.py` reads Claude.app's encrypted token cache (`~/Library/Application Support/Claude/config.json`), decrypts it using the macOS Keychain password for Electron's safeStorage, and writes the OAuth tokens to opencode's `auth.json`. opencode handles token refresh internally — no auth plugin needed at runtime.

## Architecture

```
~/.config/opencode/
├── opencode.json            # Config: MCP servers, plugins
└── memory/
    ├── server.py            # MCP memory server (Redis + ChromaDB + filesystem)
    ├── memory-inject.py     # Pre-prompt hook: injects relevant context
    ├── own-voice.py         # Pre-prompt hook: injects agent identity
    ├── pyproject.toml       # Python dependencies
    ├── chroma_db/           # Vector database (generated)
    ├── notes/               # Markdown documents (generated)
    │   └── self/            # Agent identity files
    │       ├── beliefs.md
    │       ├── focus.md
    │       └── observations.md
    └── identity.json        # Volume map / personality fingerprint (generated)

~/.local/share/opencode/
└── auth.json                # OAuth tokens (extracted from Claude.app)
```

Redis runs as a system service on localhost:6379. The MCP server exposes tools like `save_fact`, `remember`, `save_doc`, `recall`, and `sleep` (decay) to the agent at runtime.

## Troubleshooting

**"Third-party apps" error** — Your auth token has a third-party client ID. Quit Claude.app (Cmd+Q), then run:
```sh
python3 auth/extract_token.py
```
Pick the token that works. If you have multiple workspaces, try each index.

**Memory MCP is red** — Check that Redis is running (`redis-cli ping`), and that the `uv` path in `opencode.json` is absolute (not just `uv`). The installer writes the absolute path automatically.

**Keychain access denied** — The script needs the "Claude Safe Storage" Keychain entry. Run it from a terminal with GUI access (not pure SSH). Or unlock the Keychain first: `security unlock-keychain ~/Library/Keychains/login.keychain-db`

**Linux** — Claude.app doesn't exist on Linux, so the auth extraction step is skipped. You'll need to either: bring tokens from a Mac, use an API key, or use the community auth plugin (with the caveat that it may trigger third-party detection).

## License

MIT
