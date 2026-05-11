"""Semantic layer: ChromaDB + sentence-transformers (volume-aware).

Tests characterize the actual public surface in server.py at the time of
writing: remember(text, tags="") and search_memory(query, n_results=5).

Behavior gaps versus the plan (recorded in
.sisyphus/notepads/v1.1-stabilization/issues.md):

- Plan said "remember calls collection.add". The actual server.py uses
  collection.upsert. Tests characterize the real API; production not
  modified per Task 2.4 scope.

Hermeticity: no real Redis, no real ChromaDB, no model download, no
network, no ~/.config/opencode writes. The shared conftest fixtures
(memory_module, mock_chroma, mock_encoder, temp_memory_root, fake_redis)
provide the sandbox; conftest already sets `OPENCODE_MEMORY_CHROMA_DIR`
to redirect server.py's module-level CHROMA_DIR (Task 3.4), and each
test additionally monkeypatches `memory_module.CHROMA_DIR` as belt-and-
suspenders local isolation since the module is already imported by the
time the test runs. The mock_encoder fixture's encode() returns a bare
list, but production calls .tolist() on the result, so each test rewires
encode() to yield an object that supports .tolist().
"""

import re
import time as _time
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def semantic_server(memory_module, mock_chroma, mock_encoder, temp_memory_root, monkeypatch):
    """memory_module with CHROMA_DIR redirected and encoder.encode().tolist() wired."""
    monkeypatch.setattr(memory_module, "CHROMA_DIR", temp_memory_root / "chroma_db")

    def _encode(_text):
        vec = MagicMock()
        vec.tolist.return_value = [0.1] * 384
        return vec

    mock_encoder.encode = MagicMock(side_effect=_encode)
    return memory_module


# ---------------------------------------------------------------------------
# remember()
# ---------------------------------------------------------------------------


def test_remember_calls_collection_upsert(semantic_server, mock_chroma):
    """remember stores via collection.upsert (plan said .add — gap in issues.md)."""
    semantic_server.remember("hello world")
    assert mock_chroma.upsert.called


def test_remember_does_not_call_collection_add(semantic_server, mock_chroma):
    """Locks the upsert-not-add behavior so future drift is caught."""
    semantic_server.remember("hello world")
    assert not mock_chroma.add.called


def test_remember_stores_text_as_document(semantic_server, mock_chroma):
    semantic_server.remember("important architecture note")
    kwargs = mock_chroma.upsert.call_args.kwargs
    assert kwargs["documents"] == ["important architecture note"]


def test_remember_default_tags_are_empty_string(semantic_server, mock_chroma):
    semantic_server.remember("note without tags")
    metadata = mock_chroma.upsert.call_args.kwargs["metadatas"][0]
    assert metadata["tags"] == ""


def test_remember_passes_tags_into_metadata(semantic_server, mock_chroma):
    semantic_server.remember("note", tags="architecture,database")
    metadata = mock_chroma.upsert.call_args.kwargs["metadatas"][0]
    assert metadata["tags"] == "architecture,database"


def test_remember_metadata_has_timestamp_date_and_reinforced_at(
    semantic_server, mock_chroma
):
    semantic_server.remember("note")
    metadata = mock_chroma.upsert.call_args.kwargs["metadatas"][0]
    assert "timestamp" in metadata
    assert "date" in metadata
    assert "last_reinforced_at" in metadata


def test_remember_uses_encoder_for_embedding(semantic_server, mock_chroma, mock_encoder):
    semantic_server.remember("encode me")
    mock_encoder.encode.assert_called_once_with("encode me")
    embeddings = mock_chroma.upsert.call_args.kwargs["embeddings"]
    assert len(embeddings) == 1
    assert len(embeddings[0]) == 384


