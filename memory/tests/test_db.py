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


def _belief_count() -> int:
    return int(db.get_db().execute("SELECT COUNT(*) FROM belief_state").fetchone()[0])


def _belief_row(id: str) -> dict:
    return dict(db.get_db().execute(
        "SELECT * FROM belief_state WHERE id = ?", (id,)
    ).fetchone())


def test_belief_assert_is_repeatable_with_the_same_id():
    """Re-asserting the incumbent's own id updates it in place, never collides.

    The old code superseded the incumbent by ITSELF (superseded_by = its own id)
    and then re-inserted that same primary key, so the second call to a public
    MCP tool whose id is documented as reusable raised IntegrityError.
    """
    for value in ("jest", "vitest", "vitest", "bun"):
        db.belief_assert(
            id="b1",
            subject="user.preferences",
            predicate="test_runner",
            object_val=value,
            confidence=0.8,
        )

    active = db.belief_get_active("user.preferences", "test_runner")
    assert active is not None
    assert active["id"] == "b1"
    assert active["object"] == "bun"
    assert active["status"] == "active"
    assert active["superseded_by"] is None
    assert active["valid_to"] is None
    assert _belief_count() == 1


def test_belief_assert_returns_the_key_it_landed_on():
    assert db.belief_assert(id="b1", subject="s", predicate="p", object_val="x") == "b1"
    assert db.belief_assert(id="b1", subject="s", predicate="p", object_val="y") == "b1"


def test_belief_assert_reuses_an_id_taken_by_a_superseded_version():
    """An id parked in history must not be overwritten NOR collide."""
    db.belief_assert(id="b1", subject="s", predicate="p", object_val="jest")
    db.belief_assert(id="b2", subject="s", predicate="p", object_val="vitest")

    landed = db.belief_assert(id="b1", subject="s", predicate="p", object_val="mocha")

    assert landed != "b1"
    active = db.belief_get_active("s", "p")
    assert active["id"] == landed
    assert active["object"] == "mocha"
    assert active["supersedes"] == "b2"
    # Both older versions survive, dated and back-linked.
    assert _belief_row("b1")["status"] == "superseded"
    assert _belief_row("b1")["superseded_by"] == "b2"
    assert _belief_row("b2")["status"] == "superseded"
    assert _belief_row("b2")["superseded_by"] == landed
    assert _belief_count() == 3


def test_belief_assert_reuses_an_id_across_subjects():
    """The same id on a different subject must not clobber the first belief."""
    db.belief_assert(id="shared", subject="a", predicate="p", object_val="x")
    landed = db.belief_assert(id="shared", subject="b", predicate="p", object_val="y")

    assert landed != "shared"
    assert db.belief_get_active("a", "p")["id"] == "shared"
    assert db.belief_get_active("b", "p")["id"] == landed
    assert _belief_count() == 2


def test_belief_assert_survives_a_long_flip_flop_on_one_id():
    """Hammering one id across contradicting values must never raise."""
    for i in range(20):
        db.belief_assert(
            id="b1",
            subject="s",
            predicate="p",
            object_val="A" if i % 2 == 0 else "B",
        )
    active = db.belief_get_active("s", "p")
    assert active["id"] == "b1"
    assert active["object"] == "B"
    assert _belief_count() == 1


# ---------------------------------------------------------------------------
# Migration 4 — repair of the pairs this project's own observer fabricated
# ---------------------------------------------------------------------------


def _user_version() -> int:
    return int(db.get_db().execute("PRAGMA user_version").fetchone()[0])


def _causal(id: str) -> dict:
    return dict(db.get_db().execute(
        "SELECT * FROM causal_memories WHERE id = ?", (id,)
    ).fetchone())


