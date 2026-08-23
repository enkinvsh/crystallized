"""Tests for the vector retrieval stack (Stage A): chunking and `vector_search`.

Hermeticity: nothing here loads the embedding model. The chunker takes a
tokenizer argument so the ALGORITHM can be tested against a fake one, and
`vector_search` takes its query vector from `get_encoder`, which is
monkeypatched. Loading `paraphrase-multilingual-MiniLM-L12-v2` in the suite
would mean a network fetch on a cold machine and ~1s per test on a warm one.

The empirical check that the real tokenizer agrees with the chosen window is a
measurement, reported separately; it is not part of this suite for the reason
above.

The cosine fixtures below are the owner's live measurements against a real
stored lesson, not invented numbers:
    +0.547  paraphrase that substring/token matching missed all day
    +0.634  «помечай» vs stored «помечать» — the inflection ceiling
    +0.719  a natural future query
    +0.036  control: dns routing on a router
    +0.169  control: telegram bot subscription price
"""

import numpy as np
import pytest

import db
import server

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
DIM = 8

RELEVANT_PARAPHRASE = 0.547
RELEVANT_INFLECTION = 0.634
RELEVANT_FUTURE = 0.719
CONTROL_DNS = 0.036
CONTROL_TELEGRAM = 0.169


# ---------------------------------------------------------------------------
# Vector fixtures: build unit vectors at an exact cosine to a fixed query
# ---------------------------------------------------------------------------

_QUERY = np.zeros(DIM, dtype=np.float32)
_QUERY[0] = 1.0
_PERP = np.zeros(DIM, dtype=np.float32)
_PERP[1] = 1.0


def _at_cosine(target: float) -> bytes:
    """A unit vector whose dot product with `_QUERY` is exactly `target`."""
    v = target * _QUERY + np.sqrt(max(0.0, 1.0 - target * target)) * _PERP
    return (v / np.linalg.norm(v)).astype("<f4").tobytes()


@pytest.fixture
def vectors(store, monkeypatch):
    """`server` with a stub encoder returning `_QUERY`, and a clean store."""
    class _StubEncoder:
        def encode(self, text, **_kwargs):
            return _QUERY.copy()

    monkeypatch.setattr(server, "get_encoder", _StubEncoder)
    monkeypatch.setattr(server, "EMBED_MODEL_NAME", MODEL)
    server._invalidate_vector_cache()
    yield server
    server._invalidate_vector_cache()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class _FakeTokenizer:
    """Whitespace tokenizer exposing the offset mapping the chunker needs.

    Mirrors the HuggingFace fast-tokenizer contract that matters here:
    `add_special_tokens=False, return_offsets_mapping=True` yields character
    spans, one per token.
    """

    def __call__(self, text, add_special_tokens=False, return_offsets_mapping=True):
        offsets = []
        pos = 0
        for word in text.split(" "):
            if word:
                offsets.append((pos, pos + len(word)))
            pos += len(word) + 1
        return {"offset_mapping": offsets}


class TestChunking:
    def test_short_text_is_a_single_chunk(self):
        chunks = server._chunk_by_tokens("короткий текст", tokenizer=_FakeTokenizer())

        assert chunks == ["короткий текст"]

    def test_a_long_russian_text_produces_several_chunks(self):
        text = " ".join(f"слово{i}" for i in range(400))

        chunks = server._chunk_by_tokens(text, tokenizer=_FakeTokenizer())

        assert len(chunks) > 1

    def test_the_chunks_cover_the_whole_text(self):
        """The head AND the tail must both survive — the rule lives at the end."""
        head, tail = "ГИПОТЕЗА_В_НАЧАЛЕ", "ПРАВИЛО_НА_БУДУЩЕЕ"
        text = " ".join([head] + [f"слово{i}" for i in range(400)] + [tail])

        chunks = server._chunk_by_tokens(text, tokenizer=_FakeTokenizer())

        assert head in chunks[0]
        assert tail in chunks[-1]
        assert any(head in c for c in chunks) and any(tail in c for c in chunks)

    def test_consecutive_chunks_overlap(self):
        """A rule split across a boundary must survive intact in one chunk."""
        text = " ".join(f"слово{i}" for i in range(400))

        chunks = server._chunk_by_tokens(text, tokenizer=_FakeTokenizer())
        first_tokens = set(chunks[0].split())
        second_tokens = set(chunks[1].split())

        assert first_tokens & second_tokens

    def test_every_chunk_fits_the_window(self):
        text = " ".join(f"слово{i}" for i in range(1000))

        chunks = server._chunk_by_tokens(text, tokenizer=_FakeTokenizer())

        assert all(len(c.split()) <= server.CHUNK_TOKENS for c in chunks)

    def test_empty_text_yields_no_chunks(self):
        assert server._chunk_by_tokens("", tokenizer=_FakeTokenizer()) == []