def test_remember_assigns_deterministic_id(semantic_server, mock_chroma):
    """doc_id is md5(text)[:16] — same text → same id, different text → different id."""
    semantic_server.remember("alpha")
    id_alpha = mock_chroma.upsert.call_args.kwargs["ids"][0]
    semantic_server.remember("alpha")
    id_alpha_again = mock_chroma.upsert.call_args.kwargs["ids"][0]
    semantic_server.remember("beta")
    id_beta = mock_chroma.upsert.call_args.kwargs["ids"][0]
    assert id_alpha == id_alpha_again
    assert id_alpha != id_beta
    assert re.fullmatch(r"[0-9a-f]{16}", id_alpha)


def test_remember_returns_user_visible_confirmation(semantic_server):
    result = semantic_server.remember("hello world")
    assert "Remembered" in result
    assert re.search(r"id=[0-9a-f]{16}", result)


def test_remember_writes_default_semantic_volume_to_redis(
    semantic_server, mock_chroma, fake_redis
):
    semantic_server.remember("volume check")
    doc_id = mock_chroma.upsert.call_args.kwargs["ids"][0]
    score = fake_redis.zscore(
        semantic_server.VOLUME_INDEX_KEY, f"semantic:{doc_id}"
    )
    assert score == semantic_server.DEFAULT_VOLUME["semantic"]


# ---------------------------------------------------------------------------
# search_memory()
# ---------------------------------------------------------------------------


def test_search_memory_empty_collection_does_not_crash(semantic_server, mock_chroma):
    mock_chroma.count.return_value = 0
    result = semantic_server.search_memory("anything")
    assert "No memories stored yet." in result


def test_search_memory_empty_collection_skips_query(semantic_server, mock_chroma):
    mock_chroma.count.return_value = 0
    semantic_server.search_memory("anything")
    assert not mock_chroma.query.called


def test_search_memory_returns_no_relevant_message_when_query_empty(
    semantic_server, mock_chroma
):
    mock_chroma.count.return_value = 3
    mock_chroma.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    result = semantic_server.search_memory("nothing matches")
    assert "No relevant memories found." in result


def test_search_memory_returns_matched_document_text(semantic_server, mock_chroma):
    mock_chroma.count.return_value = 1
    mock_chroma.query.return_value = {
        "ids": [["doc1"]],
        "documents": [["matched document text"]],
        "metadatas": [[{
            "date": "2026-05-11",
            "tags": "",
            "timestamp": _time.time(),
        }]],
        "distances": [[0.1]],  # similarity 0.9, above 0.35 threshold
    }
    result = semantic_server.search_memory("query")
    assert "matched document text" in result
    assert "Found" in result


def test_search_memory_filters_low_similarity(semantic_server, mock_chroma):
    """server.py:385 drops results with semantic_sim < 0.35 (distance > 0.65)."""
    mock_chroma.count.return_value = 1
    mock_chroma.query.return_value = {
        "ids": [["doc1"]],
        "documents": [["irrelevant text"]],
        "metadatas": [[{"date": "2026-05-11", "tags": ""}]],
        "distances": [[0.9]],  # similarity 0.1, below threshold
    }
    result = semantic_server.search_memory("query")
    assert "irrelevant text" not in result


def test_search_memory_caps_n_results_at_collection_count(
    semantic_server, mock_chroma
):
    """actual_n = min(n_results, count) — server.py:365."""
    mock_chroma.count.return_value = 2
    mock_chroma.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    semantic_server.search_memory("q", n_results=10)
    assert mock_chroma.query.call_args.kwargs["n_results"] == 2


def test_search_memory_includes_tags_when_present(semantic_server, mock_chroma):
    mock_chroma.count.return_value = 1
    mock_chroma.query.return_value = {
        "ids": [["d1"]],
        "documents": [["doc body"]],
        "metadatas": [[{
            "date": "2026-05-11",
            "tags": "architecture,db",
            "timestamp": _time.time(),
        }]],
        "distances": [[0.05]],
    }
    result = semantic_server.search_memory("q")
    assert "architecture,db" in result
