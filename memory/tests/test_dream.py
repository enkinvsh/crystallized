"""Tests for the consolidation daemon and the shared volume module.

Covers:
* `volume` — the power-law decay math, clamping, reinforcement, and the
  sleep() pass over facts/semantic/docs.
* `dream` — the flock single-writer guard, and all five consolidation passes
  (ingest, lossy compression, belief reconciliation, decay, forgetting).

Everything is hermetic: a temp SQLite file via `db.set_db_path`, no network,
no ChromaDB, no ~/.config/opencode.
"""

import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

MEMORY_DIR = Path(__file__).resolve().parent.parent
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import db  # noqa: E402
import dream  # noqa: E402
import observer  # noqa: E402
import volume  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.py at a throwaway SQLite file for the duration of a test."""
    path = tmp_path / "memory.db"
    monkeypatch.setenv("OPENCODE_MEMORY_DB", str(path))
    db.set_db_path(path)
    yield path
    db.close_db()


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


#: Default age of a seeded L0 row. Pass 2 refuses to fold a session until it has
#: been silent for ``SESSION_QUIET_HOURS``, so a row seeded "just now" belongs to
#: a session that is still live and is deliberately left alone. Tests about
#: grouping mean "a session that has ended", which is what this expresses; tests
#: about the wait itself pass ``days_ago`` explicitly.
SETTLED_DAYS_AGO = 0.5


def _add_l0(mid: str, text: str, *, session: str | None = "s1",
            days_ago: float = SETTLED_DAYS_AGO,
            cause: str | None = None, effect: str | None = None,
            confidence: float = 0.5, tags: str = ""):
    db.causal_insert(
        id=mid,
        text=text,
        layer=0,
        cause=cause,
        effect=effect,
        confidence=confidence,
        session_id=session,
        observed_at=_iso(days_ago),
        tags=tags,
    )


def _row(mid):
    return db.causal_get(mid)


# ===========================================================================
# volume.py — decay mathematics
# ===========================================================================


class TestDecayMath:
    def test_decay_at_t_zero_returns_input(self):
        assert math.isclose(volume.decay_volume(50.0, 0.0, "fact"), 50.0, rel_tol=1e-9)

    def test_negative_age_is_treated_as_zero(self):
        # Clock skew must not AMPLIFY a volume.
        assert volume.decay_volume(50.0, -100.0, "fact") == 50.0

    def test_monotonic_decreasing(self):
        v1 = volume.decay_volume(50.0, 24.0, "fact")
        v2 = volume.decay_volume(50.0, 240.0, "fact")
        assert v2 < v1 < 50.0

    def test_respects_floor(self):
        assert volume.decay_volume(50.0, 1e12, "fact") >= volume.MIN_VOLUME

    def test_layers_differ(self):
        # Docs are tuned to decay slower than facts at the same elapsed time.
        assert volume.decay_volume(60.0, 720.0, "doc") > volume.decay_volume(
            60.0, 720.0, "fact"
        )

    def test_matches_closed_form(self):
        expected = 50.0 * (1 + 100.0 / volume.DECAY_TAU["fact"]) ** (
            -volume.DECAY_ALPHA["fact"]
        )
        assert math.isclose(volume.decay_volume(50.0, 100.0, "fact"), expected)

    def test_unknown_layer_uses_fallback_constants(self):
        expected = 50.0 * (1 + 100.0 / volume.FALLBACK_TAU) ** (-volume.FALLBACK_ALPHA)
        assert math.isclose(volume.decay_volume(50.0, 100.0, "causal"), expected)

    def test_power_law_tail_is_flatter_than_exponential(self):
        """Ten times the age must cost far less than ten times the decay."""
        near = 50.0 - volume.decay_volume(50.0, 72.0, "fact")
        far = 50.0 - volume.decay_volume(50.0, 720.0, "fact")
        assert far < near * 10


class TestDecayedFromTimestamp:
    def test_missing_timestamp_leaves_volume_untouched(self):
        assert volume.decayed(50.0, "fact", None) == 50.0
        assert volume.decayed(50.0, "fact", "") == 50.0

    def test_unparseable_timestamp_leaves_volume_untouched(self):
        assert volume.decayed(50.0, "fact", "not-a-date") == 50.0

    def test_old_timestamp_decays(self):
        assert volume.decayed(50.0, "fact", _iso(days_ago=30)) < 50.0

    def test_aware_and_naive_agree(self):
        """A tz-aware stamp must not read as age=0 against a naive clock."""
        aware = datetime.now(UTC) - timedelta(days=10)
        naive = aware.astimezone().replace(tzinfo=None)
        assert math.isclose(
            volume.decayed(50.0, "fact", aware.isoformat()),
            volume.decayed(50.0, "fact", naive.isoformat()),
            rel_tol=1e-6,
        )


class TestVolumeHelpers:
    def test_zset_key(self):
        assert volume.zset_key("fact", "user_name") == "fact:user_name"

    def test_clamp(self):
        assert volume.clamp(-5.0) == volume.MIN_VOLUME
        assert volume.clamp(1e9) == volume.MAX_VOLUME
        assert volume.clamp(42.0) == 42.0

    def test_default_for_known_and_unknown_layers(self):
        assert volume.default_for("doc") == volume.DEFAULT_VOLUME["doc"]
        assert volume.default_for("nope") == volume.FALLBACK_VOLUME

    def test_get_volume_falls_back_to_default(self, temp_db):
        assert volume.get_volume("fact", "absent") == volume.DEFAULT_VOLUME["fact"]

    def test_set_then_get_roundtrip(self, temp_db):
        volume.set_volume("fact", "k", 33.0)
        assert volume.get_volume("fact", "k") == 33.0

    def test_set_volume_clamps(self, temp_db):
        volume.set_volume("fact", "k", 5_000.0)
        assert volume.get_volume("fact", "k") == volume.MAX_VOLUME

    def test_bulk_matches_singular_and_survives_gaps(self, temp_db):
        """Absent keys must not shift the results of the keys that follow."""
        volume.set_volume("fact", "b", 20.0)
        ids = ["a", "b", "c"]
        stamps = [None, None, None]
        bulk = volume.bulk_effective_volumes("fact", ids, stamps)
        singular = [volume.effective_volume("fact", i, s) for i, s in zip(ids, stamps)]
        assert bulk == singular
        assert bulk[1] == 20.0

    def test_bulk_on_empty_input(self, temp_db):
        assert volume.bulk_effective_volumes("fact", [], []) == []


class TestReinforce:
    def test_boost_is_headroom_scaled(self, temp_db):
        volume.set_volume("fact", "quiet", 10.0)
        volume.set_volume("fact", "loud", 90.0)
        quiet_gain = volume.reinforce("fact", "quiet") - 10.0
        loud_gain = volume.reinforce("fact", "loud") - 90.0
        assert quiet_gain > loud_gain > 0

    def test_never_exceeds_max(self, temp_db):
        volume.set_volume("fact", "k", volume.MAX_VOLUME)
        assert volume.reinforce("fact", "k") <= volume.MAX_VOLUME

    def test_persists_the_new_volume(self, temp_db):
        volume.set_volume("fact", "k", 20.0)
        new = volume.reinforce("fact", "k")
        assert volume.get_volume("fact", "k") == pytest.approx(new)

    def test_timestamp_aware_boost_starts_from_decayed_value(self, temp_db):
        """Reinforcement must not erase accrued decay."""
        volume.set_volume("fact", "k", 50.0)
        with_clock = volume.reinforce("fact", "k", last_reinforced_at=_iso(days_ago=90))
        volume.set_volume("fact", "k", 50.0)
        without_clock = volume.reinforce("fact", "k")
        assert with_clock < without_clock

    def test_logs_an_event_when_a_sink_is_given(self, temp_db):
        seen = []
        volume.reinforce(
            "fact", "k", log_event=lambda *a, **kw: seen.append((a, kw))
        )
        assert seen and seen[0][0][1] == "recall"


# ===========================================================================
# volume.sleep() — the decay pass
# ===========================================================================


class TestSleep:
    def test_empty_database_is_a_noop(self, temp_db):
        assert volume.sleep() == {
            "fact": 0, "semantic": 0, "doc": 0, "total_decayed": 0
        }

    def test_decays_a_stale_fact_and_resets_its_clock(self, temp_db):
        db.fact_set("old", {"value": "v", "last_reinforced_at": _iso(days_ago=120)})
        volume.set_volume("fact", "old", 80.0)

        stats = volume.sleep()

        assert stats["fact"] == 1
        assert stats["total_decayed"] == 1
        assert volume.get_volume("fact", "old") < 80.0
        # Clock reset: the decay is now materialized, not re-applied forever.
        assert db.fact_get("old")["last_reinforced_at"] > _iso(days_ago=1)

    def test_second_pass_is_stable(self, temp_db):
        db.fact_set("old", {"value": "v", "last_reinforced_at": _iso(days_ago=120)})
        volume.set_volume("fact", "old", 80.0)
        volume.sleep()
        after_first = volume.get_volume("fact", "old")
        assert volume.sleep()["fact"] == 0
        assert volume.get_volume("fact", "old") == after_first

    def test_fresh_fact_is_left_alone(self, temp_db):
        db.fact_set("new", {"value": "v", "last_reinforced_at": _iso(days_ago=0)})
        volume.set_volume("fact", "new", 80.0)
        assert volume.sleep()["fact"] == 0
        assert volume.get_volume("fact", "new") == 80.0

    def test_never_drops_below_floor(self, temp_db):
        db.fact_set("ancient", {"value": "v", "last_reinforced_at": "1970-01-01T00:00:00"})
        volume.set_volume("fact", "ancient", volume.MIN_VOLUME)
        volume.sleep()
        assert volume.get_volume("fact", "ancient") >= volume.MIN_VOLUME

    def test_decays_semantic_fallback_rows(self, temp_db):
        db.semantic_set("d1", "text", {"last_reinforced_at": _iso(days_ago=365)})
        volume.set_volume("semantic", "d1", 80.0)
        assert volume.sleep()["semantic"] == 1
        assert volume.get_volume("semantic", "d1") < 80.0
        assert db.semantic_get("d1")["metadata"]["last_reinforced_at"] > _iso(days_ago=1)

    def test_readonly_semantic_clock_lives_in_the_store(self, temp_db):
        """Rows we cannot write back to keep their clock in semantic_reinforced."""
        volume.set_volume("semantic", "ro1", 80.0)
        rows = [("ro1", "", {"last_reinforced_at": _iso(days_ago=365)})]

        stats = volume.sleep(readonly_semantic=lambda: rows)

        assert stats["semantic"] == 1
        assert db.semantic_reinforced_get("ro1") is not None
        # The provider's own dict is never mutated — it is not ours to write.
        assert rows[0][2]["last_reinforced_at"] < _iso(days_ago=300)

    def test_decays_docs_via_doc_reinforced_clock(self, temp_db):
        volume.set_volume("doc", "arch/notes", 90.0)
        db.doc_reinforced_set("doc:arch/notes", _iso(days_ago=900))
        assert volume.sleep()["doc"] == 1
        assert volume.get_volume("doc", "arch/notes") < 90.0

    def test_a_failing_layer_does_not_sink_the_others(self, temp_db, monkeypatch):
        db.fact_set("f", {"value": "v", "last_reinforced_at": _iso(days_ago=120)})
        volume.set_volume("fact", "f", 80.0)

        def boom():
            raise RuntimeError("semantic provider exploded")

        stats = volume.sleep(writable_semantic=boom)
        assert stats["fact"] == 1
        assert stats["semantic"] == 0

    def test_report_is_human_readable(self):
        text = volume.format_sleep_report(
            {"fact": 1, "semantic": 2, "doc": 3, "total_decayed": 6}
        )
        assert "1 facts" in text and "6 total" in text


class TestSleepDoesNotCompound:
    """D5/D6: a power law does not compose, so nightly passes must telescope."""

    LAYERS = ("fact", "semantic", "doc")

    def _seed(self, layer: str, key: str, origin: datetime, stored: float) -> None:
        """Place an entity on the curve as if freshly reinforced at ``origin``."""
        db.decay_anchor_delete(volume.zset_key(layer, key))
        volume.set_volume(layer, key, stored)
        if layer == "fact":
            db.fact_set(key, {"value": "v", "last_reinforced_at": origin.isoformat()})
        elif layer == "semantic":
            db.semantic_set(key, "text", {"last_reinforced_at": origin.isoformat()})
        else:
            db.doc_reinforced_set(volume.zset_key("doc", key), origin.isoformat())

    @pytest.mark.parametrize("layer", LAYERS)
    def test_thirty_nightly_passes_match_the_closed_form(self, temp_db, layer):
        origin = datetime.now() - timedelta(days=30)
        stored = 80.0
        self._seed(layer, "k", origin, stored)

        for day in range(1, 31):
            volume.sleep(now=origin + timedelta(days=day))

        expected = volume.decay_volume(stored, 30 * 24.0, layer)
        assert volume.get_volume(layer, "k") == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize("layer", LAYERS)
    def test_many_small_passes_equal_one_big_pass(self, temp_db, layer):
        origin = datetime.now() - timedelta(days=30)
        end = origin + timedelta(days=30)
        self._seed(layer, "nightly", origin, 80.0)
        self._seed(layer, "once", origin, 80.0)

        for day in range(1, 31):
            volume.sleep(now=origin + timedelta(days=day))
        # 'once' rode along every pass; re-seed it to measure a single jump.
        self._seed(layer, "once", origin, 80.0)
        volume.sleep(now=end)

        assert volume.get_volume(layer, "nightly") == pytest.approx(
            volume.get_volume(layer, "once"), rel=1e-9
        )

    def test_nightly_decay_stays_far_above_the_compounding_curve(self, temp_db):
        """The old bug forgot ~2.5x too much over a month. Pin that it is gone."""
        origin = datetime.now() - timedelta(days=30)
        self._seed("fact", "k", origin, 50.0)
        for day in range(1, 31):
            volume.sleep(now=origin + timedelta(days=day))

        compounding = 50.0
        for _ in range(30):
            compounding = volume.decay_volume(compounding, 24.0, "fact")

        assert volume.get_volume("fact", "k") > compounding * 2

    def test_repeating_a_pass_at_the_same_instant_is_a_noop(self, temp_db):
        """D6: the `now` seam must be measured against, not just stamped."""
        origin = datetime.now() - timedelta(days=30)
        self._seed("fact", "k", origin, 50.0)
        fixed = origin + timedelta(days=30)

        assert volume.sleep(now=fixed)["fact"] == 1
        after_first = volume.get_volume("fact", "k")
        assert volume.sleep(now=fixed)["fact"] == 0
        assert volume.sleep(now=fixed)["fact"] == 0
        assert volume.get_volume("fact", "k") == after_first

    def test_reinforcement_restarts_the_curve(self, temp_db):
        """A genuine touch voids the accumulated age, not just the volume."""
        origin = datetime.now() - timedelta(days=30)
        self._seed("fact", "k", origin, 50.0)
        volume.sleep(now=origin + timedelta(days=30))

        volume.reinforce("fact", "k")
        assert db.decay_anchor_map().get(volume.zset_key("fact", "k")) is None


def test_server_reuses_the_shared_module(temp_db):
    """server.py must delegate, not duplicate — one decay curve, one source."""
    server = pytest.importorskip("server")
    assert server.MIN_VOLUME is volume.MIN_VOLUME
    assert server.DECAY_ALPHA is volume.DECAY_ALPHA
    assert server.DECAY_TAU is volume.DECAY_TAU
    assert server.DEFAULT_VOLUME is volume.DEFAULT_VOLUME
    assert server._decayed is volume.decayed
    assert server._zset_key is volume.zset_key
    assert server._effective_volume is volume.effective_volume


# ===========================================================================
# dream.py — single-writer lock
# ===========================================================================


class TestSingleWriter:
    def test_lock_path_sits_beside_the_database(self, temp_db):
        assert dream.lock_path().name == "memory.db-dream.lock"
        assert dream.lock_path().parent == temp_db.parent

    def test_lock_file_records_the_holder(self, temp_db):
        with dream.single_writer() as path:
            assert str(os.getpid()) in path.read_text()

    def test_second_holder_is_refused(self, temp_db):
        with dream.single_writer():
            with pytest.raises(dream.DreamLockBusy):
                with dream.single_writer():
                    pass

    def test_lock_is_released_on_exit(self, temp_db):
        with dream.single_writer():
            pass
        with dream.single_writer():  # must not raise
            pass

    def test_lock_is_released_when_the_body_raises(self, temp_db):
        with pytest.raises(ValueError):
            with dream.single_writer():
                raise ValueError("boom")
        with dream.single_writer():
            pass


# ===========================================================================
# Pass 1 — ingest
# ===========================================================================


class TestExtractCauseEffect:
    @pytest.mark.parametrize(
        "text,cause,effect",
        [
            ("build failed because the lockfile was stale",
             "the lockfile was stale", "build failed"),
            ("stale lockfile -> build failed", "stale lockfile", "build failed"),
            ("cache was cold so the first run was slow",
             "cache was cold", "the first run was slow"),
            ("сборка упала потому что кеш протух",
             "кеш протух", "сборка упала"),
            ("кеш протух поэтому сборка упала",
             "кеш протух", "сборка упала"),
        ],
    )
    def test_recovers_pairs(self, text, cause, effect):
        assert dream.extract_cause_effect(text) == (cause, effect)

    @pytest.mark.parametrize("text", ["", "   ", "just a plain observation"])
    def test_returns_none_when_there_is_no_causal_shape(self, text):
        assert dream.extract_cause_effect(text) is None

    def test_normalizes_whitespace(self):
        pair = dream.extract_cause_effect("a   b\n\nbecause   c   d")
        assert pair == ("c d", "a b")


class TestNormalizeSubject:
    def test_lowercases_and_slugifies(self):
        assert dream.normalize_subject("The Lockfile Was Stale!") == "the_lockfile_was_stale"

    def test_keeps_path_like_characters(self):
        assert dream.normalize_subject("src/db.py") == "src/db.py"

    def test_empty_input_is_labelled(self):
        assert dream.normalize_subject("") == "unknown"

    def test_is_stable_across_formatting(self):
        assert dream.normalize_subject("  A  B ") == dream.normalize_subject("a b")


class TestPass1Ingest:
    def test_enriches_missing_cause_and_effect(self, temp_db):
        _add_l0("m1", "build failed because the lockfile was stale")
        stats = dream.pass1_ingest()
        assert stats["scanned"] == 1
        assert stats["enriched"] == 1
        assert _row("m1")["cause"] == "the lockfile was stale"
        assert _row("m1")["effect"] == "build failed"

    def test_leaves_existing_pairs_untouched(self, temp_db):
        _add_l0("m1", "x because y", cause="hand-written", effect="curated")
        assert dream.pass1_ingest()["enriched"] == 0
        assert _row("m1")["cause"] == "hand-written"

    def test_unparseable_rows_are_scanned_but_not_invented(self, temp_db):
        _add_l0("m1", "just an observation")
        stats = dream.pass1_ingest()
        assert (stats["scanned"], stats["enriched"]) == (1, 0)
        assert _row("m1")["cause"] is None

    def test_skips_rows_that_already_have_a_parent(self, temp_db):
        _add_l0("m1", "a because b")
        dream.pass2_compress([])  # no-op, just to be explicit
        db.causal_insert(id="p1", text="episode", layer=1)
        with db.write_txn() as txn:
            txn.execute("UPDATE causal_memories SET parent_id='p1' WHERE id='m1'")
        assert dream.pass1_ingest()["scanned"] == 0

    def test_respects_the_ingest_limit(self, temp_db):
        for i in range(5):
            _add_l0(f"m{i}", f"text {i}", days_ago=5 - i)
        assert dream.pass1_ingest(limit=2)["scanned"] == 2

    def test_dry_run_writes_nothing(self, temp_db):
        _add_l0("m1", "build failed because the lockfile was stale")
        assert dream.pass1_ingest(dry_run=True)["enriched"] == 1
        assert _row("m1")["cause"] is None


class TestTelemetryIsNotExperience:
    """The observer's heartbeat is excluded from consolidation, in SQL.

    Of 394 L0 rows in the live store, 371 are two tag classes the observer emits
    unconditionally: ``session-summary`` (263, one per session end) and
    ``tool-error`` (108, one per failing Bash). They record that the agent is
    running, not that anything was learned — every L1 episode in that store
    reads "tool `Bash` reported an error: <traceback>" or "session X ended: N
    user messages".

    Nothing ever claims these rows, so they keep ``parent_id IS NULL``
    permanently. Selecting them and dropping them in Python would let 371
    undying rows consume the ``LIMIT`` window on every run — the same starvation
    ``SINGLETON_EPISODE_AFTER_DAYS`` was added to cure, with no rescue available
    because a telemetry row is never promoted. The filter therefore has to be a
    WHERE clause, and the tests below pin that distinction.
    """

    @staticmethod
    def _seed_telemetry(count: int = 1, *, days_ago: float = SETTLED_DAYS_AGO) -> None:
        for i in range(count):
            _add_l0(
                f"tool-err-{i}",
                f"tool `Bash` reported an error: boom {i}",
                session=f"telemetry-{i}",
                days_ago=days_ago,
                cause="tool_call:Bash",
                effect="tool_error",
                tags="observer,post-tool,tool-error,tool:Bash",
            )
            _add_l0(
                f"summary-{i}",
                f"session s{i} ended: 3 user messages",
                session=f"telemetry-{i}",
                days_ago=days_ago,
                cause="session_end",
                effect="session_summary",
                tags="observer,session-end,session-summary",
            )

    def test_unprocessed_l0_skips_telemetry_and_keeps_real_rows(self, temp_db):
        self._seed_telemetry()
        _add_l0("real", "build failed because the lockfile was stale",
                tags="observer,session-end,friction")

        assert [r["id"] for r in dream._unprocessed_l0(100)] == ["real"]

    def test_telemetry_does_not_eat_the_ingest_budget(self, temp_db):
        """Excluded in SQL, not in Python: 50 undying rows must not own the LIMIT.

        Telemetry is seeded OLDER than the real rows, so ``ORDER BY observed_at
        ASC LIMIT 2`` hands it the entire window if the filter runs after the
        query — and it does so again on every subsequent run, forever.
        """
        self._seed_telemetry(25, days_ago=10)
        _add_l0("real1", "x", session="pair", days_ago=1)
        _add_l0("real2", "y", session="pair", days_ago=1)

        rows = dream._unprocessed_l0(2)

        assert {r["id"] for r in rows} == {"real1", "real2"}

    def test_a_longer_tag_is_not_a_telemetry_tag(self, temp_db):
        """``tags`` is a comma-joined string, so the match must be delimited."""
        _add_l0("recovered", "the retry succeeded",
                tags="observer,post-tool,tool-error-recovered")

        assert [r["id"] for r in dream._unprocessed_l0(100)] == ["recovered"]

    def test_newest_l0_ignores_a_heartbeat(self, temp_db):
        """"Quiet" has to mean no signal arrived, not that the agent stopped
        breathing: a session end bumps this clock every time the user works."""
        _add_l0("real", "x", days_ago=2)
        self._seed_telemetry(days_ago=0.0)

        assert dream.newest_l0() == datetime.fromisoformat(_row("real")["observed_at"])

    def test_should_run_is_not_held_off_by_telemetry(self, temp_db):
        """The starved trigger: a live agent used to postpone every poll."""
        dream.write_last_success(datetime.now(UTC) - timedelta(hours=25))
        _add_l0("real", "x", days_ago=2)
        self._seed_telemetry(days_ago=0.0)

        go, why = dream.should_run()

        assert go
        assert why.endswith("m quiet")
        assert not why.startswith("only")  # the skip path's phrasing

    def test_friction_is_experience_and_still_consolidates(self, temp_db):
        """Anti-overreach: only telemetry is excluded, never friction.

        Friction rows carry no cause either — the agent's side of the exchange
        is not written to the transcript at all, so what provoked the rejection
        is unobserved — but they ARE lived experience and must still reach an
        episode. This fails if someone "fixes" the fabricated
        ``user_message -> hard_rejection`` pair by excluding friction the way
        telemetry is excluded.
        """
        self._seed_telemetry()
        _add_l0("f1", "user negative_constraint: не трогай", session="live",
                effect="negative_constraint",
                tags="observer,session-end,friction,negative_constraint")
        _add_l0("f2", "user hard_rejection: нет", session="live",
                effect="hard_rejection",
                tags="observer,session-end,friction,hard_rejection")

        stats = dream.consolidate()

        assert stats["pass1_ingest"]["scanned"] == 2
        assert stats["pass2_compress"]["l1"] == 1
        assert _row("f1")["parent_id"] is not None

    def test_a_lone_friction_row_becomes_an_episode_but_not_a_belief(self, temp_db, tmp_path):
        """The live case, end to end from a transcript: 5 of 8 sessions held
        exactly one friction row.

        Each used to inherit ``user_message -> <type>`` unanimously — unanimity
        being trivial at n=1 — and Pass 3 asserted it. With the cause gone the
        episode is still built; there is simply nothing in it that projects onto
        a belief. Driven through ``observer`` rather than a hand-seeded row, so
        it measures what the hook actually writes.
        """
        transcript = tmp_path / "ses_fd258c66a.jsonl"
        transcript.write_text(json.dumps({
            "type": "user",
            "timestamp": _iso(days_ago=30),
            "message": {"role": "user", "content": "не трогай этот файл"},
        }), "utf-8")
        observer.record(
            observer.session_end_observations({"session_id": "ses_fd258c66a"}, transcript)
        )

        # Past the singleton wait, so the episode is minted; past the default
        # TTL too, so Pass 5 would reap the row it just parented — a longer TTL
        # keeps it readable without touching DEFAULT_L0_TTL_DAYS.
        stats = dream.consolidate(l0_ttl_days=90)

        assert stats["pass1_ingest"]["scanned"] == 1  # the summary row is telemetry
        assert stats["pass2_compress"]["l1"] == 1
        assert stats["pass3_reconcile"]["asserted"] == 0
        assert db.belief_all_active() == []

    def test_a_telemetry_only_store_consolidates_into_nothing(self, temp_db):
        """No episode, and therefore no fabricated belief to promote."""
        self._seed_telemetry(3, days_ago=30)

        stats = dream.consolidate()

        assert stats["pass1_ingest"]["scanned"] == 0
        assert stats["pass2_compress"]["l1"] == 0
        assert stats["pass3_reconcile"]["asserted"] == 0
        assert db.belief_all_active() == []


# ===========================================================================
# Pass 2 — lossy compression
# ===========================================================================


class TestPass2Compress:
    def test_groups_a_session_into_one_episode(self, temp_db):
        _add_l0("m1", "first", session="sess-a")
        _add_l0("m2", "second", session="sess-a")
        rows = dream.pass1_ingest()["rows"]

        assert dream.pass2_compress(rows)["l1"] == 1

        parent = _row("m1")["parent_id"]
        assert parent is not None
        assert _row("m2")["parent_id"] == parent
        assert _row(parent)["layer"] == dream.L1_EPISODE

    def test_separate_sessions_stay_separate(self, temp_db):
        _add_l0("a1", "x", session="A")
        _add_l0("a2", "y", session="A")
        _add_l0("b1", "x", session="B")
        _add_l0("b2", "y", session="B")
        assert dream.pass2_compress(dream.pass1_ingest()["rows"])["l1"] == 2
        assert _row("a1")["parent_id"] != _row("b1")["parent_id"]

    def test_a_lone_record_is_not_an_episode(self, temp_db):
        _add_l0("m1", "solitary", session="sess-a")
        assert dream.pass2_compress(dream.pass1_ingest()["rows"])["l1"] == 0
        assert _row("m1")["parent_id"] is None

    def test_a_settled_lone_record_becomes_a_singleton_episode(self, temp_db):
        """Past the wait, no sibling is coming: promote it or starve forever."""
        _add_l0("m1", "solitary", session="sess-a", days_ago=30)
        stats = dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert (stats["l1"], stats["l1_singletons"]) == (1, 1)
        assert _row("m1")["parent_id"] is not None

    def test_a_lone_record_still_inside_the_wait_is_left_alone(self, temp_db):
        """A sibling may still land tonight — promoting now would freeze it out."""
        _add_l0("m1", "solitary", session="sess-a", days_ago=0.5)
        stats = dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert (stats["l1"], stats["l1_singletons"]) == (0, 0)
        assert _row("m1")["parent_id"] is None

    def test_lone_records_do_not_starve_the_ingest_budget(self, temp_db):
        """D2: orphans are the OLDEST rows, so they own the whole LIMIT window.

        Before the singleton rescue they could never be parented and never be
        deleted, so every subsequent run re-read the same dead rows and fresh
        records were never reached — consolidation stopped, silently, forever.
        """
        for i in range(4):
            _add_l0(f"orphan{i}", f"lonely {i}", session=f"solo-{i}", days_ago=30 - i)
        _add_l0("fresh1", "x", session="pair")
        _add_l0("fresh2", "y", session="pair")

        first = dream.consolidate(ingest_limit=4)
        assert first["pass1_ingest"]["scanned"] == 4
        assert first["pass2_compress"]["l1_singletons"] == 4

        second = dream.consolidate(ingest_limit=4)
        assert second["pass1_ingest"]["scanned"] == 2
        assert second["pass2_compress"]["l1"] == 1
        assert _row("fresh1")["parent_id"] is not None

    def test_does_not_weld_one_members_cause_to_anothers_effect(self, temp_db):
        _add_l0("m1", "x", session="s", cause="alpha config applied", effect="alpha is live")
        _add_l0("m2", "y", session="s", cause="beta reverted", effect="beta is gone")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        parent = _row(_row("m1")["parent_id"])
        assert parent["cause"] is None
        assert parent["effect"] is None

    def test_inherits_a_pair_the_members_agree_on(self, temp_db):
        _add_l0("m1", "x", session="s", cause="stale lock", effect="build fails")
        _add_l0("m2", "y", session="s", cause="stale lock", effect="build fails")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        parent = _row(_row("m1")["parent_id"])
        assert (parent["cause"], parent["effect"]) == ("stale lock", "build fails")

    def test_a_digest_does_not_borrow_an_arbitrary_session(self, temp_db):
        for session in ("A", "B"):
            _add_l0(f"{session}1", "x", session=session, tags="perf")
            _add_l0(f"{session}2", "y", session=session, tags="perf")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        digest = db.causal_query(layer=dream.L2_DIGEST, limit=5)[0]
        assert digest["session_id"] is None

    def test_an_episode_keeps_its_own_session(self, temp_db):
        _add_l0("m1", "x", session="sess-a")
        _add_l0("m2", "y", session="sess-a")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert _row(_row("m1")["parent_id"])["session_id"] == "sess-a"

    def test_sessionless_rows_group_by_day(self, temp_db):
        _add_l0("m1", "x", session=None)
        _add_l0("m2", "y", session=None)
        assert dream.pass2_compress(dream.pass1_ingest()["rows"])["l1"] == 1

    def test_summary_is_lossy_but_carries_its_members(self, temp_db):
        _add_l0("m1", "the lockfile was stale", session="s")
        _add_l0("m2", "the cache was cold", session="s")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        text = _row(_row("m1")["parent_id"])["text"]
        assert "lockfile" in text and "cache was cold" in text

    def test_abstraction_discounts_confidence(self, temp_db):
        _add_l0("m1", "x", session="s", confidence=0.8)
        _add_l0("m2", "y", session="s", confidence=0.8)
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert _row(_row("m1")["parent_id"])["confidence"] < 0.8

    def test_episode_inherits_the_earliest_observation_time(self, temp_db):
        _add_l0("m1", "x", session="s", days_ago=9)
        _add_l0("m2", "y", session="s", days_ago=1)
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        parent = _row(_row("m1")["parent_id"])
        assert parent["observed_at"] == _row("m1")["observed_at"]

    def test_is_idempotent_across_runs(self, temp_db):
        _add_l0("m1", "x", session="s")
        _add_l0("m2", "y", session="s")
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        before = db.causal_query(limit=100)
        dream.pass2_compress(dream.pass1_ingest()["rows"])
        after = db.causal_query(limit=100)
        assert len(before) == len(after)

    def _seed_night(self, night):
        """One night's raw trace: two themes, two sessions each, two records each."""
        for tag in ("perf", "build"):
            for s in ("1", "2"):
                session = f"{tag}-{night}-{s}"
                _add_l0(f"{tag}{night}{s}a", "x", session=session, tags=tag)
                _add_l0(f"{tag}{night}{s}b", "y", session=session, tags=tag)

    def test_one_night_reaches_digests_but_not_axioms(self, temp_db):
        """Within a single run every theme collapses into exactly ONE digest,
        and one digest is not yet a pattern — so no axiom is minted."""
        self._seed_night(1)
        stats = dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert stats == {"l1": 4, "l1_singletons": 0, "l1_deferred": 0, "l2": 2, "l3": 0}

    def test_a_theme_that_recurs_across_nights_becomes_an_axiom(self, temp_db):
        """Axioms are earned by recurrence: a second night's digest on the same
        theme is what proves the first was not a coincidence."""
        self._seed_night(1)
        dream.pass2_compress(dream.pass1_ingest()["rows"])

        self._seed_night(2)
        stats = dream.pass2_compress(dream.pass1_ingest()["rows"])

        assert stats == {"l1": 4, "l1_singletons": 0, "l1_deferred": 0, "l2": 2, "l3": 2}
        axioms = db.causal_query(layer=dream.L3_AXIOM, limit=10)
        assert {a["tags"] for a in axioms} == {"perf", "build"}

        axiom_ids = {a["id"] for a in axioms}
        digests = db.causal_query(layer=dream.L2_DIGEST, limit=10)
        assert all(d["parent_id"] in axiom_ids for d in digests)

    def test_dry_run_writes_nothing(self, temp_db):
        _add_l0("m1", "x", session="s")
        _add_l0("m2", "y", session="s")
        rows = dream.pass1_ingest()["rows"]
        assert dream.pass2_compress(rows, dry_run=True)["l1"] == 1
        assert _row("m1")["parent_id"] is None
        assert db.causal_query(layer=dream.L1_EPISODE, limit=10) == []


