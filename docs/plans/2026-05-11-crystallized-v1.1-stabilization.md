# crystallized v1.1 Stabilization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship v1.1 of `crystallized` as a stabilization release: reproducible installs, documented security posture, real tests, CI, configurable runtime, uninstall path, and release-ready repo, with zero behaviour change for default users.

**Architecture:** Touch the seams, not the core. `server.py`, `memory-inject.py`, `own-voice.py`, and `auth/extract_token.py` keep their behaviour. We add env-var configuration, tests, CI, docs, and installer/uninstaller polish. Every change is small, reviewable, reversible.

**Tech Stack:** Python 3.11 to 3.13, uv, pytest, ruff, pyright (lenient), fakeredis for Redis-free tests, mock for ChromaDB, GitHub Actions for CI, bash for installer/uninstaller, mcp[cli], redis, chromadb, sentence-transformers.

**Worktree root:** `<repo-root>` (the crystallized worktree where this plan is being executed). All paths below are relative to this root unless absolute.

---

## Ground Rules (read before any task)

1. **No commits unless Oen explicitly requests one in the current session.** Every `git add` / `git commit` block in this plan is a checkpoint marker. Skip it. Atlas may collect work into one final commit only when Oen says so.
2. **No `git push`, no tags, no GitHub releases, no `gh release create`.** Tagging is Oen's call after merge.
3. **No writes to external repositories.** The `oh-my-openagent` plugin pin is researched and documented locally only.
4. **No mutation of Oen's live config.** Do NOT run `./install.sh`, do NOT run `uv sync` against `~/.config/opencode/memory/`, do NOT touch `~/.config/opencode/opencode.json`. All testing is inside the worktree or a tempdir.
5. **TDD on every code change.** Test first. Watch it fail. Implement. Watch it pass. No exceptions.
6. **Each task is one focused unit.** Stop after each task and let the orchestrator review.
7. **Final verification (Phase 5) is mandatory** before declaring the plan complete.

---

## Phase 1. Safety and Reproducibility

### Task 1.1: Discover and pin `oh-my-openagent` plugin version

**Why:** `config/opencode.json` uses `oh-my-openagent@latest`, which is a floating tag and breaks reproducibility.

**Files:**
- Modify: `config/opencode.json`
- Create: notepad entry in `.sisyphus/notepads/v1.1-stabilization/decisions.md` (append)

**Step 1: Discover the currently resolved version**

Run:

```bash
npm view oh-my-openagent version 2>&1
```

Expected: prints a semver string, e.g. `0.4.2`. Record it. If `npm view` is unavailable or the package is not on npm, run:

```bash
curl -fsSL https://registry.npmjs.org/oh-my-openagent | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('dist-tags', {}).get('latest', 'UNKNOWN'))"
```

If both fail, STOP and ask Oen which version to pin. Do not fabricate.

**Step 2: Pin in `config/opencode.json`**

Replace the line `"oh-my-openagent@latest"` with `"oh-my-openagent@<VERSION>"` where `<VERSION>` is the string from Step 1.

**Step 3: Verify JSON is still valid**

Run:

```bash
python3 -c "import json; json.load(open('config/opencode.json'))" && echo OK
```

Expected: `OK`.

**Step 4: Append decision to notepad**

Append to `.sisyphus/notepads/v1.1-stabilization/decisions.md`:

```
## Plugin pin (Task 1.1)
- Pinned `oh-my-openagent` to <VERSION> (discovered via npm registry on YYYY-MM-DD).
- Rationale: reproducibility. Upgrade procedure documented in README.
```

**Step 5: Checkpoint (do not commit)**

Note in todo: "Phase 1.1 plugin pinned at `<VERSION>`."

---

### Task 1.2: Commit `uv.lock` policy and regenerate lockfile

**Why:** `uv.lock` is in `.gitignore`, so two users on the same Python minor get different transitive deps. Committing the lock fixes that.

**Files:**
- Modify: `.gitignore`
- Create: `memory/uv.lock` (generated)
- Modify: `README.md` (small note under install/troubleshooting)

**Step 1: Write the failing assertion (manual)**

There is no unit test for this; the verification is a directory check. Confirm now:

```bash
ls memory/uv.lock 2>&1
```

Expected: `No such file or directory` (current state).

**Step 2: Remove `uv.lock` from `.gitignore`**

In `.gitignore`, delete the two lines:

```
# uv
uv.lock
```

**Step 3: Generate the lockfile locally inside the worktree only**

```bash
cd memory && uv lock
```

Expected: creates `memory/uv.lock`. If `uv` is not on PATH, STOP and tell Oen. Do not run `uv sync` (sync mutates global cache, lock does not).

**Step 4: Verify lockfile exists and parses**

```bash
ls memory/uv.lock && python3 -c "import tomllib; tomllib.load(open('memory/uv.lock','rb')); print('OK')"
```

Expected: file listed, then `OK`.

**Step 5: Add a short README note**

In `README.md`, in the install / troubleshooting section, add a subsection titled `## Reproducible installs` with two short paragraphs: that `uv.lock` is committed and pins exact transitive versions; that contributors regenerate it with `cd memory && uv lock` after dependency changes. Keep it short, no em dashes.

**Step 6: Checkpoint (do not commit)**

---

### Task 1.3: Create `SECURITY.md`

**Why:** A one-command installer that touches Keychain, OAuth tokens, and starts Redis MUST document its trust model. There is none today.

**Files:**
- Create: `SECURITY.md`

**Step 1: Write `SECURITY.md`**

Sections required, in this order:

1. `# Security Policy`
2. `## Trust Model` (one paragraph: assumes local user owns the Mac, Claude.app, the shell, and `~/.config/opencode/`)
3. `## What the installer reads` (Claude.app safeStorage via Keychain on macOS, Claude config at `~/Library/Application Support/Claude/config.json`)
4. `## What the installer writes` (`~/.config/opencode/memory/`, `~/.config/opencode/opencode.json` (with backup), `~/.local/share/opencode/auth.json`, local Redis at `localhost:6379`, ChromaDB at `~/.config/opencode/memory/chroma_db/`)
5. `## Where tokens live and how to revoke` (auth.json path; revoke by deleting it; rotating via `auth/extract_token.py` after re-login)
6. `## Network surface` (installer downloads from: Homebrew, astral.sh (uv installer), github.com (opencode releases). Memory layer is local-only after install. No telemetry.)
7. `## Reporting vulnerabilities` (open a private security advisory on GitHub or email Oen at the address in the repo metadata; do not file public issues for security)

Keep prose plain. No em dashes. Mirror the existing README voice.

**Step 2: Verify it renders as Markdown**

```bash
python3 -c "import pathlib; print(pathlib.Path('SECURITY.md').read_text()[:200])"
```

