"""Tests for SQLite storage layer (db.py)."""

import sqlite3
import pytest
from pathlib import Path
import db


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    db_file = tmp_path / "test_db.sqlite"
    db.set_db_path(db_file)
    yield
    db.close_db()


def test_fact_crud():
    assert db.fact_count() == 0
    db.fact_set("user_name", {"value": "Лёха", "ttl_days": 90})
    assert db.fact_count() == 1

    fact = db.fact_get("user_name")
    assert fact is not None
    assert fact["value"] == "Лёха"
    assert fact["ttl_days"] == 90

    all_facts = db.fact_all()
    assert "user_name" in all_facts

    assert db.fact_delete("user_name") is True
    assert db.fact_count() == 0
    assert db.fact_get("user_name") is None


def test_volume_crud_and_sorting():
    db.volume_set("fact:a", "fact", 20.0)
    db.volume_set("fact:b", "fact", 80.0)
    db.volume_set("doc:c", "doc", 50.0)

    assert db.volume_get("fact:a") == 20.0
    assert db.volume_get("fact:b") == 80.0

    vol_map = db.volume_map("fact")
    assert vol_map == {"fact:a": 20.0, "fact:b": 80.0}

    sorted_vols = db.volume_all_sorted()
    # Ordered by volume ASC, entity_key ASC
    assert sorted_vols == [
        ("fact:a", 20.0),
        ("doc:c", 50.0),
        ("fact:b", 80.0),
    ]


def test_causal_memories_crud():
    db.causal_insert(
        id="c1",
        text="User corrected test runner",
        layer=0,
        cause="Running jest",
        effect="Failed due to ESM",
        confidence=0.8,
        session_id="ses_123",
        tags="testing,jest",
    )

    item = db.causal_get("c1")
    assert item is not None
    assert item["text"] == "User corrected test runner"
    assert item["layer"] == 0
    assert item["session_id"] == "ses_123"

    query_res = db.causal_query(layer=0, session_id="ses_123")
    assert len(query_res) == 1
    assert query_res[0]["id"] == "c1"


def test_belief_state_supersession():
    # Assert initial belief
    db.belief_assert(
        id="b1",
        subject="user.preferences",
        predicate="test_runner",
        object_val="jest",
        confidence=0.8,
    )

    active = db.belief_get_active("user.preferences", "test_runner")
    assert active is not None
    assert active["id"] == "b1"
    assert active["object"] == "jest"
    assert active["status"] == "active"

    # Assert new conflicting belief -> must supersede b1
    db.belief_assert(
        id="b2",
        subject="user.preferences",
        predicate="test_runner",
        object_val="vitest",
        confidence=0.9,
    )

    active_now = db.belief_get_active("user.preferences", "test_runner")
    assert active_now is not None
    assert active_now["id"] == "b2"
    assert active_now["object"] == "vitest"
    assert active_now["supersedes"] == "b1"

    # Verify b1 was updated to superseded with pointer
    conn = db.get_db()
    old_row = dict(conn.execute("SELECT * FROM belief_state WHERE id = 'b1'").fetchone())
    assert old_row["status"] == "superseded"
    assert old_row["superseded_by"] == "b2"

    all_active = db.belief_all_active("user.preferences")
    assert len(all_active) == 1
    assert all_active[0]["id"] == "b2"