# ===========================================================================
# Pass 3 — contradiction resolution & belief promotion
# ===========================================================================


def _add_ln(mid, layer, cause, effect, *, days_ago=0.0, confidence=0.8):
    db.causal_insert(
        id=mid,
        text=f"{cause} -> {effect}",
        layer=layer,
        cause=cause,
        effect=effect,
        confidence=confidence,
        observed_at=_iso(days_ago),
    )


class TestLadderIsFrequencyInvariant:
    """The ladder must be a function of the evidence, never of the cadence.

    Replaying one real day of this store at 1, 9 and 27 passes produced 12/26/42
    episodes and 0/4/8 axioms from identical input. A pass landing mid-session
    froze whatever rows existed at that instant into an episode, and because
    ``_synthetic_id`` hashes the member set, nothing ever merges a fragment
    back — so how often the daemon happened to run decided what became a
    standing principle.
    """

    @staticmethod
    def _seed_live_session(n: int = 6) -> None:
        """A session whose rows are still arriving — nothing has fallen silent."""
        for i in range(n):
            _add_l0(f"m{i}", f"step {i}", session="sess", days_ago=0.0)

    @staticmethod
    def _build(passes: int, now: datetime | None = None) -> dict[str, int]:
        """Total rungs CREATED across the whole run of passes, not the end state."""
        built = {"l1": 0, "l2": 0, "l3": 0}
        for _ in range(passes):
            rows = dream.pass1_ingest()["rows"]
            stats = dream.pass2_compress(rows, now=now)
            for rung in built:
                built[rung] += stats[rung]
        return built

    @pytest.mark.parametrize("passes", [1, 9, 27])
    def test_polling_a_live_session_folds_nothing(self, temp_db, passes):
        self._seed_live_session()
        assert self._build(passes) == {"l1": 0, "l2": 0, "l3": 0}
        assert all(_row(f"m{i}")["parent_id"] is None for i in range(6))

    @pytest.mark.parametrize("passes", [1, 9, 27])
    def test_a_finished_session_yields_one_episode_at_any_cadence(self, temp_db, passes):
        self._seed_live_session()
        silent = datetime.now(UTC) + timedelta(hours=dream.SESSION_QUIET_HOURS + 1)
        assert self._build(passes, now=silent) == {"l1": 1, "l2": 0, "l3": 0}

    def test_rows_arriving_between_passes_still_make_one_whole_episode(self, temp_db):
        """The daemon polls throughout the session; it must not come out in pieces."""
        for i in range(6):
            _add_l0(f"m{i}", f"step {i}", session="sess", days_ago=0.0)
            dream.consolidate()

        silent = datetime.now(UTC) + timedelta(hours=dream.SESSION_QUIET_HOURS + 1)
        assert dream.pass2_compress(dream.pass1_ingest()["rows"], now=silent)["l1"] == 1

        parents = {_row(f"m{i}")["parent_id"] for i in range(6)}
        assert None not in parents
        assert len(parents) == 1

    def test_a_live_session_is_reported_as_deferred_not_silently_skipped(self, temp_db):
        self._seed_live_session()
        stats = dream.pass2_compress(dream.pass1_ingest()["rows"])
        assert stats["l1_deferred"] == 1
        assert "1 sessions still live" in dream.format_report(dream.consolidate(dry_run=True))