# ---------------------------------------------------------------------------
# vector_search
# ---------------------------------------------------------------------------


class TestVectorSearch:
    def test_an_empty_store_returns_an_empty_list(self, vectors):
        assert vectors.vector_search("что угодно") == []

    def test_scores_are_the_measured_cosines(self, vectors):
        db.embedding_upsert("fact", "future", [_at_cosine(RELEVANT_FUTURE)],
                            model=MODEL, dim=DIM)

        hits = vectors.vector_search("запрос")

        assert hits[0][0] == "fact" and hits[0][1] == "future"
        assert hits[0][2] == pytest.approx(RELEVANT_FUTURE, abs=1e-3)

    def test_the_max_chunk_wins_not_the_mean(self, vectors):
        """The `ПРАВИЛО НА БУДУЩЕЕ` case, and the whole reason for chunking.

        A long fact whose LAST chunk is the relevant one must outrank a short
        row that matches weakly. Averaging its six irrelevant chunks in would
        bury it below the noise.
        """
        long_row = [_at_cosine(CONTROL_DNS)] * 6 + [_at_cosine(RELEVANT_FUTURE)]
        db.embedding_upsert("fact", "long-lesson", long_row, model=MODEL, dim=DIM)
        db.embedding_upsert("fact", "short-weak", [_at_cosine(CONTROL_TELEGRAM)],
                            model=MODEL, dim=DIM)

        hits = vectors.vector_search("запрос")

        assert [h[1] for h in hits] == ["long-lesson", "short-weak"]
        assert hits[0][2] == pytest.approx(RELEVANT_FUTURE, abs=1e-3)

    def test_a_key_appears_once_however_many_chunks_it_has(self, vectors):
        db.embedding_upsert("fact", "k", [_at_cosine(RELEVANT_FUTURE)] * 5,
                            model=MODEL, dim=DIM)

        assert len(vectors.vector_search("запрос")) == 1

    def test_rows_from_a_different_model_are_ignored(self, vectors):
        """A model change must be detectable, never silently mixed."""
        db.embedding_upsert("fact", "stale", [_at_cosine(RELEVANT_FUTURE)],
                            model="some-older-model-v1", dim=DIM)

        assert vectors.vector_search("запрос") == []

    def test_kind_filtering(self, vectors):
        db.embedding_upsert("fact", "f", [_at_cosine(RELEVANT_FUTURE)],
                            model=MODEL, dim=DIM)
        db.embedding_upsert("causal", "c", [_at_cosine(RELEVANT_PARAPHRASE)],
                            model=MODEL, dim=DIM)

        assert [h[1] for h in vectors.vector_search("q", kind="causal")] == ["c"]
        assert [h[1] for h in vectors.vector_search("q", kind="fact")] == ["f"]

    def test_min_score_separates_relevant_from_controls(self, vectors):
        """The owner's measured spread: 0.55-0.72 relevant, 0.04-0.17 noise."""
        for key, cos in [
            ("paraphrase", RELEVANT_PARAPHRASE),
            ("inflection", RELEVANT_INFLECTION),
            ("future", RELEVANT_FUTURE),
            ("dns-control", CONTROL_DNS),
            ("telegram-control", CONTROL_TELEGRAM),
        ]:
            db.embedding_upsert("fact", key, [_at_cosine(cos)], model=MODEL, dim=DIM)

        kept = {h[1] for h in vectors.vector_search("запрос", min_score=0.35)}

        assert kept == {"paraphrase", "inflection", "future"}

    def test_n_results_caps_the_output(self, vectors):
        for i in range(10):
            db.embedding_upsert("fact", f"k{i}", [_at_cosine(RELEVANT_FUTURE)],
                                model=MODEL, dim=DIM)

        assert len(vectors.vector_search("запрос", n_results=3)) == 3

    def test_results_are_sorted_by_descending_score(self, vectors):
        db.embedding_upsert("fact", "mid", [_at_cosine(RELEVANT_INFLECTION)],
                            model=MODEL, dim=DIM)
        db.embedding_upsert("fact", "top", [_at_cosine(RELEVANT_FUTURE)],
                            model=MODEL, dim=DIM)
        db.embedding_upsert("fact", "low", [_at_cosine(RELEVANT_PARAPHRASE)],
                            model=MODEL, dim=DIM)

        assert [h[1] for h in vectors.vector_search("запрос")] == ["top", "mid", "low"]

    def test_a_write_invalidates_the_cache(self, vectors):
        db.embedding_upsert("fact", "first", [_at_cosine(RELEVANT_FUTURE)],
                            model=MODEL, dim=DIM)
        assert len(vectors.vector_search("запрос")) == 1

        db.embedding_upsert("fact", "second", [_at_cosine(RELEVANT_INFLECTION)],
                            model=MODEL, dim=DIM)
        vectors._invalidate_vector_cache()

        assert len(vectors.vector_search("запрос")) == 2