Expected: prints the first 200 characters.

**Step 3: Checkpoint (do not commit)**

---

### Task 1.4: README honest install caveats

**Files:**
- Modify: `README.md`

**Step 1: Add `## What `install.sh` does` section**

Bullet list of the 9 steps from `install.sh` (prerequisites, Redis, uv, opencode, memory server, Python deps, identity templates, opencode.json, auth). One line each, plain English.

**Step 2: Add `## What `install.sh` does NOT do` section**

Bullet list:
- Does not modify Claude.app.
- Does not change shell rc files (warns about PATH instead).
- Does not phone home.
- Does not work on Windows.
- Does not detect non-default Claude install locations beyond `/Applications/Claude.app`.

**Step 3: Add `## Caveats` section**

- Keychain may prompt for the macOS login password during auth extraction. Pick "Always Allow" to skip future prompts.
- Linux skips the automatic auth step; you must run `python3 auth/extract_token.py` manually after setup.
- The installer assumes a single-user Mac. Multi-user installs are not supported.
- ChromaDB cold start can take 10 to 30 seconds on first MCP call (model download).

**Step 4: Verify the new sections are in `README.md`**

```bash
grep -c "^## What \`install.sh\` does" README.md
```

Expected: `2` (one for "does", one for "does NOT do").

**Step 5: Checkpoint (do not commit)**

---

### Task 1.5: README.ru parallel update

**Files:**
- Modify: `README.ru.md`

**Step 1: Mirror the three new sections (Task 1.4) in Russian**

Same structure: `## Что делает install.sh`, `## Что install.sh НЕ делает`, `## Оговорки`. Translate cleanly. No em dashes (use commas, dots, or line breaks). Keep the same bullets.

**Step 2: Verify**

```bash
grep -c "^## " README.ru.md
```

Expected: count is higher than before by 3 (record the baseline first).

**Step 3: Checkpoint (do not commit)**

---

### Task 1.6: `.gitignore` allowlist comment

**Why:** Make the policy explicit so future contributors do not add generated state to the repo.

**Files:**
- Modify: `.gitignore`

**Step 1: Add a header block at the top of `.gitignore`**

Insert before line 1:

```
# Policy: this repo tracks SOURCE only. Generated state lives in
# ~/.config/opencode/memory/ on user machines, never here.
# uv.lock is the ONE exception (Task 1.2): committed for reproducibility.
```

**Step 2: Verify**

```bash
head -5 .gitignore
```

Expected: shows the new comment block.

**Step 3: Checkpoint (do not commit)**

---

## Phase 2. Tests and CI

### Task 2.1: Add dev dependencies and tooling

**Files:**
- Modify: `memory/pyproject.toml`
- Create: `memory/.ruff.toml` (or section inside pyproject)
- Create: `memory/pyrightconfig.json`

**Step 1: Add `[dependency-groups]` to `memory/pyproject.toml`**

Append:

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "fakeredis>=2.20",
    "ruff>=0.5",
    "pyright>=1.1.350",
]
```

**Step 2: Create `memory/.ruff.toml`**

```toml
line-length = 100
target-version = "py311"

[lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM"]
ignore = ["E501"]

[lint.per-file-ignores]
"tests/*" = ["B", "SIM"]
```

**Step 3: Create `memory/pyrightconfig.json`**

```json
{
  "include": ["server.py", "memory-inject.py", "own-voice.py", "speak.sh"],
  "exclude": ["**/__pycache__", "**/.venv", "tests"],
  "pythonVersion": "3.11",
  "typeCheckingMode": "basic",
  "reportMissingTypeStubs": false,
  "reportUnknownMemberType": false,
  "reportUnknownVariableType": false,
  "reportUnknownArgumentType": false
}
```

**Step 4: Regenerate lockfile**

```bash
cd memory && uv lock
```

Expected: `uv.lock` updated.

**Step 5: Verify ruff runs**

```bash
cd memory && uv run --group dev ruff check . --no-fix 2>&1 | tail -5
```

Expected: ruff runs (it MAY report existing issues; that is fine for this task; we are only checking the tool is wired up).

**Step 6: Checkpoint (do not commit)**

---

### Task 2.2: Test scaffolding and fixtures

**Files:**
- Create: `memory/tests/__init__.py` (empty)
- Create: `memory/tests/conftest.py`
- Create: `memory/tests/test_smoke.py`

**Step 1: Write the failing smoke test first**

`memory/tests/test_smoke.py`:

```python
def test_pytest_collects_smoke():
    assert True
```

**Step 2: Run it to confirm pytest is wired up**

```bash
cd memory && uv run --group dev pytest tests/test_smoke.py -v
```

Expected: `1 passed`. (This is intentionally trivial; it proves the test runner works.)

**Step 3: Write `conftest.py` with shared fixtures**

`memory/tests/conftest.py`:

```python
"""Shared fixtures for crystallized memory tests.

Goal: every test runs without a real Redis, real ChromaDB, real network,
or real ~/.config/opencode/. Tests must be hermetic.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def temp_memory_root(monkeypatch):
    """Redirect NOTES_DIR / CHROMA_DIR to a tempdir."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setenv("OPENCODE_MEMORY_ROOT", str(root))
        monkeypatch.setenv("OPENCODE_MEMORY_NOTES_DIR", str(root / "notes"))
        monkeypatch.setenv("OPENCODE_MEMORY_CHROMA_DIR", str(root / "chroma_db"))
        (root / "notes").mkdir(parents=True, exist_ok=True)
        yield root


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace redis.Redis with fakeredis."""
    import fakeredis
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("redis.Redis", lambda *a, **kw: fake)
    return fake


@pytest.fixture
def mock_chroma(monkeypatch):
    """Replace chromadb.PersistentClient with an in-memory mock."""
    collection = MagicMock()
    collection.add = MagicMock()
    collection.query = MagicMock(return_value={
        "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]
    })
    client = MagicMock()
    client.get_or_create_collection = MagicMock(return_value=collection)
    monkeypatch.setattr("chromadb.PersistentClient", lambda *a, **kw: client)
    return collection


@pytest.fixture
def mock_encoder(monkeypatch):
    """Replace SentenceTransformer with a deterministic stub."""
    enc = MagicMock()
    enc.encode = MagicMock(return_value=[[0.0] * 384])
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        lambda *a, **kw: enc,
    )
    return enc


@pytest.fixture
def memory_module(temp_memory_root, fake_redis, mock_chroma, mock_encoder, monkeypatch):
    """Import server.py with all heavy deps neutralized.

    Reloads on each call so module-level state is fresh.
    """
    import importlib
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    importlib.reload(server)
    return server
```

**Step 4: Run smoke test plus collection of conftest**

```bash
cd memory && uv run --group dev pytest tests/ -v --collect-only 2>&1 | tail -10
```

Expected: smoke test collected, no import errors from conftest.

**Step 5: Checkpoint (do not commit)**

---

### Task 2.3: TDD tests for fact tools

**Files:**
- Create: `memory/tests/test_facts.py`

**Step 1: Write failing tests for `save_fact` and `get_fact`**

`memory/tests/test_facts.py`:

```python
"""Fact layer: Redis-backed key-value with volume tracking."""

import pytest


def test_save_fact_stores_value(memory_module, fake_redis):
    memory_module.save_fact("user_name", "Oen")
    # Key format is implementation-defined; assert via the public getter.
    assert memory_module.get_fact("user_name") == "Oen"


def test_get_fact_missing_returns_none_or_empty(memory_module):
    result = memory_module.get_fact("nonexistent_key_xyz")
    # Accept either None or empty string; assert it is falsy.
    assert not result


def test_save_fact_overwrites(memory_module):
    memory_module.save_fact("color", "red")
    memory_module.save_fact("color", "blue")
    assert memory_module.get_fact("color") == "blue"


def test_save_fact_with_ttl(memory_module, fake_redis):
    # ttl_days is optional; small TTL should not crash.
    memory_module.save_fact("ephemeral", "value", ttl_days=1)
    assert memory_module.get_fact("ephemeral") == "value"


def test_list_facts_returns_saved_keys(memory_module):
    memory_module.save_fact("k1", "v1")
    memory_module.save_fact("k2", "v2")
    result = memory_module.list_facts()
    # Result should mention both keys; accept string or list.
    text = str(result)
    assert "k1" in text and "k2" in text


def test_delete_fact_removes_it(memory_module):
    memory_module.save_fact("doomed", "x")
    memory_module.delete_fact("doomed")
    assert not memory_module.get_fact("doomed")
```

**Step 2: Run them and watch them fail or pass**

```bash
cd memory && uv run --group dev pytest tests/test_facts.py -v
```

Expected: tests run. Most should pass since they characterize existing behaviour. If any fail, the failure is information: the test reveals an actual gap in `server.py`. Record the gap in `.sisyphus/notepads/v1.1-stabilization/issues.md` and adjust the test to match documented behaviour (not invented behaviour). Do NOT change `server.py` in this task.

**Step 3: Checkpoint (do not commit)**

---

### Task 2.4: TDD tests for semantic tools

**Files:**
- Create: `memory/tests/test_semantic.py`

**Step 1: Write failing tests**

```python
"""Semantic layer: ChromaDB vector search."""

import pytest


def test_remember_calls_collection_add(memory_module, mock_chroma):
    memory_module.remember("Oen prefers dense prose over flowery prose")
    assert mock_chroma.add.called


def test_remember_with_tags(memory_module, mock_chroma):
    memory_module.remember("test text", tags="style,voice")
    args, kwargs = mock_chroma.add.call_args
    metadatas = kwargs.get("metadatas") or (args[2] if len(args) > 2 else None)
    assert metadatas is not None


def test_search_memory_returns_structure(memory_module, mock_chroma):
    mock_chroma.query.return_value = {
        "ids": [["id1"]],
        "documents": [["matched text"]],
        "metadatas": [[{"tags": "style"}]],
        "distances": [[0.1]],
    }
    result = memory_module.search_memory("how does Oen want prose")
    text = str(result)
    assert "matched text" in text


def test_search_memory_empty_collection(memory_module, mock_chroma):
    result = memory_module.search_memory("nothing here")
    # Should not crash; should return something representing empty.
    assert result is not None
```

**Step 2: Run them**

```bash
cd memory && uv run --group dev pytest tests/test_semantic.py -v
```

Expected: pass (or surface a gap; same handling as Task 2.3).

**Step 3: Checkpoint (do not commit)**

---

### Task 2.5: TDD tests for doc tools

**Files:**
- Create: `memory/tests/test_docs.py`

**Step 1: Write failing tests**

```python
"""Doc layer: filesystem markdown notes."""

from pathlib import Path


def test_save_doc_writes_markdown(memory_module, temp_memory_root):
    memory_module.save_doc(
        folder="architecture", name="decision-001", content="# Title\n\nBody."
    )
    notes = Path(temp_memory_root) / "notes"
    found = list(notes.rglob("decision-001.md"))
    assert len(found) == 1
    assert "Body." in found[0].read_text()


def test_read_doc_roundtrip(memory_module):
    memory_module.save_doc("contexts", "current", "x")
    content = memory_module.read_doc("contexts", "current")
    assert "x" in str(content)


def test_list_docs_finds_saved(memory_module):
    memory_module.save_doc("notes", "a", "1")
    memory_module.save_doc("notes", "b", "2")
    result = memory_module.list_docs()
    text = str(result)
    assert "a" in text and "b" in text


def test_delete_doc_removes_file(memory_module, temp_memory_root):
    memory_module.save_doc("trash", "tmp", "content")
    memory_module.delete_doc("trash", "tmp")
    notes = Path(temp_memory_root) / "notes"
    assert list(notes.rglob("tmp.md")) == []
```

**Step 2: Run**

```bash
cd memory && uv run --group dev pytest tests/test_docs.py -v
```

Expected: pass.

**Step 3: Checkpoint (do not commit)**

---

### Task 2.6: TDD tests for decay math

**Why:** The power-law decay formula is the most error-prone piece of `server.py`. It must be a pure function.

**Files:**
- Create: `memory/tests/test_decay.py`

**Step 1: Write failing tests**

```python
"""Volume decay: V_eff = V_stored * (1 + t_hours / tau)^(-alpha)."""

import math


def test_decay_at_t_zero_returns_input(memory_module):
    # If the helper is named differently, adjust the import after first failure.
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume", None
    )
    assert fn is not None, "expected a decay helper in server.py"
    v = fn(stored=50.0, t_hours=0.0, layer="fact")
    assert math.isclose(v, 50.0, rel_tol=1e-6)


def test_decay_monotonic_decreasing(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    v1 = fn(stored=50.0, t_hours=24.0, layer="fact")
    v2 = fn(stored=50.0, t_hours=240.0, layer="fact")
    assert v2 < v1 < 50.0


def test_decay_respects_floor(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    v = fn(stored=50.0, t_hours=10_000_000.0, layer="fact")
    assert v >= 0.01  # MIN_VOLUME


def test_decay_layers_differ(memory_module):
    fn = getattr(memory_module, "_decay_volume", None) or getattr(
        memory_module, "decay_volume"
    )
    # docs decay slower than facts at the same elapsed time.
    fact_v = fn(stored=60.0, t_hours=720.0, layer="fact")
    doc_v = fn(stored=60.0, t_hours=720.0, layer="doc")
    assert doc_v > fact_v
```

**Step 2: Run**

```bash
cd memory && uv run --group dev pytest tests/test_decay.py -v
```

Expected: tests pass IF a `_decay_volume` (or `decay_volume`) helper already exists. If not, the test failure tells us the math is inlined inside `sleep()` and we need a small refactor. In that case:

**Step 3: Minimal refactor (only if Step 2 reveals the gap)**

Extract the decay formula from wherever it lives in `server.py` into a top-level pure helper named `_decay_volume(stored: float, t_hours: float, layer: str) -> float`. No behaviour change. Re-run the tests and confirm they pass.

**Step 4: Re-run full test suite**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

Expected: all pass.

**Step 5: Checkpoint (do not commit)**

---

### Task 2.7: TDD tests for `memory-inject.py` fallback path

**Why:** If the MCP socket is down, the hook MUST fall back to keyword matching and still produce an injection block. This is the highest-impact regression risk.

**Files:**
- Create: `memory/tests/test_memory_inject.py`

**Step 1: Write failing tests**

```python
"""Pre-prompt memory injection hook."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "memory-inject.py"


def test_hook_runs_without_socket(temp_memory_root, fake_redis):
    """No /tmp/opencode-memory-query.sock -> hook should still exit 0."""
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(Path(temp_memory_root) / "notes"),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": "what does Oen think of dark mode"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    # Output should contain a [Memory] block or be empty (graceful fallback).
    assert "[Memory]" in result.stdout or result.stdout.strip() == ""


def test_hook_handles_empty_prompt(temp_memory_root):
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(Path(temp_memory_root) / "notes"),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": ""}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
```

**Step 2: Run**

```bash
cd memory && uv run --group dev pytest tests/test_memory_inject.py -v
```

Expected: pass. If the hook crashes on missing socket or empty prompt, that is the bug; fix it in Task 3.3 (it depends on env-var support landing).

**Step 3: Checkpoint (do not commit)**

---

### Task 2.8: TDD tests for `own-voice.py`

**Files:**
- Create: `memory/tests/test_own_voice.py`

**Step 1: Write failing tests**

```python
"""OwnVoice hook: identity injection."""

import os
import subprocess
import sys
from pathlib import Path


MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "own-voice.py"


def test_own_voice_missing_notes_exits_clean(tmp_path):
    """No notes/self/ -> hook should exit 0 with empty or minimal output."""
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(tmp_path / "notes"),
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert result.returncode == 0


def test_own_voice_emits_block_when_beliefs_exist(tmp_path):
    notes = tmp_path / "notes" / "self"
    notes.mkdir(parents=True)
    (notes / "beliefs.md").write_text("I am Sisyphus.")
    (notes / "focus.md").write_text("Stabilize v1.1.")
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(tmp_path / "notes"),
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert result.returncode == 0
    assert "OwnVoice" in result.stdout or "Sisyphus" in result.stdout
```

**Step 2: Run**

```bash
cd memory && uv run --group dev pytest tests/test_own_voice.py -v
```

Expected: pass. If fail, note dependency on Task 3.4 (NOTES_DIR env override).

**Step 3: Checkpoint (do not commit)**

---

### Task 2.9: TDD tests for `auth/extract_token.py` error paths

**Files:**
- Create: `auth/tests/__init__.py`
- Create: `auth/tests/test_extract_token.py`
- Create: `auth/tests/fixtures/config-no-token.json` (sample Claude config WITHOUT oauth:tokenCache, hand-written, no real data)

**Step 1: Write the fixture**

`auth/tests/fixtures/config-no-token.json`:

```json
{
  "version": 1,
  "settings": {}
}
```

**Step 2: Write failing tests**

`auth/tests/test_extract_token.py`:

```python
"""extract_token.py: error paths only. No real Keychain, no real tokens."""

import json
import subprocess
import sys
from pathlib import Path


AUTH_DIR = Path(__file__).parent.parent
SCRIPT = AUTH_DIR / "extract_token.py"
FIXTURES = Path(__file__).parent / "fixtures"


def test_help_flag_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert "extract" in result.stdout.lower() or "usage" in result.stdout.lower()


def test_missing_claude_config_exits_nonzero(tmp_path, monkeypatch):
    """If Claude config does not exist, script should fail cleanly, not crash."""
    monkeypatch.setenv("HOME", str(tmp_path))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--print"],
        capture_output=True, text=True, timeout=5,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    # Error message should mention the missing file, not be a raw traceback.
    combined = (result.stdout + result.stderr).lower()
    assert "claude" in combined or "config" in combined or "not found" in combined
```

**Step 3: Run**

```bash
cd memory && uv run --group dev pytest ../auth/tests/ -v
```

Expected: `test_help_flag_exits_zero` passes. `test_missing_claude_config_exits_nonzero` may fail if the script currently raises a traceback. If it does, that is real product feedback: add a graceful error message in a separate small task and re-run.

**Step 4: Checkpoint (do not commit)**

---

### Task 2.10: Ruff lint pass (fixes only)

**Files:**
- Modify: any `.py` file ruff flags as fixable.

**Step 1: Run ruff in check mode, no fixes**

```bash
cd memory && uv run --group dev ruff check . 2>&1 | tee /tmp/ruff-before.txt | tail -20
```

Record the count of issues at the bottom of `/tmp/ruff-before.txt`.

**Step 2: Apply only safe auto-fixes**

```bash
cd memory && uv run --group dev ruff check --fix . 2>&1 | tail -10
```

**Step 3: Run ruff again, verify clean (or near-clean)**

```bash
cd memory && uv run --group dev ruff check . 2>&1 | tail -5
```

Expected: zero issues, or only `noqa`-tagged unavoidable ones. If any non-auto-fixable issue remains, fix it by hand if the fix is mechanical (rename unused var to `_`, remove dead import). If it requires behaviour change, leave it and note in `.sisyphus/notepads/v1.1-stabilization/issues.md`.

**Step 4: Run tests again to confirm no regression**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

Expected: all green.

**Step 5: Checkpoint (do not commit)**

---

### Task 2.11: Pyright pass (lenient)

**Files:**
- Modify: `memory/pyrightconfig.json` (only if widening is required)

**Step 1: Run pyright**

```bash
cd memory && uv run --group dev pyright 2>&1 | tail -20
```

**Step 2: Address blocking errors only**

The config is lenient (`basic` mode, lots of `reportUnknown* = false`). Any remaining error is a real bug or a missing stub. Fix real bugs. For missing stubs (e.g. `chromadb`, `sentence_transformers`), add `# type: ignore[import-untyped]` on the import line.

**Step 3: Verify clean**

```bash
cd memory && uv run --group dev pyright 2>&1 | grep -E "(error|warning)" | wc -l
```

Expected: `0`.

**Step 4: Checkpoint (do not commit)**

---

### Task 2.12: GitHub Actions CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

**Step 1: Write the workflow**

```yaml
name: ci

on:
  push:
    branches: ["main", "master"]
  pull_request:

jobs:
  test:
    name: test (${{ matrix.os }}, py${{ matrix.python }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12", "3.13"]

    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Set up Python ${{ matrix.python }}
        run: uv python install ${{ matrix.python }}

      - name: Install deps
        working-directory: memory
        run: uv sync --group dev --frozen

      - name: Lint (ruff)
        working-directory: memory
        run: uv run ruff check .

      - name: Type check (pyright)
        working-directory: memory
        run: uv run pyright

      - name: Run tests
        working-directory: memory
        run: uv run pytest tests/ -v

  shellcheck:
    name: shellcheck
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: ShellCheck install.sh and uninstall.sh
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y shellcheck
          shellcheck install.sh || true
          [ -f uninstall.sh ] && shellcheck uninstall.sh || true
```

Note the `|| true` on shellcheck: install.sh has historical patterns we do not want to block on. Later, after Phase 3, harden this to remove `|| true`.

**Step 2: Verify YAML parses**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))" && echo OK
```

If pyyaml is not installed locally, skip this step and rely on push-time validation (since we are not pushing in this work, the gate is just visual review).

**Step 3: Checkpoint (do not commit)**

---

## Phase 3. Product Finish

### Task 3.1: Configurable Redis in `server.py` (TDD)

**Why:** Today `server.py` hard-codes `host="localhost", port=6379`. Users on a different setup must edit the source.

**Files:**
- Modify: `memory/server.py`
- Create: `memory/tests/test_config.py`

**Step 1: Write the failing test**

`memory/tests/test_config.py`:

```python
"""Env-var configuration for runtime."""