def _seed_v3_store(path: Path) -> None:
    """A database frozen at schema version 3, holding what the old hook wrote.

    Built through a raw connection so ``db`` never sees it before the test asks
    it to — opening it via ``set_db_path`` is what triggers migration 4.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for version in (1, 2, 3):
            conn.executescript(db.MIGRATIONS[version])
        conn.execute("PRAGMA user_version = 3")

        rows = [
            # (id, layer, cause, effect, tags)
            ("l0_tool_err", 0, "tool_call:Bash", "tool_error",
             "observer,post-tool,tool-error,tool:Bash"),
            ("l0_tool_err_other", 0, "tool_call:Read", "tool_error",
             "observer,post-tool,tool-error,tool:Read"),
            ("l0_summary", 0, "session_end", "session_summary",
             "observer,session-end,session-summary"),
            ("l0_friction", 0, "user_message", "hard_rejection",
             "observer,session-end,friction,hard_rejection"),
            # A real pair, hand-written or recovered by pass1. Must survive.
            ("l0_legit", 0, "the lockfile was stale", "build failed",
             "observer,session-end,friction"),
            # Delimiter bait: a real observation about a retry that worked.
            ("l0_recovered", 0, "the retry succeeded", "build passed",
             "observer,post-tool,tool-error-recovered"),
            # The second-order case: a synthesized parent that INHERITED the
            # tautology. Its tags collapsed to the dominant one, so nothing
            # keyed on tags would ever reach it.
            ("l1_inherited", 1, "tool_call:Bash", "tool_error", "observer"),
            ("l2_inherited", 2, "session_end", "session_summary", "observer"),
        ]
        for id_, layer, cause, effect, tags in rows:
            conn.execute(
                "INSERT INTO causal_memories "
                "(id, text, layer, cause, effect, observed_at, recorded_at, tags) "
                "VALUES (?, ?, ?, ?, ?, '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00', ?)",
                (id_, f"text of {id_}", layer, cause, effect, tags),
            )

        beliefs = [
            ("b_tool_bash", "tool_call_bash", "causes", "tool_error", "dream"),
            ("b_tool_read", "tool_call_read", "causes", "tool_error", "dream"),
            ("b_session", "session_end", "causes", "session_summary", "dream"),
            ("b_friction", "user_message", "causes", "hard_rejection", "dream"),
            # A belief a human asserted. Must survive.
            ("b_user", "user.ui.icon_system", "uses", "lucide", "user"),
            # A dream belief about something real. `source` is NOT the test.
            ("b_real", "stale_lock", "causes", "build fails", "dream"),
        ]
        for id_, subject, predicate, object_val, source in beliefs:
            conn.execute(
                "INSERT INTO belief_state "
                "(id, subject, predicate, object, valid_from, recorded_at, source) "
                "VALUES (?, ?, ?, ?, '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00', ?)",
                (id_, subject, predicate, object_val, source),
            )
    finally:
        conn.close()


@pytest.fixture
def migrated_from_v3(tmp_path):
    """A v3 store, opened through ``db`` so migration 4 runs against it."""
    path = tmp_path / "v3.sqlite"
    _seed_v3_store(path)
    db.set_db_path(path)
    yield path
    db.close_db()


class TestMigration4:
    """Migration 4 nulls the causal pairs the observer used to invent.

    The code fix stops new rows carrying them; it cannot reach the ones already
    written. Pass 3 reads rows, not source, so a store carrying the backlog goes
    on minting ``tool_call_bash causes tool_error`` forever. This is a defect
    this project shipped, so it is repaired where every installation will get
    it, not in one terminal.
    """

    def test_fabricated_pairs_are_nulled_and_real_ones_survive(self, migrated_from_v3):
        assert (_causal("l0_tool_err")["cause"], _causal("l0_tool_err")["effect"]) == (None, None)
        assert (_causal("l0_summary")["cause"], _causal("l0_summary")["effect"]) == (None, None)

        # Friction keeps its classification; only the invented cause goes.
        friction = _causal("l0_friction")
        assert friction["cause"] is None
        assert friction["effect"] == "hard_rejection"

        legit = _causal("l0_legit")
        assert (legit["cause"], legit["effect"]) == ("the lockfile was stale", "build failed")

        recovered = _causal("l0_recovered")
        assert (recovered["cause"], recovered["effect"]) == ("the retry succeeded", "build passed")

    def test_an_inherited_pair_is_nulled_at_every_layer(self, migrated_from_v3):
        """pass3_reconcile selects ``layer >= 1``, so a synthesized parent that
        inherited the tautology re-mints the belief after it is deleted."""
        assert (_causal("l1_inherited")["cause"], _causal("l1_inherited")["effect"]) == (None, None)
        assert (_causal("l2_inherited")["cause"], _causal("l2_inherited")["effect"]) == (None, None)

    def test_the_parent_rows_themselves_are_kept(self, migrated_from_v3):
        """Inert, not deleted — a stranger's consolidated history is not ours."""
        assert _causal("l1_inherited")["text"] == "text of l1_inherited"
        assert _causal("l2_inherited")["layer"] == 2

    def test_the_fabricated_beliefs_are_deleted(self, migrated_from_v3):
        for gone in ("tool_call_bash", "tool_call_read", "session_end", "user_message"):
            assert db.belief_get_active(gone, "causes") is None

    def test_beliefs_that_were_never_fabricated_survive(self, migrated_from_v3):
        assert db.belief_get_active("user.ui.icon_system", "uses")["object"] == "lucide"
        assert db.belief_get_active("stale_lock", "causes")["object"] == "build fails"
        assert _belief_count() == 2

    def test_the_schema_version_is_advanced(self, migrated_from_v3):
        """Past 4, and on to whatever the newest migration is."""
        assert _user_version() >= 4
        assert _user_version() == max(db.MIGRATIONS)

    def test_running_it_again_changes_nothing(self, migrated_from_v3):
        def snapshot() -> tuple[list, list, int]:
            conn = db.get_db()
            return (
                [tuple(r) for r in conn.execute(
                    "SELECT * FROM causal_memories ORDER BY id").fetchall()],
                [tuple(r) for r in conn.execute(
                    "SELECT * FROM belief_state ORDER BY id").fetchall()],
                _user_version(),
            )

        before = snapshot()
        db.init_schema()
        db.init_schema()
        assert snapshot() == before

    def test_a_fresh_database_initialises_straight_to_the_newest(self, tmp_path):
        db.set_db_path(tmp_path / "fresh.sqlite")
        assert _user_version() == max(db.MIGRATIONS)
        assert db.fact_count() == 0


