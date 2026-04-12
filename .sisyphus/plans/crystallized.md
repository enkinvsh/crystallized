# Crystallized — Implementation Plan

## Goal
Public repo that gives anyone a complete opencode + oh-my-openagent + persistent memory setup in one `git clone && ./install.sh`.

## Repo Structure
```
crystallized/
├── memory/
│   ├── server.py            # MCP memory server (3-layer: Redis + ChromaDB + docs)
│   ├── memory-inject.py     # Hook: injects relevant memory into every prompt
│   ├── own-voice.py         # Hook: injects agent identity (beliefs, focus, questions)
│   └── pyproject.toml       # Python deps (mcp, redis, chromadb, sentence-transformers)
├── config/
│   └── opencode.json        # Template config: memory MCP + anthropic auth + oh-my-openagent
├── templates/
│   └── notes/
│       └── self/
│           ├── beliefs.md       # Empty — agent fills over time
│           ├── focus.md         # Empty — agent fills over time
│           └── observations.md  # Empty — agent fills over time
├── install.sh               # One-shot setup script
├── .gitignore
└── README.md
```

## Phase 1: Port Memory Server (server.py)

Source: `~/.config/opencode/memory/server.py` (1311 lines)

### Remove:
- Vault layer (Layer 4): lines 748-854 — `vault_store`, `vault_get`, `vault_list`, `vault_delete`
- Vault imports: `from vault import VAULT_DIR, vault_path, encrypt, decrypt` (line 749)
- `from cryptography.exceptions import InvalidTag` (line 750)
- Vault section in `recall()`: lines 637-652 (vault key search in unified recall)
- Vault section in `memory_context()`: lines 727-740 (vault summary)
- Update docstring at top: "Four-layer" → "Three-layer", remove vault mention

### Keep intact:
- Layer 1: Facts (Redis) — save_fact, list_facts, delete_fact
- Layer 2: Semantic (ChromaDB) — remember, search_memory
- Layer 3: Documents (filesystem) — save_doc, read_doc, list_docs, delete_doc
- Cross-layer: recall, memory_context
- Volume system: reinforce, sleep, export_identity, import_identity
- Internal query socket (for memory-inject.py hook)
- All Cyrillic word detection logic
- All event logging

### Verify: server starts, all 3 layers work, vault references gone

## Phase 2: Port memory-inject.py

Source: `~/.config/opencode/memory/memory-inject.py` (342 lines)

### Changes:
- Line 1: `#!/Users/oen/.config/opencode/memory/.venv/bin/python3` → `#!/usr/bin/env python3`
- Venv site-packages discovery (lines 10-19): keep as-is, it's already dynamic via glob

### Keep intact: everything else (semantic socket query, keyword fallback, fact injection, doc listing)

## Phase 3: Port own-voice.py

Source: `~/.config/opencode/memory/own-voice.py` (105 lines)

### Changes: none needed
- All paths use `Path.home()` — already portable
- Reads from `~/.config/opencode/memory/notes/self/` — correct

## Phase 4: Clean pyproject.toml

Source: `~/.config/opencode/memory/pyproject.toml`

### Remove:
- `cryptography>=43.0.0` (vault dep)
- `httpx[socks]>=0.28.1` (not used in server.py directly)

### Result:
```toml
[project]
name = "opencode-memory"
version = "1.0.0"
description = "Three-layer MCP memory server: Redis facts + ChromaDB semantic + filesystem docs"
requires-python = ">=3.11,<3.14"
dependencies = [
    "mcp[cli]>=1.9.0",
    "redis>=5.0.0",
    "chromadb>=1.0.0",
    "sentence-transformers>=3.0.0",
]
```

## Phase 5: opencode.json Template

Minimal config — memory MCP only, no LSP, no extra providers:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": {
      "command": ["uv", "run", "--project", "MEMORY_PATH", "python", "MEMORY_PATH/server.py"],
      "enabled": true,
      "type": "local"
    }
  },
  "plugin": [
    "@thehugeman/opencode-anthropic-auth-community@latest",
    "oh-my-openagent@latest"
  ]
}
```

install.sh will sed MEMORY_PATH → actual `~/.config/opencode/memory` path.

## Phase 6: Identity Templates

Empty markdown files with section headers only. Agent populates them over time through journaling.

**beliefs.md:**
```markdown
# Beliefs
<!-- Your agent will develop beliefs through conversation and reflection -->
<!-- These emerge naturally — don't pre-fill -->
```

**focus.md:**
```markdown
# Current Focus
<!-- What's the agent currently working on / thinking about -->
```

**observations.md:**
```markdown
# Observations

## Strengths
<!-- What the agent does well -->

## Weaknesses
<!-- Patterns to watch for -->
```

## Phase 7: install.sh

### Flow:
1. Detect OS (macOS / Linux)
2. Check deps: git, python3 (>=3.11), curl
3. Install Redis if missing (brew install redis / apt install redis-server)
4. Install uv if missing (curl -LsSf https://astral.sh/uv/install.sh | sh)
5. Install opencode if missing (download binary for platform)
6. Copy memory/ → ~/.config/opencode/memory/
7. Run `uv sync` in memory dir
8. Copy templates/notes/ → ~/.config/opencode/memory/notes/
9. Generate opencode.json from template (sed MEMORY_PATH)
10. Install plugins: opencode plugin handled by oh-my-openagent on first run
11. Start Redis service
12. Print: "Done. Run `opencode` to start."

### Must handle:
- Existing ~/.config/opencode/ — don't overwrite, merge
- Existing Redis — don't reinstall, just verify running
- ARM64 vs x86_64 binary selection for opencode
- macOS (brew) vs Linux (apt/dnf)

## Phase 8: .gitignore

```
.venv/
__pycache__/
*.pyc
chroma_db/
notes/
vault/
identity.json
*.log
.DS_Store
redis-facts-dump.json
```

## Phase 9: README.md

Sections:
- What is this (one paragraph)
- What you get (memory + agents + identity)
- Requirements (Python 3.11+, macOS/Linux)
- Quick start (3 commands)
- How it works (architecture diagram)
- Configuration
- License (MIT)

## Phase 10: Git + GitHub

1. `git init` in crystallized/
2. `gh repo create enkinvsh/crystallized --public`
3. Initial commit + push

## Verification Checklist
- [ ] server.py starts without vault imports
- [ ] memory-inject.py runs with portable shebang
- [ ] own-voice.py runs
- [ ] uv sync installs all deps
- [ ] opencode.json template generates valid config
- [ ] install.sh runs on clean macOS
- [ ] No personal data (facts, memories, vault, identity) in repo
