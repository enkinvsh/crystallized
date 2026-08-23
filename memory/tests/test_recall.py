"""Tests for `server.recall` — the cross-layer search tool.

`recall` is the tool the agent's protocol names first, and until now its
docstring opened with "Search ALL memory layers at once" while searching three
of five. The causal and belief layers were write-only in practice: a lesson
stored as a causal memory could not be found by anything the agent could call.

These tests pin the layers `recall` actually covers. They use the `srv` fixture
from conftest, which redirects every path into tmp_path and forces the SQLite
semantic fallback, so nothing here touches ~/.config/opencode/memory.
"""

import numpy as np
import pytest

import db
import server

VEC_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
VEC_DIM = 8


def _basis(i: int) -> np.ndarray:
    v = np.zeros(VEC_DIM, dtype=np.float32)
    v[i] = 1.0
    return v


def _at_cosine(target: float) -> bytes:
    """A unit vector whose dot with the fixture query vector is `target`."""
    v = target * _basis(0) + np.sqrt(max(0.0, 1.0 - target * target)) * _basis(1)
    return (v / np.linalg.norm(v)).astype("<f4").tobytes()


class _CountingEncoder:
    """Returns one fixed query vector and counts how often it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, text, **_kwargs):
        self.calls += 1
        return _basis(0).copy()


@pytest.fixture
def vec(srv, monkeypatch):
    """`recall` with a stub encoder and an empty, isolated vector store."""
    encoder = _CountingEncoder()
    monkeypatch.setattr(server, "get_encoder", lambda: encoder)
    monkeypatch.setattr(server, "EMBED_MODEL_NAME", VEC_MODEL)
    server._invalidate_vector_cache()
    yield srv, encoder
    server._invalidate_vector_cache()


def _embed(kind: str, key: str, cosine: float) -> None:
    db.embedding_upsert(kind, key, [_at_cosine(cosine)], model=VEC_MODEL, dim=VEC_DIM)
    server._invalidate_vector_cache()

#: The live row, with the punctuation and the word forms it actually has.
#: A fixture that paraphrases the stored text flatters the matcher: «помечай»
#: hit a synthetic «помечай риск» and missed the real «помечать».
REAL_LESSON_TEXT = (
    "ГИПОТЕЗА, НЕ ИЗМЕРЕНО: помечать предполагаемый риск явно, "
    "пока он не проверен измерением"
)


def _seed_lesson() -> None:
    db.causal_insert(
        id="2026-08-23-unmeasured-risk-stated-as-fact",
        text=REAL_LESSON_TEXT,
        cause="догадка выдана за факт",
        effect="потерянное доверие",
        confidence=0.9,
        layer=1,
        tags="lesson,hand-written",
    )


class TestRecallReachesTheCausalLayer:
    def test_a_stored_lesson_is_findable_by_what_it_says(self, srv):
        """The end-to-end regression: this query used to return nothing."""
        _seed_lesson()

        out = srv.recall("предполагаемый риск")

        assert "Causal memories:" in out
        assert "2026-08-23-unmeasured-risk-stated-as-fact" in out

    def test_the_causal_line_carries_layer_confidence_and_id(self, srv):
        _seed_lesson()

        line = next(
            ln for ln in srv.recall("предполагаемый риск").splitlines()
            if "2026-08-23-unmeasured-risk-stated-as-fact" in ln
        )

        assert "L1" in line
        assert "0.9" in line
        assert "ГИПОТЕЗА" in line

    def test_telemetry_is_not_offered_as_a_recall_result(self, srv):
        db.causal_insert(
            id="tel-1",
            text="tool `Bash` reported an error: предполагаемый риск",
            confidence=0.3,
            tags="observer,post-tool,tool-error,tool:Bash",
        )

        assert "tel-1" not in srv.recall("предполагаемый риск")


class TestCausalMatchingIsAsStrongAsFacts:
    """Token overlap, not just contiguous substring.

    The causal block used to be matched by a strictly weaker rule than the
    facts block, which has always combined substring with token overlap. The
    live miss was «ГИПОТЕЗА НЕ ИЗМЕРЕНО риск» against a row storing
    «ГИПОТЕЗА, НЕ ИЗМЕРЕНО:» -- every token present, contiguity broken by one
    comma and one colon.
    """

    def test_punctuation_no_longer_breaks_the_match(self, srv):
        """The exact live miss, with the real punctuation."""
        _seed_lesson()

        assert "2026-08-23-unmeasured-risk-stated-as-fact" in srv.recall(
            "ГИПОТЕЗА НЕ ИЗМЕРЕНО риск"
        )

    def test_one_shared_token_is_below_threshold(self, srv):
        """Overlap has to clear `_overlap_threshold`, or every long row matches
        every query that happens to share one common word."""
        _seed_lesson()

        out = srv.recall("погода москва завтра дождь риск")

        assert "2026-08-23-unmeasured-risk-stated-as-fact" not in out

    def test_telemetry_stays_silent_under_strong_overlap(self, srv):
        """Every token of this query is in the row; it must still not surface."""
        db.causal_insert(
            id="tel-1",
            text="tool `Bash` reported an error: Traceback (most recent call last)",
            confidence=0.3,
            tags="observer,post-tool,tool-error,tool:Bash",
        )

        assert "tel-1" not in srv.recall("reported an error")

    def test_confidence_dominates_a_higher_overlap(self, srv):
        """A 0.27 row matching MORE tokens must not outrank a 0.90 lesson."""
        _seed_lesson()
        db.causal_insert(
            id="folded-residue",
            text="ГИПОТЕЗА НЕ ИЗМЕРЕНО риск измерением проверен явно",
            confidence=0.27,
            tags="observer,session-end,friction",
        )

        lines = [
            ln for ln in srv.recall("ГИПОТЕЗА НЕ ИЗМЕРЕНО риск").splitlines()
            if ln.startswith("  [L")
        ]

        assert "2026-08-23-unmeasured-risk-stated-as-fact" in lines[0]
        assert "folded-residue" in lines[1]

    def test_the_substring_hits_measured_live_still_hit(self, srv):
        """Regression on the three probes that already worked."""
        _seed_lesson()

        for probe in ("ГИПОТЕЗА", "unmeasured-risk", "предполагаемый"):
            assert "2026-08-23-unmeasured-risk-stated-as-fact" in srv.recall(probe), probe

    def test_DOCUMENTED_CEILING_inflection_is_not_matched(self, srv):
        """«помечай» does not find «помечать». This is a CEILING, not a bug.

        Neither substring nor token overlap can cross Russian morphology, and a
        hand-rolled stemmer here would half-work in the worst way. The fix is
        the multilingual embedding model already loaded in this server. When it
        lands this assertion flips, and that flip is the signal it worked.
        """
        _seed_lesson()

        assert "2026-08-23-unmeasured-risk-stated-as-fact" not in srv.recall("помечай")


class TestRecallReachesTheBeliefLayer:
    def test_an_active_belief_is_returned(self, srv):
        db.belief_assert(
            id="b1",
            subject="user.ui.icon_system",
            predicate="uses",
            object_val="lucide",
            source="user",
        )

        out = srv.recall("icon_system")

        assert "Beliefs:" in out
        assert "user.ui.icon_system" in out
        assert "lucide" in out

    def test_a_superseded_belief_is_not_returned(self, srv):
        db.belief_assert(
            id="b1", subject="project.testing", predicate="test_runner",
            object_val="jest",
        )
        db.belief_assert(
            id="b2", subject="project.testing", predicate="test_runner",
            object_val="vitest",
        )

        out = srv.recall("test_runner")

        assert "vitest" in out
        assert "jest" not in out


class TestRecallStillDoesEverythingItDidBefore:
    def test_facts_still_render(self, srv):
        db.fact_set("db_choice", {"value": "sqlite", "ttl_days": 90})

        out = srv.recall("db_choice")

        assert "Facts:" in out
        assert "sqlite" in out

    def test_documents_still_render(self, srv, tmp_path):
        (srv.NOTES_DIR / "architecture").mkdir(parents=True, exist_ok=True)
        (srv.NOTES_DIR / "architecture" / "storage.md").write_text(
            "the store is one sqlite file guarded by an RLock", "utf-8"
        )

        out = srv.recall("RLock")

        assert "Documents:" in out
        assert "architecture/storage" in out

    def test_semantic_fallback_still_renders(self, srv):
        srv.remember("the lockfile went stale and the build failed", tags="build")

        assert "Semantic memories:" in srv.recall("lockfile")

    def test_a_query_matching_nothing_takes_the_nothing_found_path(self, srv):
        _seed_lesson()

        out = srv.recall("совершенно посторонний запрос ни к чему")

        assert out.startswith("Nothing found across all memory layers")

    def test_one_broken_layer_cannot_suppress_the_others(self, srv, monkeypatch):
        """Each section keeps its own error boundary."""
        _seed_lesson()

        def boom(*_args, **_kwargs):
            raise RuntimeError("belief layer is down")

        monkeypatch.setattr(db, "belief_all_active", boom)

        out = srv.recall("предполагаемый риск")

        assert "Causal memories:" in out


# ===========================================================================
# Stage B — vector retrieval tops up every section
# ===========================================================================


class TestVectorTopsUpEachSection:
    """Vector hits are ADDED to each section, never substituted for it.

    Exact and id matching is precise and already works — «unmeasured-risk»
    found its row by an id fragment. Vectors add paraphrase and inflection
    reach on top. Searching per KIND rather than globally is what makes this
    usable: on the live store a document averages 30.2 chunks against a causal
    row's 1.9, so a global max-over-chunks hands verbose prose ~15x more
    chances to throw a high score than a precise lesson.
    """

    def test_a_paraphrase_only_lesson_is_reachable(self, vec):
        """The case that failed all day: no shared substring, no shared token."""
        srv, _ = vec
        db.causal_insert(
            id="2026-08-23-unmeasured-risk-stated-as-fact",
            text="ГИПОТЕЗА, НЕ ИЗМЕРЕНО: помечать предполагаемый риск явно",
            confidence=0.9, layer=2, tags="lesson",
        )
        _embed("causal", "2026-08-23-unmeasured-risk-stated-as-fact", 0.553)

        out = srv.recall("как отличать догадку от проверенного факта в документах")

        assert "Causal memories:" in out
        assert "2026-08-23-unmeasured-risk-stated-as-fact" in out

    def test_vector_lines_are_visibly_marked(self, vec):
        srv, _ = vec
        db.causal_insert(id="lesson", text="некий урок", confidence=0.9)
        _embed("causal", "lesson", 0.62)

        line = next(
            ln for ln in srv.recall("совершенно иной запрос").splitlines()
            if "lesson" in ln
        )

        assert "[vec" in line

    def test_an_exact_match_is_never_displaced_by_a_vector_hit(self, vec):
        """A vector hit may join the results; it may not evict a literal one."""
        srv, _ = vec
        db.fact_set("db_choice", {"value": "sqlite", "ttl_days": 90})
        for i in range(8):
            db.fact_set(f"filler{i}", {"value": "нерелевантно", "ttl_days": 90})
            _embed("fact", f"filler{i}", 0.99)

        out = srv.recall("db_choice")

        assert "db_choice" in out
        facts = next(s for s in out.split("\n\n") if s.startswith("Facts:"))
        assert "db_choice" in facts.splitlines()[1]  # still the first line

    def test_a_record_matching_both_ways_appears_once(self, vec):
        srv, _ = vec
        db.causal_insert(id="both", text="догадка выдана за факт", confidence=0.9)
        _embed("causal", "both", 0.9)

        out = srv.recall("догадка")

        assert out.count("both") == 1

    def test_a_document_cannot_surface_in_the_causal_section(self, vec):
        """Each section is searched with its OWN kind."""
        srv, _ = vec
        (srv.NOTES_DIR / "notes.md").write_text("многословная проза", "utf-8")
        _embed("doc", "notes", 0.99)
        db.causal_insert(id="lesson", text="точный урок", confidence=0.9)
        _embed("causal", "lesson", 0.40)

        out = srv.recall("запрос без общих слов")
        causal = next(s for s in out.split("\n\n") if s.startswith("Causal memories:"))

        assert "lesson" in causal
        assert "notes" not in causal
        assert "Documents:" in out

    def test_the_query_is_encoded_once_per_call(self, vec):
        """Four sections, one forward pass: the encode is ~95% of the cost."""
        srv, encoder = vec
        db.causal_insert(id="c", text="урок", confidence=0.9)
        _embed("causal", "c", 0.8)
        _embed("fact", "f", 0.8)
        _embed("doc", "d", 0.8)
        _embed("semantic", "s", 0.8)

        srv.recall("какой-то запрос")

        assert encoder.calls == 1

    def test_min_score_trims_below_threshold_hits(self, vec):
        srv, _ = vec
        db.causal_insert(id="near", text="близкий", confidence=0.9)
        db.causal_insert(id="far", text="далёкий", confidence=0.9)
        _embed("causal", "near", 0.62)
        _embed("causal", "far", 0.11)

        out = srv.recall("запрос без общих слов", min_score=0.35)

        assert "near" in out
        assert "far" not in out

    def test_an_empty_vector_store_changes_nothing(self, vec):
        """No-regression guard: with no embeddings, `recall` is exactly as before."""
        srv, _ = vec
        db.fact_set("db_choice", {"value": "sqlite", "ttl_days": 90})

        out = srv.recall("db_choice")

        assert "Facts:" in out and "sqlite" in out
        assert "[vec" not in out
