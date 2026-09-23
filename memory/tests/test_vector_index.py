"""The living recall index: each key's vectors must follow its text.

Until the sync existed the table had one writer, a backfill run once, so a
record written afterwards never reached meaning-based recall. These tests pin
the contract that replaces it: new text is embedded, edited text re-embedded,
deleted records dropped, legacy rows kept serving until their turn, and an
unreadable source skipped rather than taken for an empty one.
"""

import numpy as np
import pytest

import vector_index as vi

MODEL = "test-model"


def _vec(seed: int) -> bytes:
    a = np.asarray([1.0, float(seed % 7), float(seed % 3), 1.0], dtype=np.float32)
    return (a / np.linalg.norm(a)).astype("<f4").tobytes()


class _Embedder:
    """Two chunk vectors per text; records what it was asked to encode."""

    def __init__(self):
        self.seen: list[str] = []

    def __call__(self, texts):
        self.seen.extend(texts)
        return [[_vec(len(t)), _vec(len(t) + 1)] for t in texts]


def _run(kind, pairs, embed, **kwargs):
    return vi.sync([vi.plan(kind, pairs, MODEL)], embed, MODEL, **kwargs)[kind]


def _rows(store, kind):
    return store.embedding_load(kind=kind, model=MODEL)[0]


def test_a_new_record_is_embedded_once(store):
    store.fact_set("fresh_fact", {"value": "written after the backfill"})
    embed = _Embedder()

    first = _run("fact", vi.fact_sources(), embed)
    second = _run("fact", vi.fact_sources(), embed)

    assert (first.embedded, first.chunks) == (1, 2)
    assert (second.embedded, second.deleted) == (0, 0)
    assert embed.seen == ["fresh_fact: written after the backfill"]
    assert store.embedding_hashes("fact", MODEL) == {
        "fresh_fact": vi.text_hash("fresh_fact: written after the backfill")
    }


def test_an_edited_record_is_re_embedded_from_its_new_text(store):
    store.fact_set("edited", {"value": "v1"})
    _run("fact", vi.fact_sources(), _Embedder())
    store.fact_set("edited", {"value": "v2"})

    todo = vi.plan("fact", vi.fact_sources(), MODEL).todo
    embed = _Embedder()
    _run("fact", vi.fact_sources(), embed)

    assert [(w.key, w.priority) for w in todo] == [("edited", vi.CHANGED)]
    assert embed.seen == ["edited: v2"]


def test_a_deleted_record_loses_its_vectors(store):
    store.embedding_upsert("fact", "deleted_fact", [_vec(1)] * 3, model=MODEL, dim=4, src_hash="x")
    store.fact_set("kept", {"value": "k"})

    result = _run("fact", vi.fact_sources(), _Embedder())

    assert result.deleted == 1
    assert [r["key"] for r in _rows(store, "fact")] == ["kept", "kept"]


def test_missing_work_runs_first_and_legacy_rows_serve_until_their_turn(store):
    store.embedding_upsert("fact", "legacy", [_vec(1)], model=MODEL, dim=4)  # no hash
    store.fact_set("legacy", {"value": "indexed on 08-23"})
    store.fact_set("unseen", {"value": "never indexed"})
    embed = _Embedder()

    result = _run("fact", vi.fact_sources(), embed, step=1, pace=lambda done, total: True)

    assert embed.seen == ["unseen: never indexed"]
    assert (result.embedded, result.pending) == (1, 1)
    assert store.embedding_hashes("fact", MODEL) == {
        "legacy": "",
        "unseen": vi.text_hash("unseen: never indexed"),
    }


def test_priority_spans_kinds(store):
    store.embedding_upsert("fact", "old", [_vec(1)], model=MODEL, dim=4)  # legacy
    store.fact_set("old", {"value": "o"})
    store.causal_insert("lesson_new", "a lesson nobody indexed", cause="c", effect="e")
    embed = _Embedder()
    plans = [
        vi.plan("fact", vi.fact_sources(), MODEL),
        vi.plan("causal", vi.causal_sources(), MODEL),
    ]

    vi.sync(plans, embed, MODEL, step=1)

    assert embed.seen == ["lesson_new a lesson nobody indexed c e", "old: o"]


def test_blank_text_is_never_planned_and_its_old_vectors_go(store):
    store.embedding_upsert("semantic", "blank", [_vec(1)], model=MODEL, dim=4, src_hash="x")
    store.semantic_set("blank", "   ", {})

    p = vi.plan("semantic", vi.semantic_sources([]), MODEL)

    assert (p.todo, p.gone) == ([], ["blank"])


def test_force_re_embeds_current_keys(store):
    store.fact_set("current", {"value": "c"})
    _run("fact", vi.fact_sources(), _Embedder())

    forced = vi.plan("fact", vi.fact_sources(), MODEL, force=True)

    assert [w.key for w in forced.todo] == ["current"]


def test_a_text_without_vectors_leaves_no_rows(store):
    store.embedding_upsert("fact", "k", [_vec(1)], model=MODEL, dim=4)
    store.fact_set("k", {"value": "v"})

    _run("fact", vi.fact_sources(), lambda texts: [[] for _ in texts])

    assert _rows(store, "fact") == []


def test_kinds_are_isolated(store):
    store.fact_set("same_key", {"value": "v"})
    store.embedding_upsert(vi.HOOK_FACT_KIND, "same_key", [_vec(1)], model=MODEL, dim=4, src_hash="h")

    _run("fact", vi.fact_sources(), _Embedder())

    assert store.embedding_hashes(vi.HOOK_FACT_KIND, MODEL) == {"same_key": "h"}


class TestSources:
    def test_docs_are_keyed_by_relative_path_without_suffix(self, store, tmp_path):
        (tmp_path / "leo").mkdir()
        (tmp_path / "leo" / "2026-09-23.md").write_text("entry", encoding="utf-8")

        assert vi.doc_sources(tmp_path) == [("leo/2026-09-23", "leo/2026-09-23\nentry")]

    def test_an_absent_notes_dir_is_unknown_not_empty(self, store, tmp_path):
        assert vi.doc_sources(tmp_path / "unmounted") is None

    def test_an_unreadable_chroma_skips_the_semantic_kind(self, store, tmp_path):
        store.semantic_set("fb", "fallback text", {})

        skipped = vi.sources("semantic", notes_dir=tmp_path, chroma_documents=lambda: None)
        read = vi.sources("semantic", notes_dir=tmp_path, chroma_documents=lambda: [("ch", "c")])

        assert skipped is None
        assert read == [("ch", "c"), ("fb", "fallback text")]

    def test_the_fallback_wins_an_id_both_stores_hold(self, store):
        store.semantic_set("dup", "newer", {})

        assert vi.semantic_sources([("dup", "older")]) == [("dup", "newer")]

    def test_an_unknown_kind_is_an_error(self, store, tmp_path):
        with pytest.raises(ValueError):
            vi.sources("nope", notes_dir=tmp_path, chroma_documents=lambda: [])