class TestCoherentPair:
    """A pair is inherited only when EVERY member states it.

    Silence is diversity. Dropping the members that state no pair and then
    agreeing with whoever is left let ``l2:94ef7d7debec6f62`` inherit
    ``tool_call:Bash -> tool_error`` from one of its twelve members and assert
    it on behalf of the other eleven — the ladder by which a single row becomes
    a system-wide axiom.
    """

    def test_one_paired_member_does_not_speak_for_the_silent_rest(self):
        members = [{"cause": "", "effect": ""} for _ in range(11)]
        members.append({"cause": "tool_call:Bash", "effect": "tool_error"})
        assert dream._coherent_pair(members) == (None, None)

    def test_a_pair_every_member_states_is_inherited(self):
        members = [{"cause": "stale lock", "effect": "build fails"} for _ in range(3)]
        assert dream._coherent_pair(members) == ("stale lock", "build fails")

    def test_whitespace_does_not_count_as_disagreement(self):
        members = [
            {"cause": "stale  lock", "effect": "build fails"},
            {"cause": "stale lock", "effect": "build\nfails"},
        ]
        assert dream._coherent_pair(members) == ("stale lock", "build fails")

    def test_members_stating_different_pairs_carry_none(self):
        members = [{"cause": "a", "effect": "b"}, {"cause": "c", "effect": "d"}]
        assert dream._coherent_pair(members) == (None, None)

    def test_no_members_carry_no_pair(self):
        assert dream._coherent_pair([]) == (None, None)