import importlib
import sys
from pathlib import Path


def _reload_server():
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    return importlib.reload(server)


def test_redis_url_env_used(monkeypatch, fake_redis):
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6380/2")
    server = _reload_server()
    # get_redis() must construct from REDIS_URL when set.
    # Since fakeredis monkeypatches redis.Redis, the call should still succeed.
    r = server.get_redis()
    assert r is not None


def test_redis_default_unchanged(monkeypatch, fake_redis):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    server = _reload_server()
    r = server.get_redis()
    assert r is not None
```

**Step 2: Run, watch fail**

```bash
cd memory && uv run --group dev pytest tests/test_config.py::test_redis_url_env_used -v
```

Expected: fail (REDIS_URL not honored yet).

**Step 3: Implement minimal change in `server.py`**

Replace the body of `get_redis()`:

```python
def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _redis = redis.Redis.from_url(url, decode_responses=True)
        else:
            host = os.environ.get("REDIS_HOST", "localhost")
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            _redis = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        _redis.ping()
    return _redis
```

**Step 4: Run, watch pass**

```bash
cd memory && uv run --group dev pytest tests/test_config.py -v
```

Expected: all pass.

**Step 5: Run full suite**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

Expected: all green.

**Step 6: Checkpoint (do not commit)**

---

### Task 3.2: Configurable Redis in `memory-inject.py` (TDD)

**Files:**
- Modify: `memory/memory-inject.py`

**Step 1: Write a small additional test** in `memory/tests/test_config.py`:

```python
def test_inject_honors_redis_env(monkeypatch, fake_redis):
    """memory-inject.py reads REDIS_URL too."""
    import os
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6380/3")
    # Just import the module via subprocess and ensure it does not crash.
    import subprocess, sys
    from pathlib import Path
    hook = Path(__file__).parent.parent / "memory-inject.py"
    result = subprocess.run(
        [sys.executable, str(hook)],
        input='{"prompt":"hi"}',
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "OPENCODE_MEMORY_SOCKET": "/tmp/nope.sock"},
    )
    # With fake Redis unreachable in subprocess and no socket,
    # the hook must still exit cleanly.
    assert result.returncode == 0
