"""Shared fixtures for crystallized memory tests.

Goal: every test runs without a real Redis, real ChromaDB, real network,
or real ~/.config/opencode/. Tests must be hermetic.
"""

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

    class _StubRedis:
        def __new__(cls, *args, **kwargs):
            return fake

    monkeypatch.setattr("redis.Redis", _StubRedis)
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

    class _StubPersistentClient:
        def __new__(cls, *args, **kwargs):
            return client

    monkeypatch.setattr("chromadb.PersistentClient", _StubPersistentClient)
    return collection


@pytest.fixture
def mock_encoder(monkeypatch):
    """Replace SentenceTransformer with a deterministic stub."""
    enc = MagicMock()
    enc.encode = MagicMock(return_value=[[0.0] * 384])

    class _StubSentenceTransformer:
        def __new__(cls, *args, **kwargs):
            return enc

    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        _StubSentenceTransformer,
    )
    return enc


@pytest.fixture
def memory_module(temp_memory_root, fake_redis, mock_chroma, mock_encoder, monkeypatch):
    """Import server.py with all heavy deps neutralized.

    Reloads on each call so module-level state is fresh.
    """
    import importlib
    server_dir = str(Path(__file__).parent.parent)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server
    importlib.reload(server)
    return server
