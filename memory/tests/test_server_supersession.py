"""Tests for supersession features in server.py."""

import numpy as np

import db
import server
import volume


def test_save_fact_explicit_and_textual_supersedes(srv):
    db.fact_set("fact_old1_12345", {"value": "old fact 1"})
    db.fact_set("fact_old2_67890", {"value": "old fact 2"})
    volume.set_volume("fact", "fact_old1_12345", 80.0)
    volume.set_volume("fact", "fact_old2_67890", 80.0)

    res = server.save_fact(
        "fact_new_abcdef",
        "ОТМЕНЯЕТ fact_old2_67890 и дополняет его",
        supersedes="fact_old1_12345, non_existent_fact_12345",
    )

    assert "fact_new_abcdef" in res
    assert "fact_old1_12345" in res
    assert "fact_old2_67890" in res
    assert "non_existent_fact_12345" in res  # reported as skipped in returned string

    f1 = db.fact_get("fact_old1_12345")
    assert f1["superseded_by"] == "fact_new_abcdef"
    assert volume.effective_volume("fact", "fact_old1_12345") <= volume.SUPERSEDED_VOLUME_RATIO * volume.effective_volume("fact", "fact_new_abcdef") + 1e-6

    f2 = db.fact_get("fact_old2_67890")
    assert f2["superseded_by"] == "fact_new_abcdef"
    assert volume.effective_volume("fact", "fact_old2_67890") <= volume.SUPERSEDED_VOLUME_RATIO * volume.effective_volume("fact", "fact_new_abcdef") + 1e-6


def test_supersede_fact_tool(srv):
    db.fact_set("f_a", {"value": "a"})
    db.fact_set("f_b", {"value": "b"})
    volume.set_volume("fact", "f_a", 70.0)
    volume.set_volume("fact", "f_b", 40.0)

    res = server.supersede_fact("f_a", "f_b")
    assert "f_a" in res and "f_b" in res
    assert db.fact_get("f_a")["superseded_by"] == "f_b"
    assert volume.effective_volume("fact", "f_a") <= volume.SUPERSEDED_VOLUME_RATIO * volume.effective_volume("fact", "f_b") + 1e-6

    # Clear link
    res_clear = server.supersede_fact("f_a", "")
    assert "cleared" in res_clear.lower() or "unlinked" in res_clear.lower()
    assert "superseded_by" not in db.fact_get("f_a")


def test_get_fact_displays_superseded_header(srv):
    db.fact_set("f1", {"value": "first version"})
    db.fact_set("f2", {"value": "second version"})
    db.fact_set("f3", {"value": "third version"})
    db.fact_supersede("f1", "f2")
    db.fact_supersede("f2", "f3")

    out_f1 = server.get_fact("f1")
    assert "Superseded by: f2 — it overrides this fact in whole or in part; read it first." in out_f1
    assert "Latest in chain: f3" in out_f1

    out_f2 = server.get_fact("f2")
    assert "Superseded by: f3 — it overrides this fact in whole or in part; read it first." in out_f2
    assert "Latest in chain:" not in out_f2  # f3 is head, same as f3


def test_list_facts_marks_superseded(srv):
    db.fact_set("proj_active", {"value": "active value"})
    db.fact_set("proj_old", {"value": "old value"})
    db.fact_supersede("proj_old", "proj_active")

    out = server.list_facts(prefix="proj_")
    assert "proj_old [superseded by proj_active]:" in out
    assert "proj_active:" in out
    assert "proj_active [superseded" not in out


def test_recall_ranks_active_above_superseded_and_marks(srv):
    # Both match the word "database"
    db.fact_set("fact_old", {"value": "database connection settings", "updated_at": "2026-01-01T00:00:00"})
    db.fact_set("fact_new", {"value": "database connection settings v2", "updated_at": "2026-01-02T00:00:00"})
    volume.set_volume("fact", "fact_old", 90.0)
    volume.set_volume("fact", "fact_new", 50.0)

    db.fact_supersede("fact_old", "fact_new")

    res = server.recall("database")
    assert "Facts:" in res
    assert "fact_old [superseded by fact_new]:" in res
    assert "fact_new:" in res

    # active fact comes first even if old had higher volume
    pos_new = res.find("fact_new:")
    pos_old = res.find("fact_old [superseded by fact_new]:")
    assert pos_new != -1
    assert pos_old != -1
    assert pos_new < pos_old


