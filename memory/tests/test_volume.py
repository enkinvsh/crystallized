"""Tests for volume and supersession demotion (volume.py)."""

from datetime import datetime, timedelta

import pytest

import db
import volume


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    db_file = tmp_path / "test_db.sqlite"
    db.set_db_path(db_file)
    yield
    db.close_db()


def test_reinforce_superseded_fact_is_noop():
    db.fact_set("f_old", {"value": "old"})
    db.fact_set("f_new", {"value": "new"})
    volume.set_volume("fact", "f_old", 60.0)
    volume.set_volume("fact", "f_new", 50.0)

    db.fact_supersede("f_old", "f_new")

    events = []
    def log_evt(*args, **kwargs):
        events.append(args)

    # Reinforcing f_old should return its current volume without changing it
    vol = volume.reinforce("fact", "f_old", quality=1.0, log_event=log_evt)
    assert vol == 60.0
    assert volume.get_volume("fact", "f_old") == 60.0
    assert len(events) == 0

    # Reinforcing unsuperseded fact works normally
    vol_new = volume.reinforce("fact", "f_new", quality=1.0, log_event=log_evt)
    assert vol_new > 50.0
    assert len(events) == 1


def test_demote_superseded_caps_below_its_correction():
    db.fact_set("f_old", {"value": "old"})
    db.fact_set("f_new", {"value": "new"})
    volume.set_volume("fact", "f_old", 70.0)
    volume.set_volume("fact", "f_new", 40.0)

    old_vol = volume.demote_superseded("f_old", "f_new")

    ceiling = volume.SUPERSEDED_VOLUME_RATIO * volume.effective_volume("fact", "f_new")
    assert old_vol == pytest.approx(ceiling, abs=1e-3)
    assert volume.effective_volume("fact", "f_old") <= ceiling + 1e-6
    # The cap is a ranking rule, not a burial: a partly-corrected fact stays audible.
    assert old_vol > 0.5 * volume.effective_volume("fact", "f_new")


def test_demote_superseded_never_raises():
    db.fact_set("f_old", {"value": "old"})
    db.fact_set("f_new", {"value": "new"})
    volume.set_volume("fact", "f_old", 10.0)
    volume.set_volume("fact", "f_new", 80.0)

    # Half of new is 40.0, but old is already 10.0 (< 40.0), so demote must NOT raise old.
    old_vol = volume.demote_superseded("f_old", "f_new")
    assert pytest.approx(old_vol, abs=1e-4) == 10.0
    assert volume.get_volume("fact", "f_old") == 10.0


def test_lowering_a_fact_recaps_the_facts_it_supersedes():
    for key, vol in (("chain_a", 70.0), ("chain_b", 60.0), ("chain_c", 40.0)):
        db.fact_set(key, {"value": key})
        volume.set_volume("fact", key, vol)

    db.fact_supersede("chain_a", "chain_b")
    volume.demote_superseded("chain_a", "chain_b")  # a capped under b at 60
    db.fact_supersede("chain_b", "chain_c")
    volume.demote_superseded("chain_b", "chain_c")  # b drops to 32; a must follow

    ratio = volume.SUPERSEDED_VOLUME_RATIO
    b = volume.effective_volume("fact", "chain_b")
    assert b <= ratio * volume.effective_volume("fact", "chain_c") + 1e-6
    assert volume.effective_volume("fact", "chain_a") <= ratio * b + 1e-6


def _effective_at(key, at):
    fact = db.fact_get(key)
    return volume.decayed(
        volume.get_volume("fact", key), "fact", fact["last_reinforced_at"], at
    )


def test_sleep_closes_a_cap_that_drifted_open():
    """A cap is exact only at the instant it is applied.

    Decay is a power law of each fact's own age: a fresh correction sits on
    the steep head of the curve, the old fact on its flat tail, so days later
    the old fact is back above its cap (live store 2026-09-23: 6 of 200).
    """
    t0 = datetime(2026, 9, 1, 12, 0)
    db.fact_set("f_old", {"value": "old", "last_reinforced_at": (t0 - timedelta(days=30)).isoformat()})
    db.fact_set("f_new", {"value": "new", "last_reinforced_at": t0.isoformat()})
    volume.set_volume("fact", "f_old", 100.0)
    volume.set_volume("fact", "f_new", 50.0)
    db.fact_supersede("f_old", "f_new")
    ratio = volume.SUPERSEDED_VOLUME_RATIO

    assert volume.recap_superseded(now=t0) == 1
    assert _effective_at("f_old", t0) == pytest.approx(ratio * _effective_at("f_new", t0))

    later = t0 + timedelta(days=10)
    assert _effective_at("f_old", later) > ratio * _effective_at("f_new", later)

    volume.sleep(now=later)
    assert _effective_at("f_old", later) <= ratio * _effective_at("f_new", later) + 1e-6


def test_recap_walks_chains_from_the_head():
    for key in ("chain_a", "chain_b", "chain_c"):
        db.fact_set(key, {"value": key})
        volume.set_volume("fact", key, 90.0)
    # Linked without demotion, the way a raw store update would leave them.
    db.fact_supersede("chain_a", "chain_b")
    db.fact_supersede("chain_b", "chain_c")

    assert volume.recap_superseded() == 2

    ratio = volume.SUPERSEDED_VOLUME_RATIO
    b = volume.effective_volume("fact", "chain_b")
    assert b == pytest.approx(ratio * volume.effective_volume("fact", "chain_c"))
    assert volume.effective_volume("fact", "chain_a") == pytest.approx(ratio * b)
    assert volume.recap_superseded() == 0