```

**Step 2: Run, watch behavior**

```bash
cd memory && uv run --group dev pytest tests/test_config.py::test_inject_honors_redis_env -v
```

**Step 3: Implement in `memory-inject.py`**

Replace `get_redis()`:

```python
def get_redis():
    global _redis_conn
    if _redis_conn is None:
        import redis
        url = os.environ.get("REDIS_URL")
        if url:
            _redis_conn = redis.Redis.from_url(url, decode_responses=True)
        else:
            host = os.environ.get("REDIS_HOST", "localhost")
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            _redis_conn = redis.Redis(host=host, port=port, db=db, decode_responses=True)
    return _redis_conn
```

Also wrap any Redis call site that can throw with a try/except so the hook never crashes when Redis is unreachable. The hook is best-effort.

**Step 4: Verify**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

**Step 5: Checkpoint (do not commit)**

---

### Task 3.3: Configurable socket path (TDD)

**Files:**
- Modify: `memory/server.py`
- Modify: `memory/memory-inject.py`

**Step 1: Write the failing test** in `tests/test_config.py`:

```python
def test_socket_path_env_override(monkeypatch):
    """OPENCODE_MEMORY_SOCKET overrides the default /tmp path."""
    monkeypatch.setenv("OPENCODE_MEMORY_SOCKET", "/tmp/custom-test.sock")
    import importlib, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    importlib.reload(server)
    assert str(server.QUERY_SOCKET) == "/tmp/custom-test.sock"
```

**Step 2: Run, watch fail**

```bash
cd memory && uv run --group dev pytest tests/test_config.py::test_socket_path_env_override -v
```

**Step 3: Implement**

In `memory/server.py`:

```python
QUERY_SOCKET = Path(
    os.environ.get("OPENCODE_MEMORY_SOCKET", "/tmp/opencode-memory-query.sock")
)
```

In `memory/memory-inject.py`:

```python
QUERY_SOCKET = os.environ.get(
    "OPENCODE_MEMORY_SOCKET", "/tmp/opencode-memory-query.sock"
)
```

**Step 4: Verify**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

**Step 5: Checkpoint (do not commit)**

---

### Task 3.4: Configurable NOTES_DIR / CHROMA_DIR (TDD)

**Files:**
- Modify: `memory/server.py`
- Modify: `memory/memory-inject.py`
- Modify: `memory/own-voice.py`

**Step 1: Write the failing test** in `tests/test_config.py`:

```python
def test_notes_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "alt-notes"
    monkeypatch.setenv("OPENCODE_MEMORY_NOTES_DIR", str(custom))
    import importlib, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    importlib.reload(server)
    assert str(server.NOTES_DIR) == str(custom)


def test_chroma_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "alt-chroma"
    monkeypatch.setenv("OPENCODE_MEMORY_CHROMA_DIR", str(custom))
    import importlib, sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    importlib.reload(server)
    assert str(server.CHROMA_DIR) == str(custom)
