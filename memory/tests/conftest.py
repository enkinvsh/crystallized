"""Shared fixtures for crystallized memory tests (v2.0).

Hermeticity contract — every test in this suite MUST run:
  * without Redis (there is no Redis anywhere in this project anymore),
  * without network access (no model downloads, no telemetry),
  * without reading or writing ANY path under the real
    ``~/.config/opencode/memory``.

The last point is enforced structurally rather than by convention: this module
rewrites the ``OPENCODE_MEMORY_*`` environment variables to a process-wide
temporary root at IMPORT TIME, i.e. before pytest imports any test module and
therefore before ``db``/``server`` are ever imported. Those modules
resolve their paths at import time, so patching later would be too late.

``OPENCODE_MEMORY_DISABLE_CHROMA_API`` is forced to "1" so the semantic layer
uses the SQLite fallback table and never constructs a ChromaDB client or a
SentenceTransformer encoder (which would try to download a model).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import-time isolation. Runs before any test module is imported.
# ---------------------------------------------------------------------------

MEMORY_DIR = Path(__file__).resolve().parent.parent
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="crystallized-tests-"))
atexit.register(shutil.rmtree, _SESSION_ROOT, True)

os.environ["OPENCODE_MEMORY_DB"] = str(_SESSION_ROOT / "memory.db")
os.environ["OPENCODE_MEMORY_NOTES_DIR"] = str(_SESSION_ROOT / "notes")
os.environ["OPENCODE_MEMORY_CHROMA_DIR"] = str(_SESSION_ROOT / "chroma_db")
os.environ["OPENCODE_MEMORY_IDENTITY"] = str(_SESSION_ROOT / "identity.json")
os.environ["OPENCODE_MEMORY_SOCKET"] = str(_SESSION_ROOT / "query.sock")
os.environ["OPENCODE_MEMORY_DISABLE_CHROMA_API"] = "1"
os.environ["ANONYMIZED_TELEMETRY"] = "False"

# The observer reads the agent's own turns from opencode's store, which lives
# outside this project entirely. Point it at a path inside the session root that
# will never exist, so no test can touch the live 23 GB database: the same
# structural isolation the OPENCODE_MEMORY_* variables above provide, extended
# to the one foreign path this suite can reach.
os.environ["CRYSTALLIZED_AGENT_STORE"] = str(_SESSION_ROOT / "absent-agent-store.db")

(_SESSION_ROOT / "notes").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def session_root() -> Path:
    """The temporary root every OPENCODE_MEMORY_* path points into."""
    return _SESSION_ROOT


# ---------------------------------------------------------------------------
# Per-test database isolation
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """A freshly migrated, empty database, isolated to a single test.

    Repoints the module-global connection at ``tmp_path/memory.db`` via
    ``db.set_db_path`` and restores the previous path on teardown, so tests
    never observe each other's rows.
    """
    import db

    previous = db.DB_PATH
    db.set_db_path(tmp_path / "memory.db")
    try:
        yield db
    finally:
        db.close_db()
        db.set_db_path(previous)
        db.close_db()


@pytest.fixture
def srv(store, tmp_path, monkeypatch):
    """Import ``server`` with every layer redirected into ``tmp_path``.

    ``server`` is imported once and then re-pointed per test (rather than
    reloaded) because reloading it would re-import chromadb and
    sentence_transformers on every single test, which is slow and pointless:
    all mutable state that matters lives in ``db`` and in the module-level
    path constants patched here.
    """
    import server

    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(server, "NOTES_DIR", notes)
    monkeypatch.setattr(server, "CHROMA_DIR", tmp_path / "chroma_db")
    monkeypatch.setattr(server, "IDENTITY_PATH", tmp_path / "identity.json")
    monkeypatch.setattr(server, "QUERY_SOCKET", tmp_path / "query.sock")
    monkeypatch.setenv("OPENCODE_MEMORY_DISABLE_CHROMA_API", "1")

    server._invalidate_fact_embeddings()
    return server


@pytest.fixture
def hook_env(tmp_path):
    """Environment for subprocess hook tests: fully redirected, no live paths."""
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENCODE_MEMORY_DB": str(tmp_path / "memory.db"),
        "OPENCODE_MEMORY_NOTES_DIR": str(tmp_path / "notes"),
        "OPENCODE_MEMORY_CHROMA_DIR": str(tmp_path / "chroma_db"),
        "OPENCODE_MEMORY_SOCKET": str(tmp_path / "absent.sock"),
    }
