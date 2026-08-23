"""Tests for `server.recall` — the cross-layer search tool.

`recall` is the tool the agent's protocol names first, and until now its
docstring opened with "Search ALL memory layers at once" while searching three
of five. The causal and belief layers were write-only in practice: a lesson
stored as a causal memory could not be found by anything the agent could call.

These tests pin the layers `recall` actually covers. They use the `srv` fixture
from conftest, which redirects every path into tmp_path and forces the SQLite
semantic fallback, so nothing here touches ~/.config/opencode/memory.
"""

import db

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
