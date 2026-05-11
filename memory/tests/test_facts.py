"""Fact layer: Redis-backed key-value with volume tracking.

Tests characterize the actual public surface in server.py at the time of
writing: save_fact / list_facts / delete_fact. The plan also referenced
a get_fact tool and a ttl_days parameter; neither exists in server.py.
Those gaps are recorded in .sisyphus/notepads/v1.1-stabilization/issues.md.

A local fixture (facts_server) is used because the shared conftest
fake_redis / mock_encoder fixtures patch redis.Redis / SentenceTransformer
with bare lambdas, which collides with the `redis.Redis | None` and
`SentenceTransformer | None` annotations evaluated when server.py is
(re)imported. That fixture defect is also recorded in issues.md per the
"stop and record the defect" instruction; conftest.py is not modified here.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis
import pytest


@pytest.fixture
def facts_server(monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    if "server" in sys.modules:
        del sys.modules["server"]
    import chromadb
    import sentence_transformers
    chroma_collection = MagicMock()
    chroma_client = MagicMock()
    chroma_client.get_or_create_collection = MagicMock(return_value=chroma_collection)

    class _StubChromaClient:
        def __new__(cls, *a, **kw):
            return chroma_client

    monkeypatch.setattr(chromadb, "PersistentClient", _StubChromaClient)

    enc = MagicMock()
    enc.encode = MagicMock(return_value=[[0.0] * 384])

    class _StubEncoder:
        def __new__(cls, *a, **kw):
            return enc

    monkeypatch.setattr(
        sentence_transformers, "SentenceTransformer", _StubEncoder
    )
    import server
    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(server, "get_redis", lambda: fake)
    return server, fake


def _stored_fact(fake, server, key):
    raw = fake.hget(f"{server.REDIS_PREFIX}:facts", key)
    if raw is None:
        return None
    return json.loads(raw)["value"]


def test_save_fact_stores_value(facts_server):
    server, fake = facts_server
    result = server.save_fact("user_name", "Oen")
    assert _stored_fact(fake, server, "user_name") == "Oen"
    assert "user_name" in result and "Oen" in result


def test_save_fact_missing_key_is_falsy(facts_server):
    server, fake = facts_server
    assert _stored_fact(fake, server, "nonexistent_key_xyz") is None


def test_save_fact_overwrites(facts_server):
    server, fake = facts_server
    server.save_fact("color", "red")
    server.save_fact("color", "blue")
    assert _stored_fact(fake, server, "color") == "blue"


def test_save_fact_signature_has_no_ttl_kwarg(facts_server):
    server, _ = facts_server
    import inspect
    params = inspect.signature(server.save_fact).parameters
    assert set(params) == {"key", "value"}


def test_list_facts_returns_saved_keys(facts_server):
    server, _ = facts_server
    server.save_fact("k1", "v1")
    server.save_fact("k2", "v2")
    text = str(server.list_facts())
    assert "k1" in text and "k2" in text
    assert "v1" in text and "v2" in text


def test_list_facts_empty(facts_server):
    server, _ = facts_server
    text = str(server.list_facts())
    assert "No facts" in text


def test_delete_fact_removes_it(facts_server):
    server, fake = facts_server
    server.save_fact("doomed", "x")
    assert _stored_fact(fake, server, "doomed") == "x"
    result = server.delete_fact("doomed")
    assert _stored_fact(fake, server, "doomed") is None
    assert "doomed" in result


def test_delete_fact_missing_key_is_noop(facts_server):
    server, _ = facts_server
    result = server.delete_fact("never_existed")
    assert "No fact" in result or "not found" in result.lower()


def test_no_get_fact_tool_exists(facts_server):
    server, _ = facts_server
    assert not hasattr(server, "get_fact")
