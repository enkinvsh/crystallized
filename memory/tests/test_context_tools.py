"""Isolated tests for the read-path UX pass (memory_context, get_fact,
list_facts, recall bounding, reinforce decay, clip/budget helpers).

Isolation is TWO-LAYERED and must stay that way — this data is irreplaceable:

  1. ``OPENCODE_MEMORY_DB`` is pointed at a throwaway directory BEFORE ``db`` is
     imported, so even an import-time code path cannot open production.
  2. Every test runs through the ``iso`` fixture, which repoints the store at a
     per-test ``tmp_path`` DB and ASSERTS the resulting ``db.DB_PATH`` lives
     inside that tmp dir before a single row is written.

Semantic/doc/vault sections may report empty/unavailable — those layers are
try/except-wrapped and are not the subject under test here.

Run from project root:
    uv run --with pytest python -m pytest tests/test_context_tools.py -v
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("OPENCODE_MEMORY_DISABLE_CHROMA_API", "1")
os.environ.setdefault(
    "OPENCODE_MEMORY_DB",
    str(Path(tempfile.mkdtemp(prefix="memtest-import-")) / "import-guard.db"),
)

import db  # noqa: E402
import server  # noqa: E402

_ORIG_DB_PATH = db.DB_PATH

PRODUCTION_DB = Path.home() / ".config" / "opencode" / "memory" / "memory.db"

BIG_START = "BIGSTART_MARKER"
BIG_END = "BIGEND_MARKER"


def _iso(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _seed_fact(key, value, updated_days_ago, vol, expires_days_ahead=30,
               last_reinforced_days_ago=None):
    lr = _iso(last_reinforced_days_ago if last_reinforced_days_ago is not None
              else updated_days_ago)
    data = {
        "value": value,
        "updated_at": _iso(updated_days_ago),
        "last_reinforced_at": lr,
        "ttl_days": 60,
        "expires_at": _iso(-expires_days_ahead),
    }
    db.fact_set(key, data)
    db.volume_set(f"fact:{key}", "fact", vol)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "test.db")
    assert str(tmp_path) in str(db.DB_PATH), db.DB_PATH
    assert db.DB_PATH.resolve() != PRODUCTION_DB.resolve(), db.DB_PATH

    big = BIG_START + ("x" * (50000 - len(BIG_START) - len(BIG_END))) + BIG_END
    _seed_fact("alpha_one", "alpha one widget zqxwidget core", 1.0, 80.0, last_reinforced_days_ago=0)
    _seed_fact("alpha_two", "alpha two zqxwidget detail", 2.0, 70.0, last_reinforced_days_ago=0)
    _seed_fact("alpha_three", "alpha three note", 3.0, 30.0)
    _seed_fact("alpha_big", big, 6.0, 25.0)
    _seed_fact("alpha_old", "alpha old expired note", 40.0, 20.0,
               expires_days_ahead=-5)
    _seed_fact("beta_one", "beta one zqxwidget info", 1.5, 60.0, last_reinforced_days_ago=0)
    _seed_fact("beta_two", "beta two info", 2.5, 55.0)
    _seed_fact("beta_three", "beta three info", 3.5, 15.0)
    _seed_fact("gamma_one", "gamma one zqxwidget info", 1.2, 50.0, last_reinforced_days_ago=0)
    _seed_fact("gamma_two", "gamma two info", 2.2, 45.0)
    server._invalidate_fact_embeddings()

    yield db

    db.close_db()
    db.DB_PATH = _ORIG_DB_PATH
    server._invalidate_fact_embeddings()


def _snapshot(store):
    """Facts AND every volume row — both halves are load-bearing for the
    'never reinforces' invariant."""
    return store.fact_all(), dict(store.volume_all_sorted())


def _section(out: str, name: str) -> list[str]:
    for block in out.split("\n\n"):
        if block.startswith(f"{name}:"):
            return [ln for ln in block.splitlines()[1:] if ln.strip()]
    return []


def test_memory_context_zero_arg_bounded(iso):
    out = server.memory_context()
    assert len(out.encode("utf-8")) <= 12000, len(out.encode("utf-8"))
    assert "Projects:" in out
    assert "Recent:" in out
    assert "Salient:" in out


def test_memory_context_never_reinforces(iso):
    before = _snapshot(iso)
    server.memory_context()
    server.memory_context(project="alpha")
    after = _snapshot(iso)
    assert before == after


def test_fact_group_boundaries():
    assert server._fact_group("dropweb_x") == "dropweb"
    assert server._fact_group("dropwebx_y") == "dropwebx"
    assert server._fact_group("remna_fleet_z") == "remna_fleet"
    assert server._fact_group("foo_bar") == "foo"
    assert server._fact_group("solo") == "solo"


def test_recent_diversified_and_salient_disjoint(iso):
    out = server.memory_context()
    recent_lines = _section(out, "Recent")
    salient_lines = _section(out, "Salient")

    import re
    rec = [re.match(r"\s+\[(?P<g>[^ ]+) · \d\d-\d\d\] (?P<k>[^:]+):", ln)
           for ln in recent_lines]
    rec = [m.groupdict() for m in rec if m]
    groups = [d["g"] for d in rec]
    for g in set(groups):
        assert groups.count(g) <= 2, (g, groups)

    facts = iso.fact_all()
    ups = [facts[d["k"]]["updated_at"] for d in rec]
    assert ups == sorted(ups, reverse=True)

    recent_keys = {d["k"] for d in rec}
    sal = [re.match(r"\s+\[vol [\d.]+ · [^\]]+\] (?P<k>[^:]+):", ln)
           for ln in salient_lines]
    sal_keys = {m.group("k") for m in sal if m}
    assert recent_keys.isdisjoint(sal_keys), (recent_keys, sal_keys)


def test_recall_reinforces_only_returned_topN(iso):
    _f, before = _snapshot(iso)
    server.recall("zqxwidget", n_results=2)
    _f2, after = _snapshot(iso)
    changed = {
        k[len("fact:"):]
        for k in before
        if k.startswith("fact:") and abs(after.get(k, 0.0) - before[k]) > 1e-9
    }
    assert changed == {"alpha_one", "alpha_two"}, changed


def test_reinforce_starts_from_decayed(iso):
    iso.volume_set("fact:decay_fact", "fact", 50.0)
    old = _iso(30.0)
    new_vol = server._reinforce("fact", "decay_fact", quality=1.0, last_reinforced_at=old)
    naive = 50.0 + 12.0 * 1.0 * (50.0 / 100.0)
    assert new_vol < naive, (new_vol, naive)
    stored = iso.volume_get("fact:decay_fact")
    assert abs(stored - new_vol) < 1e-6


def test_get_fact_reinforces_once_and_chunks_losslessly(iso):
    before = iso.volume_get("fact:gamma_two")
    server.get_fact("gamma_two")
    after_first = iso.volume_get("fact:gamma_two")
    assert after_first > before
    server.get_fact("gamma_two", offset=1)
    after_cont = iso.volume_get("fact:gamma_two")
    assert abs(after_cont - after_first) < 1e-9

    value = iso.fact_get("alpha_big")["value"]
    total = len(value)
    out0 = server.get_fact("alpha_big")
    assert 'Continue: get_fact("alpha_big", offset=20000)' in out0
    assert value[0:20000] in out0
    out1 = server.get_fact("alpha_big", offset=20000)
    assert value[20000:40000] in out1
    out2 = server.get_fact("alpha_big", offset=40000)
    assert value[40000:total] in out2
    assert "Continue:" not in out2


def test_get_fact_missing_suggests(iso):
    out = server.get_fact("alpha_onx")
    assert "Fact not found" in out
    assert "alpha_one" in out


def test_list_facts_prefix_limit_no_reinforce(iso):
    out = server.list_facts(prefix="alpha_")
    assert "alpha_one" in out
    assert "beta_one" not in out

    out_clamped = server.list_facts(prefix="", limit=1000)
    assert "showing 10 of 10" in out_clamped
    out_lim = server.list_facts(prefix="", limit=1)
    assert "showing 1 of 10" in out_lim

    _f, before = _snapshot(iso)
    server.list_facts(prefix="")
    _f2, after = _snapshot(iso)
    assert before == after


def test_list_facts_full_budget_never_splits(iso):
    out = server.list_facts(prefix="alpha_", full=True)
    assert len(out.encode("utf-8")) <= 45000
    assert (BIG_START in out) == (BIG_END in out)


def test_clip_output_utf8_safe():
    s = "я" * 30000
    out = server._clip_output(s, 45000)
    assert len(out.encode("utf-8")) <= 45000
    out.encode("utf-8").decode("utf-8")
    assert out.endswith("[output clipped]")


def test_save_fact_over_limit_rejected_and_nothing_written(iso):
    before = _snapshot(iso)
    out = server.save_fact("alpha_huge", "y" * (server.FACT_MAX_CHARS + 1))
    assert out.startswith("REJECTED (not saved)")
    assert iso.fact_get("alpha_huge") is None
    assert _snapshot(iso) == before


def test_save_fact_at_limit_accepted(iso):
    value = "z" * server.FACT_MAX_CHARS
    server.save_fact("alpha_edge", value)
    assert iso.fact_get("alpha_edge")["value"] == value


def test_save_fact_success_message_excludes_value(iso):
    value = "UNIQUEVALUEMARKER " * 10
    out = server.save_fact("alpha_msg", value)
    assert out.startswith(f"Saved fact: alpha_msg ({len(value)} chars, TTL: ")
    assert "UNIQUEVALUEMARKER" not in out


def _seed_long_keyed(prefix, count):
    keys = [f"{prefix}_{i:02d}_" + "k" * 500 for i in range(count)]
    for i, k in enumerate(keys):
        _seed_fact(k, "qvzmatch " + "v" * 2000, 1.0 + i * 0.01, 40.0)
    server._invalidate_fact_embeddings()
    return keys


def test_recall_capped_and_reinforces_only_visible(iso):
    keys = _seed_long_keyed("alpha_cap", 10)
    _f, before = _snapshot(iso)
    out = server.recall("qvzmatch", n_results=10)
    assert len(out) <= server.RECALL_MAX_CHARS, len(out)
    assert out.endswith("narrow the query or open items with get_fact/read_doc]")
    _f2, after = _snapshot(iso)
    reinforced = {k for k in keys if abs(after[f"fact:{k}"] - before[f"fact:{k}"]) > 1e-9}
    visible = {k for k in keys if f"  {k}: " in out}
    assert visible and visible != set(keys)
    assert reinforced == visible


def test_recall_fact_vector_topup_capped(iso, monkeypatch):
    keys = [k for k in iso.fact_all() if k.startswith(("alpha_", "beta_", "gamma_"))]
    monkeypatch.setattr(server, "_get_vector_cache", lambda: {"rows": [1]})
    monkeypatch.setattr(server, "embed_query", lambda q: None)
    monkeypatch.setattr(
        server, "vector_search",
        lambda kind, **kw: [(kind, k, 0.9) for k in keys] if kind == "fact" else [],
    )
    out = server.recall("nomatchqqq", n_results=5)
    vec_lines = [ln for ln in _section(out, "Facts") if ln.lstrip().startswith("[vec")]
    assert len(vec_lines) == server.RECALL_FACT_VECTOR_TOPUP, out


def test_recall_per_section_line_caps(iso, tmp_path, monkeypatch):
    notes = tmp_path / "notes" / "f"
    notes.mkdir(parents=True)
    monkeypatch.setattr(server, "NOTES_DIR", tmp_path / "notes")
    for i in range(5):
        (notes / f"doc{i}.md").write_text(f"qsecmatch doc {i}", encoding="utf-8")
        iso.causal_insert(f"c{i}", f"qsecmatch lesson {i}", confidence=0.9)
        iso.belief_assert(f"b{i}", f"subj{i}", f"pred{i}", f"qsecmatch obj {i}")
        iso.semantic_set(f"vsem{i}", f"vector only memory {i}", {})
    vec = {
        "semantic": [f"vsem{i}" for i in range(5)],
        "causal": [f"c{i}" for i in range(5)],
        "doc": [f"f/doc{i}" for i in range(5)],
    }
    monkeypatch.setattr(server, "_get_vector_cache", lambda: {"rows": [1]})
    monkeypatch.setattr(server, "embed_query", lambda q: None)
    monkeypatch.setattr(
        server, "vector_search",
        lambda kind, **kw: [(kind, k, 0.9) for k in vec.get(kind, [])],
    )
    out = server.recall("qsecmatch", n_results=5)
    assert "truncated" not in out
    assert len(_section(out, "Causal memories")) == server.RECALL_CAUSAL_MAX_LINES
    assert len(_section(out, "Documents")) == server.RECALL_DOC_MAX_LINES
    assert len(_section(out, "Beliefs")) == server.RECALL_BELIEF_MAX_LINES
    sem_vec = [ln for ln in _section(out, "Semantic memories") if "[vec" in ln]
    assert len(sem_vec) == server.RECALL_SEMANTIC_VECTOR_TOPUP, out


def test_memory_context_all_sections_untruncated(iso):
    out = server.memory_context()
    assert "truncated" not in out
    for header in ("Drill down:", "Projects:", "Recent:", "Salient:",
                   "Freshness:", "Semantic (", "Docs: "):
        assert header in out, header


def test_memory_context_within_cap(iso):
    _seed_long_keyed("alpha_ctx", 20)
    assert len(server.memory_context()) <= server.CONTEXT_MAX_CHARS
    assert len(server.memory_context(project="alpha")) <= server.CONTEXT_MAX_CHARS