# ---------------------------------------------------------------------------
# causal_search — the causal layer had no text search at all until now
# ---------------------------------------------------------------------------


class TestCausalSearch:
    """Text search over the causal layer.

    `causal_query` filters by layer and session only, so nothing in the system
    could find a lesson by what it SAYS. `recall` never touched this table, and
    the layer was write-only in practice.
    """

    @staticmethod
    def _seed() -> None:
        db.causal_insert(
            id="2026-08-23-unmeasured-risk-stated-as-fact",
            text="ГИПОТЕЗА, НЕ ИЗМЕРЕНО: помечай риск, если он не проверен",
            cause="догадка выдана за факт",
            effect="потерянное доверие",
            confidence=0.9,
            tags="lesson,hand-written",
        )
        db.causal_insert(
            id="folded-1",
            text="машинная свёртка про риск",
            confidence=0.27,
            tags="observer,session-end,friction",
        )
        db.causal_insert(
            id="tel-1",
            text="tool `Bash` reported an error: помечай риск",
            confidence=0.3,
            tags="observer,post-tool,tool-error,tool:Bash",
        )
        db.causal_insert(
            id="tel-2",
            text="session s1 ended: риск",
            confidence=0.3,
            tags="observer,session-end,session-summary",
        )

    def test_finds_a_row_by_a_word_in_its_text(self):
        self._seed()
        assert "2026-08-23-unmeasured-risk-stated-as-fact" in {
            r["id"] for r in db.causal_search("помечай")
        }

    def test_finds_a_row_by_a_word_in_its_cause(self):
        self._seed()
        assert [r["id"] for r in db.causal_search("догадка")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact"
        ]

    def test_finds_a_row_by_a_word_in_its_effect(self):
        self._seed()
        assert [r["id"] for r in db.causal_search("доверие")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact"
        ]

    def test_finds_a_row_by_a_fragment_of_its_id(self):
        """Hand-authored ids are descriptive; they are a search surface."""
        self._seed()
        assert [r["id"] for r in db.causal_search("unmeasured-risk")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact"
        ]

    def test_cyrillic_is_matched_case_insensitively(self):
        """LIKE folds ASCII only, and this store is predominantly Russian."""
        self._seed()
        assert [r["id"] for r in db.causal_search("гипотеза")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact"
        ]
        assert [r["id"] for r in db.causal_search("ГиПоТеЗа")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact"
        ]

    def test_telemetry_never_occupies_a_retrieval_slot(self):
        """Their text matches; they still must not surface."""
        self._seed()
        found = {r["id"] for r in db.causal_search("риск")}
        assert "tel-1" not in found and "tel-2" not in found
        assert found == {"2026-08-23-unmeasured-risk-stated-as-fact", "folded-1"}

    def test_deliberate_knowledge_outranks_machine_residue(self):
        """Confidence is the honest signal: 0.9 hand-written vs 0.27 folded."""
        self._seed()
        assert [r["id"] for r in db.causal_search("риск")] == [
            "2026-08-23-unmeasured-risk-stated-as-fact",
            "folded-1",
        ]

    def test_a_query_matching_nothing_returns_nothing(self):
        self._seed()
        assert db.causal_search("совершенно посторонний запрос") == []

    def test_the_limit_is_honoured(self):
        for i in range(10):
            db.causal_insert(id=f"r{i}", text="повторяющийся текст", confidence=0.5)
        assert len(db.causal_search("повторяющийся", limit=3)) == 3


