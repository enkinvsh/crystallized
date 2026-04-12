# crystallized

Persistent memory and growing identity for your AI agent, ready in one command.

## What is this

Crystallized gives your opencode agent a three-layer memory system and an identity that develops over time. Clone it, run the installer, and your agent will remember past sessions, inject relevant context into every prompt, and gradually build its own voice through accumulated beliefs and observations.

## What you get

- **Three-layer memory** — Redis for fast key/value facts, ChromaDB for semantic search across past sessions, filesystem markdown for structured documents
- **Automatic memory injection** — every prompt gets enriched with relevant context from previous conversations, transparently
- **Agent identity system** — beliefs, focus areas, and observations start empty and grow as the agent works; the personality is earned, not configured
- **Volume-based memory decay** — memories are never deleted, they fade via power-law decay; important things stay loud, old noise goes quiet
- **Sisyphus agent orchestration** — full oh-my-openagent plugin with parallel agents, skill loading, and structured task execution

## Requirements

- Python 3.11+
- macOS or Linux
- An Anthropic account (API key)

## Quick start

```sh
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
```

## How it works

Every time you start a session, `memory-inject.py` runs as a pre-prompt hook. It searches all three memory layers for context relevant to your current task and prepends that context to your prompt. Redis handles structured facts (names, decisions, preferences). ChromaDB does vector similarity search across everything the agent has ever seen. The markdown layer stores longer documents like architecture notes or checklists.

The identity system lives in `own-voice.py`. As the agent works, it accumulates beliefs, sharpens its focus, and logs observations about the codebase and project. These are injected alongside memory context, so the agent's responses reflect its history with your project. Memory decay runs on a power-law schedule: a memory accessed yesterday stays strong, one from six months ago fades, but nothing is ever hard-deleted.

## Architecture

```
~/.config/opencode/
├── opencode.json            # Config: MCP servers, plugins
└── memory/
    ├── server.py            # MCP memory server (runs via opencode)
    ├── memory-inject.py     # Pre-prompt hook: injects relevant context
    ├── own-voice.py         # Pre-prompt hook: injects agent identity
    ├── pyproject.toml       # Python dependencies
    ├── chroma_db/           # ChromaDB vector database (generated)
    ├── notes/               # Markdown documents (generated)
    │   └── self/            # Agent identity files
    │       ├── beliefs.md
    │       ├── focus.md
    │       └── observations.md
    └── identity.json        # Volume map / personality fingerprint (generated)
```

Redis stores facts and volume scores externally (localhost:6379). The MCP server exposes tools like `save_fact`, `remember`, `save_doc`, `recall`, and `sleep` (decay) to the agent at runtime.

## Configuration

Your main config lives at `~/.config/opencode/opencode.json`. After install, it will have:

- The memory MCP server registered under `mcpServers`
- The Anthropic community plugin for authentication
- The oh-my-openagent plugin for Sisyphus orchestration

The memory server connects to Redis on localhost:6379 and stores ChromaDB data in `~/.config/opencode/memory/chroma_db/`. To export the agent's identity (volume map) for backup or transfer, use the `export_identity` tool from within a session.

## License

MIT