def test_memory_context_salient_excludes_superseded(srv):
    db.fact_set("proj_super_old", {"value": "loud but superseded", "updated_at": "2026-01-01T00:00:00"})
    db.fact_set("proj_active_new", {"value": "active recent", "updated_at": "2026-01-02T00:00:00"})
    volume.set_volume("fact", "proj_super_old", 95.0)
    volume.set_volume("fact", "proj_active_new", 60.0)

    db.fact_supersede("proj_super_old", "proj_active_new")

    ctx = server.memory_context()
    # Salient should not contain superseded fact
    if "Salient:" in ctx:
        salient_part = ctx.split("Salient:")[1]
        assert "proj_super_old" not in salient_part

    # Recent should mark it if present
    if "Recent:" in ctx and "proj_super_old" in ctx:
        assert "[superseded by proj_active_new]" in ctx

    # Zoom should mark it
    zoom = server.memory_context(project="proj")
    assert "proj_super_old [superseded by proj_active_new]" in zoom


def test_semantic_fact_search_marks_superseded_instead_of_hiding(srv, monkeypatch):
    """Most corrections rewrite only part of the older fact, so the vector path
    must keep it findable, with the pointer leading its value."""

    class _Encoder:
        def encode(self, texts, **_kwargs):
            if isinstance(texts, str):
                return np.array([1.0, 0.0], dtype=np.float32)
            return np.array([[1.0, 0.0]] * len(texts), dtype=np.float32)

    monkeypatch.setattr(server, "get_encoder", _Encoder)
    db.fact_set("vec_old_fact", {"value": "the font has cyrillic; use it for the hero"})
    db.fact_set("vec_new_fact", {"value": "hero font replaced, cyrillic finding still true"})
    db.fact_supersede("vec_old_fact", "vec_new_fact")
    server._invalidate_fact_embeddings()
    try:
        hits = server._search_facts_semantic(np.array([1.0, 0.0], dtype=np.float32), n=10)
    finally:
        server._invalidate_fact_embeddings()

    by_key = {h["key"]: h for h in hits}
    assert set(by_key) == {"vec_old_fact", "vec_new_fact"}
    assert by_key["vec_old_fact"]["value"].startswith("[superseded by vec_new_fact] ")
    assert not by_key["vec_new_fact"]["value"].startswith("[superseded")


def test_reading_a_superseded_fact_neither_boosts_nor_reclocks_it(srv):
    db.fact_set("frozen_old_fact", {"value": "old", "last_reinforced_at": "2026-01-01T00:00:00"})
    db.fact_set("frozen_new_fact", {"value": "new"})
    db.fact_supersede("frozen_old_fact", "frozen_new_fact")
    stored_before = volume.get_volume("fact", "frozen_old_fact")

    server.get_fact("frozen_old_fact")
    server.recall("frozen_old_fact")

    after = db.fact_get("frozen_old_fact")
    assert after["last_reinforced_at"] == "2026-01-01T00:00:00"
    assert volume.get_volume("fact", "frozen_old_fact") == stored_before


def test_fact_vectors_follow_writes_from_other_processes(srv, monkeypatch):
    """Another window's server writes straight to SQLite; this one must notice
    and re-encode only what changed."""
    encoded: list[str] = []

    class _Encoder:
        def encode(self, texts, **_kwargs):
            encoded.extend(texts)
            return np.array([[1.0, 0.0]] * len(texts), dtype=np.float32)

    monkeypatch.setattr(server, "get_encoder", _Encoder)
    server._invalidate_fact_embeddings()
    db.fact_set("xproc_first", {"value": "a", "updated_at": "2026-09-01T00:00:00"})
    assert server._get_fact_embeddings()["keys"] == ["xproc_first"]

    encoded.clear()
    db.fact_set("xproc_second", {"value": "b", "updated_at": "2026-09-02T00:00:00"})  # no server tool involved
    cache = server._get_fact_embeddings()
    assert set(cache["keys"]) == {"xproc_first", "xproc_second"}
    assert encoded == ["xproc_second: b"]
    server._invalidate_fact_embeddings()