# ---------------------------------------------------------------------------
# Backfill: the semantic corpus is the UNION of two disjoint stores
# ---------------------------------------------------------------------------


def _make_chroma_sqlite(path, docs: list[tuple[str, str]]) -> None:
    """A minimal chroma.sqlite3 carrying just the column the backfill reads."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute(
            "CREATE TABLE embedding_metadata "
            "(id TEXT, key TEXT, string_value TEXT)"
        )
        conn.executemany(
            "INSERT INTO embedding_metadata (id, key, string_value) "
            "VALUES (?, 'chroma:document', ?)",
            docs,
        )
    finally:
        conn.close()


@pytest.fixture
def backfill_env(store, tmp_path, monkeypatch):
    """`backfill_embeddings` with both semantic stores pointed at tmp_path."""
    import backfill_embeddings as bf

    monkeypatch.setattr(server, "CHROMA_SQLITE", tmp_path / "chroma" / "chroma.sqlite3")
    monkeypatch.setattr(server, "NOTES_DIR", tmp_path / "notes")
    (tmp_path / "notes").mkdir(parents=True, exist_ok=True)
    return bf


class TestBackfillSemanticUnion:
    """Chroma holds what was written before safe-mode, the fallback everything
    since. Measured live: 682 and 615 rows, ZERO overlap, union 1297. Reading
    only Chroma silently drops the newer half.
    """

    def test_chroma_only(self, backfill_env, tmp_path):
        _make_chroma_sqlite(tmp_path / "chroma" / "chroma.sqlite3", [("c1", "старая запись")])

        assert backfill_env._semantic_union() == [("c1", "старая запись")]

    def test_fallback_only(self, backfill_env):
        db.semantic_set("f1", "новая запись", {})

        assert backfill_env._semantic_union() == [("f1", "новая запись")]

    def test_both_sources_are_unioned(self, backfill_env, tmp_path):
        _make_chroma_sqlite(tmp_path / "chroma" / "chroma.sqlite3", [("c1", "старая")])
        db.semantic_set("f1", "новая", {})

        assert backfill_env._semantic_union() == [("c1", "старая"), ("f1", "новая")]

    def test_neither_source_is_safe(self, backfill_env):
        assert backfill_env._semantic_union() == []

    def test_an_id_in_both_is_not_duplicated_and_the_newer_wins(
        self, backfill_env, tmp_path
    ):
        """They are disjoint today; the code must not depend on that holding."""
        _make_chroma_sqlite(tmp_path / "chroma" / "chroma.sqlite3", [("x", "старый текст")])
        db.semantic_set("x", "новый текст", {})

        assert backfill_env._semantic_union() == [("x", "новый текст")]

    def test_rerunning_repairs_rather_than_duplicates(self, backfill_env, monkeypatch):
        """A store already holding chroma-only vectors gains the missing half,
        and the rows it already had are not written twice."""
        monkeypatch.setattr(server, "EMBED_MODEL_NAME", MODEL)
        monkeypatch.setattr(
            server, "embed_chunks", lambda text: [_at_cosine(0.5)] * 2
        )
        db.semantic_set("f1", "новая запись", {})

        first = backfill_env.backfill("semantic", force=False)
        rows_after_first, _ = db.embedding_load()
        second = backfill_env.backfill("semantic", force=False)
        rows_after_second, _ = db.embedding_load()

        assert first == (1, 2)
        assert second == (0, 0)
        assert len(rows_after_first) == len(rows_after_second) == 2
