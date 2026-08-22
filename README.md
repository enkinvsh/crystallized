<p align="center">
  <code>c r y s t a l l i z e d</code>
  <br><br>
  <strong>Self-evolving causal memory. Bi-temporal beliefs. First-party authentication.</strong>
  <br><br>
  <a href="#quick-start">Quick Start</a> .
  <a href="#architecture">Architecture</a> .
  <a href="#memory-hierarchy">Memory Hierarchy</a> .
  <a href="#authentication">Authentication</a> .
  <a href="#mcp-tools">MCP Tools</a> .
  <a href="#troubleshooting">Troubleshooting</a> .
  <a href="README.ru.md">Русский</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/version-2.0.0-success.svg" alt="Version 2.0.0">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/MCP-compatible-00CED1.svg" alt="MCP">
</p>

***

# Crystallized v2.0

Crystallized is a persistent memory MCP server built for [opencode](https://opencode.ai). It gives your coding agent continuous long-term recall, evolving self-identity, and reliable first-party authentication across sessions.

Standard agent sessions start completely cold. Crystallized changes this by capturing real workflow friction, distilling raw traces into causal principles, and automatically injecting relevant context into every prompt.

**v2.0 runs with zero external infrastructure.** No Redis instances, no background broker services, and no cloud dependencies. The entire system is powered by a single SQLite database running in WAL mode alongside local markdown files.

```
+-------------------------------------------------------------------------+
|                               USER PROMPT                               |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| PRE-PROMPT HOOKS (memory-inject.py + own-voice.py)                      |
| * Injects active beliefs, facts, and past causal lessons                |
| * Injects agent self-voice, current focus, and observations            |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| AGENT EXECUTION (opencode + MCP tools)                                  |
| * Reads/writes facts, docs, and beliefs via standard MCP tools          |
| * Runs sub-millisecond observer hook on every tool result               |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| CAUSAL OBSERVATION (observer.py)                                        |
| * PostToolUse / Stop hooks capture friction without blocking execution  |
| * Appends Layer 0 raw traces to SQLite WAL                              |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
| NIGHTLY DREAM CONSOLIDATION (dream.py @ 04:00)                          |
| * L0 Traces -> L1 Episodes -> L2 Patterns -> L3 Axioms                  |
| * Bi-temporal belief reconciliation + power-law memory decay            |
+-------------------------------------------------------------------------+
```

---

## Core Capabilities

- **Zero-Redis, 100% SQLite WAL**: Single file database at `~/.config/opencode/memory/memory.db`. Instant startup, crash-safe concurrency, zero services to babysit.
- **Hierarchical Causal Memory**: Promotes raw tool traces (L0) through episodes (L1) and recurring patterns (L2) into high-level axioms (L3).
- **Bi-Temporal Belief State**: Tracks both system-validity time and assertion time. Contradictions are resolved cleanly by superseding stale records without deleting history.
- **Sub-Millisecond Observer Hook**: `observer.py` matches friction patterns against tool outputs and transcripts with a strict execution budget, ensuring the agent is never delayed.
- **Nightly Dream Engine**: `dream.py` runs nightly consolidation to prune noise, recalculate power-law volume decay, and synthesize fresh principles.
- **First-Party Authentication**: Extracts real OAuth tokens from Claude Desktop on macOS and Windows, bypassing broken web PKCE flows and avoiding third-party credit throttling.

---

## Architecture

Crystallized structures memory into three complementary storage layers:

| Layer | Engine | Primary Function |
|---|---|---|
| **Facts & Beliefs** | SQLite (WAL) | Instant key/value facts, bi-temporal belief states, volume decay maps. |
| **Semantic Recall** | ChromaDB | Meaning-based vector search across conversational memories. |
| **Structured Documents** | Local Filesystem | Long-form markdown notes, architecture guides, checklists, identity files. |

```
~/.config/opencode/
|-- opencode.json                    # MCP registrations and plugin definitions
`-- memory/
    |-- memory.db                    # Unified SQLite database (WAL mode)
    |-- server.py                    # MCP server exposing memory and belief tools
    |-- db.py                        # SQLite schema, bi-temporal tables, migrations
    |-- volume.py                    # Power-law memory decay implementation
    |-- observer.py                  # Fast hook for PostToolUse and Stop events
    |-- patterns.py                  # Friction and correction pattern matchers
    |-- dream.py                     # Dream consolidation and causal distillation
    |-- memory-inject.py             # Pre-prompt context retrieval hook
    |-- own-voice.py                 # Pre-prompt identity retrieval hook
    |-- chroma_db/                   # Local vector storage
    `-- notes/                       # Markdown storage
        |-- architecture/
        `-- self/
            |-- beliefs.md
            |-- focus.md
            `-- observations.md
```

---

## Memory Hierarchy

Raw experience undergoes continuous distillation. The progression turns isolated errors into generalized rules:

```
[ L3: Axiom / Principle ]      "Always run migrations before starting worker daemons"
           ^
           | (Distillation & clustering across episodes)
[ L2: Recurring Pattern ]      "Worker crashes with MissingColumnError on deployment"
           ^
           | (Session transition & root-cause extraction)
[ L1: Episode ]                "Postgres migration was skipped during hotfix deploy"
           ^
           | (Observer hook captures tool friction signal)
[ L0: Raw Observation ]        "Error: column 'session_id' does not exist in table 'jobs'"
```

1. **L0 (Raw Observation)**: Captured live by `observer.py` during `PostToolUse` and `Stop` hooks when a command fails or a correction is made.
2. **L1 (Episode)**: Grouped sequence of actions connecting a trigger, an attempted remedy, and an outcome within a session.
3. **L2 (Pattern)**: Recurring theme detected across multiple sessions indicating common pitfalls or specific project traits.
4. **L3 (Axiom)**: High-confidence decision rule stored in the bi-temporal `belief_state` table and reflected in `beliefs.md`.

---

## Quick Start

### Installation

Clone the repository and run the setup script:

```bash
# Ensure Claude Desktop is quit before running (Cmd+Q on macOS)
git clone https://github.com/enkinvsh/crystallized.git
cd crystallized
./install.sh
opencode
```

Test what will happen beforehand using the dry-run flag:

```bash
./install.sh --dry-run
```

### What install.sh Handles

1. Verifies prerequisites: `git`, `python3` (3.11+), and `curl`.
2. Installs `uv` for reproducible Python package management if missing.
3. Installs the `opencode` CLI binary if needed.
4. Copies memory engine files to `~/.config/opencode/memory/` and writes `.crystallized-manifest`.
5. Syncs dependencies into an isolated virtual environment via `uv sync --frozen`.
6. Initializes the SQLite schema (`memory.db`) with WAL mode.
7. Seeds initial identity files (`beliefs.md`, `focus.md`, `observations.md`).
8. Merges the memory MCP configuration into `~/.config/opencode/opencode.json`.
9. Registers hooks in `~/.claude/settings.json` without modifying existing third-party hooks.
10. Deploys the nightly consolidation job (`launchd` on macOS, `cron` on Linux).
11. Runs first-party token extraction on macOS.

### Installer Flags

| Flag | Description |
|---|---|
| `--dry-run` | Prints all planned filesystem actions without making changes. |
| `--no-hooks` | Skips hook registration in `~/.claude/settings.json`. |
| `--no-daemon` | Skips installing the nightly dream consolidation daemon. |
| `--offline` | Disallows network downloads, requiring local prerequisites. |
| `--help`, `-h` | Shows available options and exits. |

---

## Authentication

When using an Anthropic subscription (Claude Pro or Max) inside opencode, standard PKCE web login flows often break or trigger third-party client flags. Anthropic inspects request headers, routing unrecognized clients to a separate credit pool or returning HTTP 429 errors.

Crystallized extracts first-party OAuth tokens directly from Claude Desktop. These tokens carry genuine client credentials. When combined with the `@thehugeman/opencode-anthropic-auth-community` plugin, outgoing requests pass all client checks.

```
+---------------------+      +---------------------+      +---------------------+
|   Claude Desktop    | ---> | auth/extract_token  | ---> |   opencode auth     |
| (Encrypted Storage) |      | (Keychain / DPAPI)  |      |   (~/.local/share)  |
+---------------------+      +---------------------+      +---------------------+
```

### macOS Token Extraction
On macOS, tokens are encrypted with Electron safeStorage (AES-128-CBC) using a password in the system Keychain:

```bash
# 1. Log in to Claude.app
# 2. Quit Claude.app completely (Cmd+Q)
# 3. Run the extractor
python3 auth/extract_token.py
```

### Windows Token Extraction
On Windows, tokens are protected by a two-layer encryption scheme (DPAPI master key + AES-256-GCM payload):

```powershell
# 1. Log in to Claude Desktop for Windows
# 2. Quit Claude.exe completely
# 3. Run the extractor
python auth\extract_token.py
```

For complete platform details, troubleshooting steps, and non-interactive usage flags, consult the [Authentication Guide](docs/AUTHENTICATION.md).

---

## MCP Tools

Crystallized provides a comprehensive suite of tools for agents and developers:

### Facts & Beliefs
- `memory_save_fact(key, value, ttl_days)`: Save a key/value fact with optional expiration.
- `memory_get_fact(key)`: Retrieve a fact by exact key with automatic volume reinforcement.
- `memory_list_facts(prefix, limit, full)`: List facts matching a prefix.
- `memory_delete_fact(key)`: Remove a fact.
- `memory_belief_assert(id, subject, predicate, object_val, confidence)`: Assert a belief, atomically superseding prior active states.
- `memory_belief_get_active(subject, predicate)`: Fetch the currently active belief.
- `memory_belief_list_active(subject)`: List all active beliefs for a domain.

### Semantic Memory & Documents
- `memory_remember(text, tags)`: Save descriptive text to ChromaDB for semantic vector retrieval.
- `memory_recall(query, n_results)`: Unified search across facts, vector memories, and markdown documents.
- `memory_save_doc(folder, name, content)`: Save a structured markdown document.
- `memory_read_doc(folder, name)`: Read a markdown document.
- `memory_list_docs(folder)`: List available documents.
- `memory_delete_doc(folder, name)`: Remove a document.

### Causal & Maintenance
- `memory_causal_log(id, text, layer, cause, effect, confidence)`: Record an episodic transition.
- `memory_causal_list(layer, session_id, limit)`: Query causal memories by abstraction layer.
- `memory_memory_context(project)`: Fast overview of active beliefs, salient facts, and recent notes.
- `memory_reinforce(key, layer)`: Manually boost an item's volume score.
- `memory_sleep()`: Trigger a decay and consolidation cycle manually.

---

## Nightly Dream Consolidation

The dream engine (`dream.py`) runs at 04:00 every night. It performs three critical maintenance jobs:

1. **Power-Law Volume Decay**:
   Memory strength decays according to:
   $$V_{\text{eff}} = V_{\text{stored}} \cdot \left(1 + \frac{t_{\text{hours}}}{\tau}\right)^{-\alpha}$$
   Frequent recall boosts volume, while unreferenced noise gently fades toward the floor value of `0.01`. Nothing is ever deleted.
2. **Causal Promotion**:
   Clusters related L0 observations into L1 episodes, extracts recurring L2 patterns, and updates L3 belief state records.
3. **Identity Sync**:
   Writes the highest-confidence beliefs and focus points into `~/.config/opencode/memory/notes/self/` so pre-prompt hooks load them instantly.

---

## Uninstalling

To clean up Crystallized:

```bash
./uninstall.sh            # Prompts before removing database and notes
./uninstall.sh --keep-data # Removes code and hooks, preserves memory.db and notes
./uninstall.sh --purge     # Removes everything without prompting
```

`uninstall.sh` uses `.crystallized-manifest` to remove only installed files, ensuring any personal scripts or extra documents in the directory remain untouched.

---

## Troubleshooting

### API Returns HTTP 429
The `@thehugeman/opencode-anthropic-auth-community` plugin must be present in `opencode.json`. Verify the file contains:
```json
{
  "plugin": ["@thehugeman/opencode-anthropic-auth-community@latest"]
}
```

### Memory MCP Status is Red
Check that the SQLite database initializes cleanly:
```bash
cd ~/.config/opencode/memory
.venv/bin/python -c "import db; db.init_schema(); print('Database initialized')"
```

### Observer Hooks Not Firing
Verify hook registrations in `~/.claude/settings.json`:
```bash
python3 -c "import json, os; print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks'].keys())"
```
If missing, run `./install.sh` again to safely merge the configuration.

### Missing Token on Windows / macOS
1. Open Claude Desktop and confirm you are signed in.
2. Quit Claude Desktop completely so local files are written.
3. Re-run `python3 auth/extract_token.py` (or `python auth\extract_token.py`).

---

## License

[MIT](LICENSE)