```

**Step 2: Run, watch fail**

```bash
cd memory && uv run --group dev pytest tests/test_config.py -v
```

**Step 3: Implement in `server.py`**

Replace the three module-level constants near line 36 to 38:

```python
NOTES_DIR = Path(
    os.environ.get(
        "OPENCODE_MEMORY_NOTES_DIR",
        str(Path.home() / ".config" / "opencode" / "memory" / "notes"),
    )
)
CHROMA_DIR = Path(
    os.environ.get(
        "OPENCODE_MEMORY_CHROMA_DIR",
        str(Path.home() / ".config" / "opencode" / "memory" / "chroma_db"),
    )
)
```

Apply the same override pattern in `memory-inject.py` (lines 32 to 33) and `own-voice.py` (lines 18 to 20: `NOTES_DIR`, `SELF_DIR`, `JOURNAL_DIR`). `SELF_DIR` and `JOURNAL_DIR` derive from `NOTES_DIR`, so just deriving them from the new `NOTES_DIR` is enough.

**Step 4: Verify**

```bash
cd memory && uv run --group dev pytest tests/ -v
```

Expected: all green.

**Step 5: Checkpoint (do not commit)**

---

### Task 3.5: Seed `templates/notes/journal/`

**Why:** `own-voice.py` reads from `JOURNAL_DIR`. There is no journal template, so the agent has no scaffold to follow.

**Files:**
- Create: `templates/notes/journal/README.md`
- Create: `templates/notes/journal/_template.md`

**Step 1: Write `templates/notes/journal/README.md`**

Short, plain prose: what the journal is for (one entry per session, dated `YYYY-MM-DD-HHMM.md`), what goes in it (open questions, decisions, surprises, gratitude), what does NOT go in it (secrets, raw transcripts).

**Step 2: Write `templates/notes/journal/_template.md`**

```markdown
# YYYY-MM-DD HH:MM

## Context
What was happening.

## Decision / Discovery
What changed in my understanding.

## Open Question
What I do not yet know.

## Mood
One line.
```

**Step 3: Update installer to copy journal templates too**

Look at `install.sh`, function `copy_templates()` (lines around 441). It already iterates `templates/notes/`. Confirm by reading: `find templates/notes -type f -print0` should now include the journal files. Run:

```bash
find templates/notes -type f
```

Expected output lists `self/beliefs.md`, `self/focus.md`, `self/observations.md`, `journal/README.md`, `journal/_template.md`. No change needed in `install.sh` if the existing `find` already covers them. If a path filter excludes journal, fix it (it should not).

**Step 4: Checkpoint (do not commit)**

---

### Task 3.6: Installer backup-before-merge for `opencode.json`

**Why:** `install.sh` merges into an existing `~/.config/opencode/opencode.json` without a backup. If the merge logic misbehaves, the user loses their config.

**Files:**
- Modify: `install.sh`

**Step 1: Write a small bash assertion harness** (no formal unit test; bash dry-run only)

Inspect the current behaviour:

```bash
bash -n install.sh && echo "syntax OK"
```

Expected: `syntax OK`.

**Step 2: Edit `generate_config()` in `install.sh`**

Inside `generate_config()`, just before the `# Config already exists — attempt to merge MCP section` block (around line 506 in the current file), insert:

```bash
  # Backup existing config before any merge.
  local ts backup
  ts="$(date +%Y%m%d-%H%M%S)"
  backup="${dest}.bak.${ts}"
  cp "$dest" "$backup"
  info "Backup of existing config written to: $backup"
```

**Step 3: Verify syntax**

```bash
bash -n install.sh && echo OK
```

Expected: `OK`.

**Step 4: Sanity check by reading help**

```bash
./install.sh --help 2>&1 | head -20
```

Expected: prints usage. Do NOT run without `--help`.

**Step 5: Checkpoint (do not commit)**

---

### Task 3.7: Installer Redis ownership detection

**Why:** If the user already runs Redis (maybe for another app), we should not start a duplicate. We should detect, warn, and continue.

**Files:**
- Modify: `install.sh`

**Step 1: Inspect current `start_redis()`** (lines around 197 to 224).

It already short-circuits if `redis-cli ping` succeeds (`success "Redis is running"; return`). That is exactly the right behaviour, BUT it does not say whose Redis it is. Improve the message.

**Step 2: Edit `start_redis()`**

Replace:

```bash
  if redis-cli ping &>/dev/null 2>&1; then
    success "Redis is running"
    return
  fi
```

With:

```bash
  if redis-cli ping &>/dev/null 2>&1; then
    success "Redis is already running on localhost:6379"
    info "  Memory will share this Redis instance. If that is not desired,"
    info "  set REDIS_URL before starting opencode."
    return
  fi
```

**Step 3: Verify syntax**

```bash
bash -n install.sh && echo OK
```

**Step 4: Checkpoint (do not commit)**

---

### Task 3.8: Create `uninstall.sh`

**Why:** No path off the product. Users who try it should be able to remove it cleanly.

**Files:**
- Create: `uninstall.sh`