class TestBeliefProjection:
    def test_requires_both_cause_and_effect(self):
        assert dream.belief_from({"cause": "a", "effect": None}) is None
        assert dream.belief_from({"cause": "", "effect": "b"}) is None

    def test_projects_a_triple(self):
        triple = dream.belief_from({"cause": "Stale Lock", "effect": "build fails",
                                    "confidence": 0.7})
        assert triple == ("stale_lock", "causes", "build fails", 0.7)

    def test_id_is_deterministic_but_time_scoped(self):
        a = dream.belief_id("s", "causes", "o", "2026-01-01")
        b = dream.belief_id("s", "causes", "o", "2026-01-01")
        c = dream.belief_id("s", "causes", "o", "2026-06-01")
        assert a == b != c


class TestPass3Reconcile:
    def test_asserts_a_new_belief(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails")
        stats = dream.pass3_reconcile()
        assert stats["asserted"] == 1
        assert db.belief_get_active("stale_lock", "causes")["object"] == "build fails"

    def test_ignores_l0_records(self, temp_db):
        _add_l0("m1", "x", cause="stale lock", effect="build fails")
        assert dream.pass3_reconcile()["asserted"] == 0

    def test_ignores_memories_without_a_causal_pair(self, temp_db):
        db.causal_insert(id="e1", text="just a note", layer=1)
        assert dream.pass3_reconcile()["asserted"] == 0

    def test_a_stable_belief_is_never_churned(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails")
        dream.pass3_reconcile()
        first = db.belief_get_active("stale_lock", "causes")
        stats = dream.pass3_reconcile()
        assert stats == {"asserted": 0, "superseded": 0, "unchanged": 1,
                         "rejected": 0, "shadowed": 0}
        assert db.belief_get_active("stale_lock", "causes")["id"] == first["id"]

    def test_newer_contradiction_supersedes_and_back_links(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails", days_ago=10)
        dream.pass3_reconcile()
        old = db.belief_get_active("stale_lock", "causes")

        _add_ln("e2", 1, "stale lock", "build is merely slow", days_ago=1)
        stats = dream.pass3_reconcile()

        assert (stats["asserted"], stats["superseded"]) == (1, 1)
        new = db.belief_get_active("stale_lock", "causes")
        assert new["object"] == "build is merely slow"

        with db.read_conn() as c:
            row = dict(c.execute(
                "SELECT * FROM belief_state WHERE id = ?", (old["id"],)
            ).fetchone())
        assert row["status"] == "superseded"
        assert row["superseded_by"] == new["id"]
        assert row["valid_to"] is not None
        assert new["supersedes"] == old["id"]

    def test_a_much_more_confident_incumbent_survives(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails", days_ago=10, confidence=0.95)
        dream.pass3_reconcile()
        _add_ln("e2", 1, "stale lock", "nothing happens", days_ago=1, confidence=0.2)

        stats = dream.pass3_reconcile()

        assert stats["rejected"] == 1
        assert stats["asserted"] == 0
        assert db.belief_get_active("stale_lock", "causes")["object"] == "build fails"

    def test_only_the_newest_claim_of_a_run_is_asserted(self, temp_db):
        """Two contradicting memories in one run: the older one is shadowed."""
        _add_ln("e1", 1, "stale lock", "build fails", days_ago=10)
        _add_ln("e2", 1, "stale lock", "build is slow", days_ago=1)

        stats = dream.pass3_reconcile()

        assert stats["asserted"] == 1
        assert stats["shadowed"] == 1
        assert db.belief_get_active("stale_lock", "causes")["object"] == "build is slow"

    def test_a_flip_flop_does_not_collide_on_primary_key(self, temp_db):
        """A -> B -> A must produce three distinct historical rows."""
        _add_ln("e1", 1, "topic", "A", days_ago=30)
        dream.pass3_reconcile()
        _add_ln("e2", 1, "topic", "B", days_ago=20)
        dream.pass3_reconcile()
        _add_ln("e3", 1, "topic", "A", days_ago=10)
        dream.pass3_reconcile()

        assert db.belief_get_active("topic", "causes")["object"] == "A"
        with db.read_conn() as c:
            total = c.execute("SELECT COUNT(*) FROM belief_state").fetchone()[0]
        assert total == 3

    def test_distinct_subjects_coexist(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails")
        _add_ln("e2", 1, "cold cache", "first run is slow")
        assert dream.pass3_reconcile()["asserted"] == 2
        assert len(db.belief_all_active()) == 2

    def test_records_the_evidence_and_the_source(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails")
        dream.pass3_reconcile()
        belief = db.belief_get_active("stale_lock", "causes")
        assert belief["evidence_id"] == "e1"
        assert belief["source"] == "dream"

    def test_dry_run_writes_nothing(self, temp_db):
        _add_ln("e1", 1, "stale lock", "build fails")
        assert dream.pass3_reconcile(dry_run=True)["asserted"] == 1
        assert db.belief_get_active("stale_lock", "causes") is None


# ===========================================================================
# Pass 4 / Pass 5
# ===========================================================================


class TestPass4Decay:
    def test_delegates_to_the_shared_sleep_pass(self, temp_db):
        db.fact_set("old", {"value": "v", "last_reinforced_at": _iso(days_ago=200)})
        volume.set_volume("fact", "old", 80.0)
        assert dream.pass4_decay()["fact"] == 1
        assert volume.get_volume("fact", "old") < 80.0

    def test_dry_run_writes_nothing(self, temp_db):
        db.fact_set("old", {"value": "v", "last_reinforced_at": _iso(days_ago=200)})
        volume.set_volume("fact", "old", 80.0)
        assert dream.pass4_decay(dry_run=True)["total_decayed"] == 0
        assert volume.get_volume("fact", "old") == 80.0


class TestPass5Forget:
    def _seed(self):
        db.causal_insert(id="parent", text="episode", layer=1)
        _add_l0("old_parented", "x", days_ago=30)
        _add_l0("old_orphan", "y", days_ago=30)
        _add_l0("fresh_parented", "z", days_ago=0)
        with db.write_txn() as txn:
            txn.execute(
                "UPDATE causal_memories SET parent_id='parent' "
                "WHERE id IN ('old_parented','fresh_parented')"
            )

    def test_deletes_only_aged_parented_l0(self, temp_db):
        self._seed()
        assert dream.pass5_forget(l0_ttl_days=7) == 1
        assert _row("old_parented") is None
        assert _row("old_orphan") is not None      # nothing summarizes it yet
        assert _row("fresh_parented") is not None  # still inside the TTL
        assert _row("parent") is not None          # abstractions are kept

    def test_ttl_is_honoured(self, temp_db):
        self._seed()
        assert dream.pass5_forget(l0_ttl_days=90) == 0
        assert _row("old_parented") is not None

    def test_dry_run_counts_without_deleting(self, temp_db):
        self._seed()
        assert dream.pass5_forget(l0_ttl_days=7, dry_run=True) == 1
        assert _row("old_parented") is not None


class TestPass5ReapsTelemetry:
    """Telemetry ages out on the TTL alone, because it can never earn a parent.

    Excluding it from consolidation removed the only route by which it used to
    disappear: it was folded into a junk episode, which set ``parent_id``, which
    let the reaper take it at 7 days. Nothing claims it now, so a reaper that
    demands a parent would make it immortal — roughly 90 rows a day, forever.
    The parent check still guards REAL observations, whose content has to
    survive inside an episode before the trace may go.
    """

    @staticmethod
    def _seed_every_case() -> None:
        """One row per branch of the predicate, so the four cases are separable."""
        db.causal_insert(id="parent", text="episode", layer=1)
        _add_l0("aged_parented_real", "consumed", days_ago=30)
        _add_l0("aged_orphan_real", "nothing summarizes this yet", days_ago=30)
        _add_l0("aged_telemetry", "session s ended: 3 user messages", days_ago=30,
                tags="observer,session-end,session-summary")
        _add_l0("fresh_telemetry", "tool `Bash` reported an error: boom", days_ago=0,
                tags="observer,post-tool,tool-error,tool:Bash")
        with db.write_txn() as txn:
            txn.execute(
                "UPDATE causal_memories SET parent_id='parent' "
                "WHERE id = 'aged_parented_real'"
            )

    def test_reaps_aged_telemetry_that_nothing_will_ever_claim(self, temp_db):
        _add_l0("aged_telemetry", "session s ended: 3 user messages", days_ago=30,
                tags="observer,session-end,session-summary")
        assert dream.pass5_forget(l0_ttl_days=7) == 1
        assert _row("aged_telemetry") is None

    def test_telemetry_inside_the_ttl_is_left_alone(self, temp_db):
        _add_l0("fresh_telemetry", "tool `Bash` reported an error: boom", days_ago=1,
                tags="observer,post-tool,tool-error,tool:Bash")
        assert dream.pass5_forget(l0_ttl_days=7) == 0
        assert _row("fresh_telemetry") is not None

    def test_an_unparented_real_observation_is_still_never_reaped(self, temp_db):
        """The original safety property. This fails if the predicate is ever
        'simplified' into a bare TTL sweep."""
        _add_l0("aged_orphan_real", "the lockfile was stale", days_ago=300)
        assert dream.pass5_forget(l0_ttl_days=7) == 0
        assert _row("aged_orphan_real") is not None

    def test_an_aged_parented_real_observation_is_still_reaped(self, temp_db):
        db.causal_insert(id="parent", text="episode", layer=1)
        _add_l0("aged_parented_real", "consumed", days_ago=30)
        with db.write_txn() as txn:
            txn.execute("UPDATE causal_memories SET parent_id='parent' WHERE id = ?",
                        ("aged_parented_real",))

        assert db.causal_delete_l0_reapable(l0_ttl_days=7) == 1
        assert _row("aged_parented_real") is None
        assert _row("parent") is not None

    def test_a_longer_tag_is_not_reapable_without_a_parent(self, temp_db):
        """``tool-error-recovered`` is a real observation about a retry."""
        _add_l0("recovered", "the retry succeeded", days_ago=300,
                tags="observer,post-tool,tool-error-recovered")
        assert dream.pass5_forget(l0_ttl_days=7) == 0
        assert _row("recovered") is not None

    def test_the_dry_run_count_is_exactly_what_the_delete_removes(self, temp_db):
        """Anti-drift: report and action must read ONE predicate, not two copies."""
        self._seed_every_case()

        predicted = dream.pass5_forget(l0_ttl_days=7, dry_run=True)
        deleted = dream.pass5_forget(l0_ttl_days=7)

        assert predicted == deleted == 2
        assert _row("aged_parented_real") is None
        assert _row("aged_telemetry") is None
        assert _row("aged_orphan_real") is not None
        assert _row("fresh_telemetry") is not None


# ===========================================================================
# Orchestration & CLI
# ===========================================================================


class TestConsolidate:
    def test_runs_every_pass_end_to_end(self, temp_db):
        """The session states ONE claim twice, so the episode may carry it."""
        _add_l0("m1", "build failed because the lockfile was stale", session="s")
        _add_l0("m2", "build failed because the lockfile was stale", session="s")

        stats = dream.consolidate()

        assert stats["pass1_ingest"] == {"scanned": 2, "enriched": 2}
        assert stats["pass2_compress"]["l1"] == 1
        assert stats["pass3_reconcile"]["asserted"] >= 1
        assert stats["pass5_forget"]["ttl_days"] == 7
        assert stats["dry_run"] is False
        assert stats["duration_sec"] >= 0
        assert db.belief_all_active()

    def test_a_session_that_disagrees_promotes_no_belief(self, temp_db):
        """Two different claims in one session must not be welded into a third."""
        _add_l0("m1", "build failed because the lockfile was stale", session="s")
        _add_l0("m2", "tests failed because the cache was cold", session="s")

        stats = dream.consolidate()

        assert stats["pass2_compress"]["l1"] == 1
        assert stats["pass3_reconcile"]["asserted"] == 0
        assert db.belief_all_active() == []

    def test_is_safe_to_run_twice(self, temp_db):
        _add_l0("m1", "a because b", session="s")
        _add_l0("m2", "c because b", session="s")
        dream.consolidate()
        beliefs_before = len(db.belief_all_active())
        second = dream.consolidate()
        assert second["pass2_compress"]["l1"] == 0
        assert len(db.belief_all_active()) == beliefs_before

    def test_dry_run_leaves_the_database_untouched(self, temp_db):
        _add_l0("m1", "a because b", session="s")
        _add_l0("m2", "c because b", session="s")
        stats = dream.consolidate(dry_run=True)
        assert stats["dry_run"] is True
        assert _row("m1")["parent_id"] is None
        assert db.belief_all_active() == []

    def test_report_mentions_every_pass(self, temp_db):
        text = dream.format_report(dream.consolidate())
        for marker in ("Pass 1", "Pass 2", "Pass 3", "Pass 4", "Pass 5"):
            assert marker in text


class TestShouldRun:
    """Time decides whether a poll works, and a pass lands once a calendar day.

    The record of "last pass" is a file of its own, NOT the flock sidecar. That
    one is stamped on acquisition, before the work: a ``--dry-run`` advances it,
    and so does a pass that dies halfway. A daemon crash-looping every poll would
    have kept satisfying the staleness fallback forever.
    """

    @staticmethod
    def _record_success(hours_ago: float) -> datetime:
        moment = datetime.now(UTC) - timedelta(hours=hours_ago)
        dream.write_last_success(moment)
        return moment

    def test_runs_when_no_pass_was_ever_recorded(self, temp_db):
        go, why = dream.should_run()
        assert go
        assert "no successful pass" in why

    def test_an_unreadable_record_counts_as_never_run(self, temp_db):
        dream.state_path().write_text("not a timestamp\n")
        assert dream.should_run()[0]

    def test_holds_off_when_a_pass_already_landed_today(self, temp_db):
        self._record_success(hours_ago=0.5)
        _add_l0("m1", "x", days_ago=30)
        assert dream.should_run() == (False, "already consolidated today")

    def test_a_missed_day_forces_a_pass_even_with_no_quiet(self, temp_db):
        self._record_success(hours_ago=27)
        _add_l0("m1", "x", days_ago=0.0)
        go, why = dream.should_run()
        assert go
        assert "since the last pass" in why

    def test_runs_once_the_day_turned_and_the_store_went_quiet(self, temp_db):
        self._record_success(hours_ago=25)
        _add_l0("m1", "x", days_ago=1 / 24)
        assert dream.should_run()[0]

    def test_waits_while_observations_are_still_landing(self, temp_db):
        self._record_success(hours_ago=25)
        _add_l0("m1", "x", days_ago=5 / 1440)
        go, why = dream.should_run()
        assert not go
        assert "quiet" in why

    def test_an_empty_store_has_nothing_to_wait_for(self, temp_db):
        self._record_success(hours_ago=25)
        assert dream.should_run()[0]


class TestLastSuccessIsOnlyClaimedOnSuccess:
    def test_a_successful_pass_is_recorded(self, temp_db):
        assert dream.main(["--force", "--db", str(temp_db)]) == 0
        assert dream.read_last_success() is not None

    def test_a_dry_run_claims_nothing(self, temp_db):
        assert dream.main(["--dry-run", "--db", str(temp_db)]) == 0
        assert dream.read_last_success() is None

    def test_a_pass_that_raises_claims_nothing(self, temp_db, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("pass 3 fell over")

        monkeypatch.setattr(dream, "pass3_reconcile", _boom)
        assert dream.main(["--force", "--db", str(temp_db)]) == 1
        assert dream.read_last_success() is None

    def test_the_gate_stops_a_second_pass_the_same_day(self, temp_db):
        assert dream.main(["--force", "--db", str(temp_db)]) == 0
        first = dream.read_last_success()
        assert dream.main(["--nightly", "--db", str(temp_db)]) == 0
        assert dream.read_last_success() == first


class TestCli:
    def test_json_output_is_parseable(self, temp_db, capsys):
        assert dream.main(["--json", "--db", str(temp_db)]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["pass5_forget"]["ttl_days"] == 7

    def test_ttl_flag_is_honoured(self, temp_db, capsys):
        assert dream.main(["--json", "--db", str(temp_db), "--l0-ttl-days", "30"]) == 0
        assert json.loads(capsys.readouterr().out)["pass5_forget"]["ttl_days"] == 30

    def test_dry_run_flag_is_propagated(self, temp_db, capsys):
        assert dream.main(["--json", "--dry-run", "--db", str(temp_db)]) == 0
        assert json.loads(capsys.readouterr().out)["dry_run"] is True

    def test_manual_trigger_prints_a_report(self, temp_db, capsys):
        assert dream.main(["--db", str(temp_db)]) == 0
        assert "Dream cycle complete" in capsys.readouterr().out

    def test_nightly_is_quiet_when_there_was_nothing_to_do(self, temp_db, capsys):
        assert dream.main(["--nightly", "--db", str(temp_db)]) == 0
        assert capsys.readouterr().out == ""

    def test_nightly_reports_when_it_did_work(self, temp_db, capsys):
        _add_l0("m1", "a because b")
        assert dream.main(["--nightly", "--db", str(temp_db)]) == 0
        assert "Dream cycle complete" in capsys.readouterr().out

    def test_a_busy_lock_exits_zero(self, temp_db, capsys):
        """An overlapping run is normal, not a failure — launchd must not alarm."""
        with dream.single_writer():
            assert dream.main(["--db", str(temp_db)]) == 0
        assert "skipped" in capsys.readouterr().err

    def test_a_failing_pass_exits_nonzero(self, temp_db, monkeypatch, capsys):
        monkeypatch.setattr(
            dream, "consolidate",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("kaboom")),
        )
        assert dream.main(["--db", str(temp_db)]) == 1
        assert "FAILED" in capsys.readouterr().err

    def test_module_runs_as_a_script(self, tmp_path):
        """The launchd/cron entrypoint must work without an installed package."""
        result = subprocess.run(
            [sys.executable, str(MEMORY_DIR / "dream.py"), "--nightly",
             "--db", str(tmp_path / "cli.db")],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr


# ===========================================================================
# Shipped configuration templates
# ===========================================================================


CONFIG_DIR = MEMORY_DIR.parent / "config"


class TestHookTemplate:
    @pytest.fixture
    def hooks(self):
        return json.loads((CONFIG_DIR / "claude-settings.json").read_text())["hooks"]

    def _commands(self, hooks, event):
        return [
            entry["command"]
            for group in hooks[event]
            for entry in group["hooks"]
        ]

    def test_registers_all_three_events(self, hooks):
        assert set(hooks) >= {"UserPromptSubmit", "PostToolUse", "Stop"}

    def test_user_prompt_submit_injects_memory(self, hooks):
        assert any(
            c.endswith("memory-inject.py")
            for c in self._commands(hooks, "UserPromptSubmit")
        )

    def test_post_tool_use_runs_the_observer(self, hooks):
        assert any(
            c.endswith("observer.py --post-tool")
            for c in self._commands(hooks, "PostToolUse")
        )

    def test_stop_runs_the_session_end_observer(self, hooks):
        assert any(
            c.endswith("observer.py --session-end")
            for c in self._commands(hooks, "Stop")
        )

    def test_every_command_uses_the_venv_interpreter(self, hooks):
        for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
            for command in self._commands(hooks, event):
                assert ".venv/bin/python" in command

    def test_every_command_is_installer_templated(self, hooks):
        for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
            for command in self._commands(hooks, event):
                assert command.startswith("MEMORY_PATH/")

    def test_every_hook_has_a_timeout(self, hooks):
        """A hook without a timeout can wedge the agent turn indefinitely."""
        for groups in hooks.values():
            for group in groups:
                for entry in group["hooks"]:
                    assert entry["type"] == "command"
                    assert isinstance(entry["timeout"], int)


class TestLaunchdTemplate:
    @pytest.fixture
    def plist(self):
        import plistlib
        return plistlib.loads((CONFIG_DIR / "com.crystallized.dream.plist").read_bytes())

    def test_label_matches_the_filename(self, plist):
        assert plist["Label"] == "com.crystallized.dream"

    def test_runs_dream_nightly_from_the_venv(self, plist):
        args = plist["ProgramArguments"]
        assert args[0].endswith(".venv/bin/python")
        assert args[1].endswith("dream.py")
        assert args[2] == "--nightly"

    def test_polls_instead_of_keeping_an_appointment(self, plist):
        """A calendar hour is dropped outright when the machine is powered off.

        That is why the nightly agent recorded runs = 0: this laptop is
        regularly shut down at 04:00. An interval has no slot to miss.
        """
        assert "StartCalendarInterval" not in plist
        assert plist["StartInterval"] == 900

    def test_stays_out_of_the_way_while_polling(self, plist):
        assert plist["ProcessType"] == "Background"
        assert plist["LowPriorityIO"] is True

    def test_does_not_run_at_load(self, plist):
        """Loading the agent must never kick off a full pass mid-session."""
        assert plist["RunAtLoad"] is False

    def test_logs_are_captured(self, plist):
        assert plist["StandardOutPath"].endswith("dream.log")
        assert plist["StandardErrorPath"].endswith("dream.log")

    def test_paths_are_installer_templated(self, plist):
        assert plist["WorkingDirectory"] == "MEMORY_PATH"
        assert all(a.startswith(("MEMORY_PATH", "--")) for a in plist["ProgramArguments"])