# ---------------------------------------------------------------------------
# embeddings — one row per CHUNK, migration 5
# ---------------------------------------------------------------------------

MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def _unit(*values: float) -> bytes:
    """A normalized float32 little-endian vector, as the table stores them."""
    import numpy as np

    a = np.asarray(values, dtype=np.float32)
    return (a / np.linalg.norm(a)).astype("<f4").tobytes()


class TestEmbeddingStorage:
    def test_migration_5_creates_the_table(self):
        assert _user_version() == 5

    def test_chunks_round_trip_byte_identical(self):
        chunks = [_unit(1, 0, 0, 0), _unit(0, 1, 0, 0)]
        db.embedding_upsert("fact", "k1", chunks, model=MODEL, dim=4)

        rows, matrix = db.embedding_load()

        assert [r["vec"] for r in rows] == chunks
        assert {r["dim"] for r in rows} == {4}
        assert [r["chunk_ix"] for r in rows] == [0, 1]
        assert matrix.shape == (2, 4)
        assert matrix.dtype.str == "<f4"

    def test_the_matrix_rows_align_with_the_metadata_rows(self):
        import numpy as np

        db.embedding_upsert("fact", "k1", [_unit(1, 0, 0, 0)], model=MODEL, dim=4)
        db.embedding_upsert("fact", "k2", [_unit(0, 0, 1, 0)], model=MODEL, dim=4)

        rows, matrix = db.embedding_load()

        for i, r in enumerate(rows):
            assert matrix[i].astype("<f4").tobytes() == r["vec"]
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0)

    def test_reupsert_replaces_chunks_rather_than_appending(self):
        """The idempotence guard: a re-run of the backfill must not grow rows."""
        db.embedding_upsert("fact", "k1", [_unit(1, 0, 0, 0)] * 3, model=MODEL, dim=4)
        db.embedding_upsert("fact", "k1", [_unit(0, 1, 0, 0)], model=MODEL, dim=4)

        rows, _ = db.embedding_load()

        assert len(rows) == 1
        assert rows[0]["vec"] == _unit(0, 1, 0, 0)

    def test_reupsert_does_not_strand_a_shortened_tail(self):
        """Re-embedding a shortened record must not leave orphan chunks behind."""
        db.embedding_upsert("fact", "k1", [_unit(1, 0, 0, 0)] * 5, model=MODEL, dim=4)
        db.embedding_upsert("fact", "k1", [_unit(1, 0, 0, 0)] * 2, model=MODEL, dim=4)

        rows, _ = db.embedding_load()

        assert [r["chunk_ix"] for r in rows] == [0, 1]

    def test_delete_removes_every_chunk_of_one_key_only(self):
        db.embedding_upsert("fact", "keep", [_unit(1, 0, 0, 0)], model=MODEL, dim=4)
        db.embedding_upsert("fact", "drop", [_unit(0, 1, 0, 0)] * 4, model=MODEL, dim=4)

        db.embedding_delete("fact", "drop")

        assert [r["key"] for r in db.embedding_load()[0]] == ["keep"]

    def test_load_can_be_filtered_by_kind(self):
        db.embedding_upsert("fact", "f", [_unit(1, 0, 0, 0)], model=MODEL, dim=4)
        db.embedding_upsert("causal", "c", [_unit(0, 1, 0, 0)], model=MODEL, dim=4)

        assert [r["key"] for r in db.embedding_load(kind="causal")[0]] == ["c"]

    def test_load_can_be_filtered_by_model(self):
        db.embedding_upsert("fact", "new", [_unit(1, 0, 0, 0)], model=MODEL, dim=4)
        db.embedding_upsert("fact", "old", [_unit(0, 1, 0, 0)], model="older-v1", dim=4)

        assert [r["key"] for r in db.embedding_load(model=MODEL)[0]] == ["new"]

    def test_the_model_name_is_recorded_per_row(self):
        db.embedding_upsert("fact", "k1", [_unit(1, 0, 0, 0)], model=MODEL, dim=4)

        assert db.embedding_load()[0][0]["model"] == MODEL

    def test_an_empty_table_loads_as_an_empty_result(self):
        rows, matrix = db.embedding_load()

        assert rows == []
        assert matrix.shape == (0, 0)