**Step 1: Write `uninstall.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# crystallized — uninstall / rollback
# Idempotent. Asks before deleting user data.
# ─────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
success() { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}──── $* ────${RESET}"; }

usage() {
  cat <<EOF
${BOLD}crystallized uninstall.sh${RESET}

Removes the memory MCP server and (optionally) user memory data.
Restores backed-up opencode.json if one is found.

${BOLD}USAGE${RESET}
  ./uninstall.sh [OPTIONS]

${BOLD}OPTIONS${RESET}
  --help, -h    Show this message and exit
  --keep-data   Do not prompt; preserve all user data
  --purge       Do not prompt; remove ALL data (memory, chroma_db, notes)

${BOLD}WHAT THIS REMOVES${RESET}
  - ~/.config/opencode/memory/server.py and friends (script files)
  - The MCP entry from ~/.config/opencode/opencode.json (if present)
  - Optionally: ~/.config/opencode/memory/{chroma_db,notes,vault,identity.json}

${BOLD}WHAT THIS DOES NOT TOUCH${RESET}
  - Redis (the service stays installed and running)
  - opencode CLI binary
  - ~/.local/share/opencode/auth.json
  - Claude.app

EOF
  exit 0
}

KEEP_DATA="ask"
for arg in "$@"; do
  case "$arg" in
    --help|-h)    usage ;;
    --keep-data)  KEEP_DATA="keep" ;;
    --purge)      KEEP_DATA="purge" ;;
    *)            warn "Unknown option: $arg" ;;
  esac
done

MEMORY_DIR="$HOME/.config/opencode/memory"
CONFIG_FILE="$HOME/.config/opencode/opencode.json"

step "1. Remove memory server scripts"

if [[ -d "$MEMORY_DIR" ]]; then
  for f in server.py memory-inject.py own-voice.py speak.sh pyproject.toml uv.lock .python-version README.md; do
    if [[ -e "$MEMORY_DIR/$f" ]]; then
      rm -f "$MEMORY_DIR/$f"
      info "  removed: $f"
    fi
  done
  # The .venv is generated; remove it.
  if [[ -d "$MEMORY_DIR/.venv" ]]; then
    rm -rf "$MEMORY_DIR/.venv"
    info "  removed: .venv/"
  fi
  success "Server scripts removed"
else
  info "No memory directory at $MEMORY_DIR. Nothing to remove."
fi

step "2. User data (notes, chroma_db, vault, identity)"

DATA_PATHS=(
  "$MEMORY_DIR/notes"
  "$MEMORY_DIR/chroma_db"
  "$MEMORY_DIR/vault"
  "$MEMORY_DIR/identity.json"
)

has_any_data=0
for p in "${DATA_PATHS[@]}"; do
  if [[ -e "$p" ]]; then has_any_data=1; fi
done

if [[ "$has_any_data" -eq 0 ]]; then
  info "No user data found."
else
  case "$KEEP_DATA" in
    keep)
      info "Keeping all user data (--keep-data)"
      ;;
    purge)
      for p in "${DATA_PATHS[@]}"; do
        if [[ -e "$p" ]]; then
          rm -rf "$p"
          info "  removed: $p"
        fi
      done
      success "User data purged"
      ;;
    ask|*)
      warn "About to remove user memory data:"
      for p in "${DATA_PATHS[@]}"; do
        [[ -e "$p" ]] && echo "    $p"
      done
      read -r -p "Delete this data? [y/N] " ans
      ans="${ans:-N}"
      if [[ "$ans" =~ ^[Yy]$ ]]; then
        for p in "${DATA_PATHS[@]}"; do
          if [[ -e "$p" ]]; then
            rm -rf "$p"
            info "  removed: $p"
          fi
        done
        success "User data removed"
      else
        info "Keeping user data."
      fi
      ;;
  esac
fi

step "3. opencode.json: remove memory MCP entry"

if [[ -f "$CONFIG_FILE" ]]; then
  python3 - "$CONFIG_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
mcp = cfg.get("mcp", {})
if "memory" in mcp:
    del mcp["memory"]
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2); f.write("\n")
    print("[info]  memory MCP entry removed from opencode.json")
else:
    print("[info]  no memory MCP entry to remove")
PY
else
  info "No opencode.json at $CONFIG_FILE. Skipping."
fi

step "4. Restore most recent backup (if present)"

shopt -s nullglob
backups=("${CONFIG_FILE}".bak.*)
shopt -u nullglob

if [[ "${#backups[@]}" -gt 0 ]]; then
  latest=""
  for b in "${backups[@]}"; do
    if [[ -z "$latest" || "$b" > "$latest" ]]; then latest="$b"; fi
  done
  info "Found backup: $latest"
  read -r -p "Restore this backup over the current opencode.json? [y/N] " ans
  ans="${ans:-N}"
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    cp "$latest" "$CONFIG_FILE"
    success "Restored from $latest"
  else
    info "Backup left in place; current config unchanged."
  fi
else
  info "No backups found."
fi

echo ""
success "uninstall complete"
echo -e "${YELLOW}Note:${RESET} Redis service was NOT stopped (it may be used by other apps)."
echo -e "${YELLOW}Note:${RESET} opencode CLI and auth.json were NOT touched."
echo ""
```

**Step 2: Make it executable**

```bash
chmod +x uninstall.sh
```

**Step 3: Verify bash syntax**

```bash
bash -n uninstall.sh && echo OK
```

Expected: `OK`.

**Step 4: Verify help renders**

```bash
./uninstall.sh --help 2>&1 | head -20
```

Expected: usage text.

**Step 5: Do NOT run without `--help`.** This script touches `~/.config/opencode/`.

**Step 6: Checkpoint (do not commit)**

---

### Task 3.9: Better Keychain error in `install.sh`

**Files:**
- Modify: `install.sh`

**Step 1: Inspect current `authenticate()`** (lines around 617).

The current warning is `"Keychain unlock failed — will attempt extraction anyway"` (and uses an em dash, which violates Oen's standing rule). Fix the message.

**Step 2: Replace the warning**

Find:

```bash
  security unlock-keychain -p "$password" ~/Library/Keychains/login.keychain-db 2>/dev/null || {
    warn "Keychain unlock failed — will attempt extraction anyway"
  }
```

Replace with:

```bash
  security unlock-keychain -p "$password" ~/Library/Keychains/login.keychain-db 2>/dev/null || {
    warn "Keychain unlock failed. This usually means:"
    warn "  - the password is wrong, or"
    warn "  - the login keychain is in a different location."
    warn "The extractor will try anyway. If it fails, run it manually:"
    warn "  python3 $REPO_DIR/auth/extract_token.py"
  }
```

**Step 3: Sweep `install.sh` for any other em dashes / en dashes and replace them** with commas, periods, or "to":

```bash
grep -nE "—|–" install.sh || echo "no em/en dashes"
```

Expected after fixing: `no em/en dashes`.

**Step 4: Verify**

```bash
bash -n install.sh && echo OK
./install.sh --help 2>&1 | head -5
```

**Step 5: Checkpoint (do not commit)**

---

## Phase 4. Release Readiness

### Task 4.1: Seed `CHANGELOG.md`

**Files:**
- Create: `CHANGELOG.md`

**Step 1: Write `CHANGELOG.md`**

Format: Keep a Changelog 1.1.0, semver. Two entries: v1.0.0 (the existing public release, summarized retroactively) and v1.1.0 (this stabilization). No dates for v1.1.0 yet (Oen will tag).

Header block:

```markdown
# Changelog

All notable user-visible changes to crystallized are recorded here.
Format: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning 2.0.0](https://semver.org/).
Tag scheme: `vMAJOR.MINOR.PATCH`. Tags are created by the maintainer on merge.
```

v1.1.0 section bullets, mapped to actual changes (group as Added / Changed / Fixed):

- **Added:** `SECURITY.md`. `uv.lock` committed. `uninstall.sh`. `CHANGELOG.md`. Journal template. README install caveats sections. `tests/` directory with pytest suite. GitHub Actions CI. Env vars: `REDIS_URL`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `OPENCODE_MEMORY_SOCKET`, `OPENCODE_MEMORY_NOTES_DIR`, `OPENCODE_MEMORY_CHROMA_DIR`.
- **Changed:** Pinned `oh-my-openagent` plugin to a specific version. Installer backs up existing `opencode.json` before merging. Installer detects existing Redis and shares it instead of starting a duplicate. Installer keychain error message is now actionable.
- **Fixed:** Em dashes removed from installer prose.

v1.0.0 section: one paragraph noting it was the initial public release, plus a short bullet list of what shipped (three-layer memory MCP, OwnVoice hook, Claude.app token extraction, one-command installer).

**Step 2: Verify**

```bash
head -10 CHANGELOG.md
```

**Step 3: Checkpoint (do not commit)**

---

### Task 4.2: Bump version

**Files:**
- Modify: `memory/pyproject.toml`

**Step 1: Change version**

In `memory/pyproject.toml` line 3, replace `version = "1.0.0"` with `version = "1.1.0"`.

**Step 2: Regenerate lockfile** (version is part of the resolved manifest)

```bash
cd memory && uv lock
```

**Step 3: Verify**

```bash
grep '^version' memory/pyproject.toml
```

Expected: `version = "1.1.0"`.

**Step 4: Checkpoint (do not commit)**

---

### Task 4.3: README upgrade-from-v1.0 section

**Files:**
- Modify: `README.md`
- Modify: `README.ru.md`

**Step 1: Add `## Upgrading from v1.0` to `README.md`**

Three short paragraphs:
1. Pull the new code in the repo. Re-run `./install.sh`. The installer backs up your `opencode.json` before merging.
2. Note env vars are new and all optional. Defaults preserve v1.0 behaviour.
3. If something breaks, run `./uninstall.sh` and re-run `./install.sh`.

Add a "See [CHANGELOG.md](CHANGELOG.md) for full release notes." link at the bottom.

**Step 2: Mirror in `README.ru.md`** as `## Обновление с v1.0`.

**Step 3: Verify**

```bash
grep -c "Upgrading from v1.0" README.md
grep -c "Обновление с v1.0" README.ru.md
```

Both expected: `1`.

**Step 4: Checkpoint (do not commit)**

---

## Phase 5. Final Verification

This phase is mandatory. Do not skip. Do not abbreviate. Each step's output is the evidence required to declare the plan complete.

### Task 5.1: Python compile check on every `.py` file

```bash
find . -name "*.py" -not -path "./memory/.venv/*" -not -path "*/__pycache__/*" -print0 \
  | xargs -0 python3 -m py_compile && echo "ALL PY OK"
```

Expected: `ALL PY OK`. Any syntax error = fix and re-run.

### Task 5.2: Full test suite

```bash
cd memory && uv run --group dev pytest tests/ ../auth/tests/ -v
```

Expected: all tests pass. Record the count.

### Task 5.3: Lint clean

```bash
cd memory && uv run --group dev ruff check .
```

Expected: zero issues.

### Task 5.4: Type check clean

```bash
cd memory && uv run --group dev pyright
```

Expected: zero errors.

### Task 5.5: Bash syntax for shell scripts

```bash
bash -n install.sh && bash -n uninstall.sh && bash -n memory/speak.sh && echo "BASH OK"
```

Expected: `BASH OK`.

### Task 5.6: shellcheck (best-effort)

```bash
command -v shellcheck >/dev/null && shellcheck install.sh uninstall.sh memory/speak.sh || echo "shellcheck not installed, skipping"
```

If shellcheck is installed and reports issues, fix the easy ones (quoting, `[[ ]]` vs `[ ]`). If it reports issues we cannot fix without behaviour change, log them in `.sisyphus/notepads/v1.1-stabilization/problems.md` for follow-up.

### Task 5.7: Help screens render

```bash
./install.sh --help >/dev/null && ./uninstall.sh --help >/dev/null && echo "HELP OK"
```

Expected: `HELP OK`.

### Task 5.8: `opencode.json` template still parses

```bash
python3 -c "import json; json.load(open('config/opencode.json'))" && echo "JSON OK"
```

Expected: `JSON OK`. And: the plugin entry no longer ends with `@latest`.

### Task 5.9: No em dashes / en dashes in changed prose

```bash
grep -rnE "—|–" README.md README.ru.md SECURITY.md CHANGELOG.md install.sh uninstall.sh 2>/dev/null \
  | grep -v "\.bak\." || echo "PROSE CLEAN"
```

Expected: `PROSE CLEAN`. (Existing source code with em dashes inside docstrings stays; the rule is for new prose.)

### Task 5.10: Git diff scope check

```bash
git status --short
```

Expected: changed/added files are confined to:
- `.gitignore`
- `.github/workflows/ci.yml`
- `CHANGELOG.md`
- `SECURITY.md`
- `README.md`
- `README.ru.md`
- `config/opencode.json`
- `install.sh`
- `uninstall.sh`
- `memory/pyproject.toml`
- `memory/uv.lock`
- `memory/.ruff.toml`
- `memory/pyrightconfig.json`
- `memory/server.py`
- `memory/memory-inject.py`
- `memory/own-voice.py`
- `memory/tests/**`
- `auth/extract_token.py` (if Task 2.9 surfaced a real bug worth fixing)
- `auth/tests/**`
- `templates/notes/journal/**`
- `docs/plans/2026-05-11-crystallized-v1.1-stabilization*.md`

Anything outside this list = scope creep. Investigate before declaring complete.

### Task 5.11: Acceptance criteria checklist (from design doc Section 14)

Re-read the design doc and tick each criterion against actual repo state:

- [ ] `uv.lock` committed
- [ ] Plugin pinned (not `@latest`)
- [ ] `SECURITY.md` present
- [ ] README install/caveats sections present
- [ ] `pytest` runs locally; CI workflow exists for Linux + macOS x Py 3.11/3.12/3.13
- [ ] `ruff` and `pyright` pass
- [ ] Installer backs up `opencode.json` before merging
- [ ] `uninstall.sh` exists, idempotent, asks before destructive ops
- [ ] Redis and ChromaDB locations configurable; defaults unchanged
- [ ] `CHANGELOG.md` describes v1.1 changes
- [ ] `memory/pyproject.toml` version is `1.1.0`
- [ ] No commits to external repos. No remote writes.

If every box is checked AND every step above produces the expected output, the plan is complete. Stop. Hand back to Atlas for review.

---

## Handoff

After Atlas reviews this plan, the recommended execution path is:

**Option 1 (recommended): Subagent-driven** in Atlas's session. Atlas dispatches one Sisyphus-Junior per task, reviews diff between tasks, and stops at every checkpoint. Per Oen's standing rule, no commits are created until Oen explicitly asks.

**Option 2: Parallel session.** Open a fresh opencode session in this worktree with REQUIRED SUB-SKILL `superpowers:executing-plans`. Run task-by-task. Use the verification phase as the final gate.

Either way: the plan file is sacred. Atlas updates the checkbox state in `.sisyphus/plans/`, never here.
