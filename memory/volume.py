"""Volume decay mathematics — the single source of truth for the loudness model.

Extracted from ``server.py`` so that BOTH the MCP server and the consolidation
daemon (``dream.py``) share one implementation. Nothing here may import
ChromaDB, sentence-transformers or the MCP SDK: ``dream.py`` runs as a headless
nightly job and must stay cheap to start.

The model
---------
Every remembered entity carries a *volume* (loudness). Volume is reinforced on
recall and decays with elapsed time following a power law::

    V_eff = V_stored * (1 + t_hours / tau) ** (-alpha)

Power-law (not exponential) decay is deliberate: it matches human forgetting
curves — fast right after the last touch, then an ever-flattening tail — so old
but once-important memories stay audible instead of vanishing.

Decay is normally computed ON READ and never stored. ``sleep()`` is the one
exception: it materializes the decayed value and resets the reinforcement clock
so the curve restarts from the lower plateau.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Protocol

import db

# ---------------------------------------------------------------------------
# Volume constants (power-law decay system)
# ---------------------------------------------------------------------------

MIN_VOLUME = 0.01
MAX_VOLUME = 100.0
DEFAULT_VOLUME: dict[str, float] = {
    "fact": 50.0,  # facts start mid-range, prove themselves via recall
    "semantic": 40.0,  # slightly lower — inferred, not stated
    "doc": 60.0,  # docs are intentionally saved, start louder
}

# Decay constants (power-law): V_eff = V_stored * (1 + t_hours / τ)^(-α)
DECAY_ALPHA: dict[str, float] = {"fact": 0.15, "semantic": 0.08, "doc": 0.03}
DECAY_TAU: dict[str, float] = {"fact": 72.0, "semantic": 168.0, "doc": 720.0}  # hours

# Fallbacks for layers not listed above (e.g. "causal").
FALLBACK_VOLUME = 50.0
FALLBACK_ALPHA = 0.1
FALLBACK_TAU = 168.0

# Reinforcement boost is headroom-scaled: a loud memory gains less than a quiet
# one, so volumes converge instead of everything pinning at MAX_VOLUME.
REINFORCE_BOOST = 12.0

# A layer is only rewritten by sleep() when it drifted by more than this, so a
# no-op nightly run does not churn every row in the database.
DECAY_EPSILON = 0.01

#: ``(doc_id, text, metadata)`` as yielded by the semantic providers.
SemanticRow = tuple[str, str, dict]


class LogEvent(Protocol):
    """Signature of the optional event-log sink handed to :func:`sleep`."""

    def __call__(
        self,
        entry_id: str,
        event_type: str,
        volume: float,
        layer: str,
        conn: sqlite3.Connection | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def zset_key(layer: str, entry_id: str) -> str:
    """Composite ``volumes.entity_key``: ``'fact:user_name'``, ``'semantic:abc'``."""
    return f"{layer}:{entry_id}"


def parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp into a naive LOCAL datetime. None on failure.

    Timezone-aware inputs are converted to local time then stripped of tzinfo,
    so aware/naive mixes never silently yield age=0.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def decay_volume(stored: float, t_hours: float, layer: str) -> float:
    """Pure power-law decay: ``V * (1 + t/tau) ** (-alpha)``, floored at MIN_VOLUME.

    This is the whole model in one side-effect-free function — every other
    decay path in the codebase funnels through it, so the curve can never
    diverge between the server and the dream daemon.
    """
    if t_hours <= 0:
        return stored
    alpha = DECAY_ALPHA.get(layer, FALLBACK_ALPHA)
    tau = DECAY_TAU.get(layer, FALLBACK_TAU)
    return max(MIN_VOLUME, stored * (1 + t_hours / tau) ** (-alpha))


def as_local_naive(moment: datetime) -> datetime:
    """Normalize a datetime to the naive-local convention :func:`parse_ts` uses."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def elapsed_hours(now: datetime, last_reinforced_at: str | None) -> float | None:
    """Hours between a stored clock and ``now``. None when the clock is unusable."""
    last_dt = parse_ts(last_reinforced_at)
    if last_dt is None:
        return None
    return (as_local_naive(now) - last_dt).total_seconds() / 3600.0


def decayed(
    stored: float,
    layer: str,
    last_reinforced_at: str | None,
    now: datetime | None = None,
) -> float:
    """Apply :func:`decay_volume` using a timestamp instead of an age.

    An unparseable/absent clock means "never reinforced, no measurable age" and
    returns the stored value untouched — guessing an age would silently mute
    memories whose metadata is merely incomplete.

    ``now`` is injectable so callers that already fixed an instant (notably
    :func:`sleep`) measure against THAT instant instead of re-reading the wall
    clock, which would otherwise re-charge the same interval on every call.
    """
    age = elapsed_hours(now or datetime.now(), last_reinforced_at)
    if age is None:
        return stored
    return decay_volume(stored, age, layer)


def decay_step(
    stored: float, prev_age_hours: float, delta_hours: float, layer: str
) -> float:
    """Advance an ALREADY-materialized volume along the original power law.

    A power law does not compose: ``f(a) * f(b) != f(a + b)``. Materializing
    decay nightly with ``stored * f(delta)`` therefore multiplies 30 steep
    head-of-curve factors together and forgets ~2.5x too much over a month.

    Advancing by the RATIO of the true curve at the new and previous total ages
    telescopes instead — ``f(t1)/f(t0) * f(t2)/f(t1) * ... = f(tn)/f(t0)`` — so
    a nightly pass lands on exactly the same value as one single pass over the
    whole elapsed period. ``prev_age_hours`` is the accumulated age already
    charged to ``stored``; 0 means "reinforced, curve starts here".
    """
    if delta_hours <= 0:
        return stored
    alpha = DECAY_ALPHA.get(layer, FALLBACK_ALPHA)
    tau = DECAY_TAU.get(layer, FALLBACK_TAU)
    t_prev = max(0.0, prev_age_hours)
    t_total = t_prev + delta_hours
    factor = ((1 + t_total / tau) / (1 + t_prev / tau)) ** (-alpha)
    return max(MIN_VOLUME, stored * factor)


def clamp(volume: float) -> float:
    """Clamp a volume into ``[MIN_VOLUME, MAX_VOLUME]``."""
    return max(MIN_VOLUME, min(MAX_VOLUME, volume))


def default_for(layer: str) -> float:
    return DEFAULT_VOLUME.get(layer, FALLBACK_VOLUME)


# ---------------------------------------------------------------------------
# Store-backed accessors
# ---------------------------------------------------------------------------


def get_volume(layer: str, entry_id: str) -> float:
    """Raw stored volume from the volumes table (source of truth).

    The store returns None for absent keys and never invents a default —
    this module owns DEFAULT_VOLUME.
    """
    score = db.volume_get(zset_key(layer, entry_id))
    return score if score is not None else default_for(layer)


def set_volume(
    layer: str, entry_id: str, volume: float, conn: sqlite3.Connection | None = None
) -> None:
    """Write a clamped volume. Pass ``conn`` to join the caller's transaction."""
    db.volume_set(zset_key(layer, entry_id), layer, clamp(volume), conn=conn)


def effective_volume(
    layer: str, entry_id: str, last_reinforced_at: str | None = None
) -> float:
    """Stored volume with power-law decay applied. Computed on read, not stored."""
    return decayed(get_volume(layer, entry_id), layer, last_reinforced_at)


def bulk_effective_volumes(
    layer: str, entry_ids: list[str], last_reinforced: list[str | None]
) -> list[float]:
    """Effective volumes for many entries via ONE ``volume_map()`` query.

    Lookups are BY KEY, never positional: a ``WHERE entity_key IN (...)`` query
    returns rows in rowid order and omits absent keys, so zipping its result
    against ``entry_ids`` silently misaligns everything after the first gap.
    """
    if not entry_ids:
        return []
    vol_map = db.volume_map(layer)
    default = default_for(layer)
    return [
        decayed(vol_map.get(zset_key(layer, eid), default), layer, lr)
        for eid, lr in zip(entry_ids, last_reinforced, strict=False)
    ]


def reinforce(
    layer: str,
    entry_id: str,
    quality: float = 1.0,
    last_reinforced_at: str | None = None,
    conn: sqlite3.Connection | None = None,
    log_event: LogEvent | None = None,
) -> float:
    """Reinforce volume on recall. Headroom-scaled diminishing returns.

    When ``last_reinforced_at`` is provided the boost starts from the DECAYED
    effective volume (so accrued decay is NOT erased); otherwise it starts from
    the raw stored volume (legacy behavior for callers that lack the timestamp).

    quality: 0.0 (appeared but unused) to 1.0 (directly used in response).
    conn: optional write transaction — the volume write and its event log are
          one logical unit and must land (or roll back) together.
    Returns the new volume.
    """
    if last_reinforced_at is not None:
        current = effective_volume(layer, entry_id, last_reinforced_at)
    else:
        current = get_volume(layer, entry_id)
    headroom = MAX_VOLUME - current
    boost = REINFORCE_BOOST * quality * (headroom / MAX_VOLUME)
    new_vol = min(current + boost, MAX_VOLUME)
    set_volume(layer, entry_id, new_vol, conn=conn)
    # A real touch restarts the curve, so the accumulated decay age is void.
    db.decay_anchor_delete(zset_key(layer, entry_id), conn=conn)
    if log_event is not None:
        log_event(entry_id, "recall", new_vol, layer, conn=conn)
    return new_vol


# ---------------------------------------------------------------------------
# sleep() — the decay pass
# ---------------------------------------------------------------------------


def _default_writable_semantic() -> Iterable[SemanticRow]:
    """Semantic rows the store itself owns (``semantic_fallback`` table).

    Materialized up front so callers may write while iterating.
    """
    try:
        return list(db.semantic_iter())
    except sqlite3.Error:
        return []


def _default_writable_update(
    rows: list[SemanticRow], conn: sqlite3.Connection
) -> None:
    for doc_id, text, meta in rows:
        db.semantic_set(doc_id, text, meta, conn=conn)


def _advance(
    entity_key: str,
    layer: str,
    stored: float,
    last_reinforced_at: str | None,
    now: datetime,
    anchors: dict[str, float],
) -> tuple[float, float] | None:
    """One entity's materialization step, or None when there is nothing to charge.

    Returns ``(new_volume, new_accumulated_age)``. The delta is measured from
    the entity's OWN previous stamp to ``now``; the accumulated age carries the
    curve position forward so the step never re-charges what was already paid.
    """
    delta = elapsed_hours(now, last_reinforced_at)
    if delta is None or delta <= 0:
        return None
    prev_age = anchors.get(entity_key, 0.0)
    eff = decay_step(stored, prev_age, delta, layer)
    if eff >= stored - DECAY_EPSILON:
        return None
    return max(MIN_VOLUME, eff), prev_age + delta


def _decay_facts(now: datetime) -> int:
    """Decay every fact and reset its reinforcement clock. Returns rows touched."""
    all_facts = db.fact_all()  # materialized
    fact_vols = db.volume_map("fact")  # materialized
    anchors = db.decay_anchor_map()  # materialized
    default = default_for("fact")
    updates: list[tuple[str, str, float]] = []
    anchor_updates: list[tuple[str, float]] = []
    rows: list[tuple[str, dict]] = []
    for key, parsed in all_facts.items():
        entity_key = zset_key("fact", key)
        last_reinforced = parsed.get("last_reinforced_at", parsed.get("updated_at", ""))
        stored = fact_vols.get(entity_key, default)
        step = _advance(entity_key, "fact", stored, last_reinforced, now, anchors)
        if step is None:
            continue
        eff, age = step
        updates.append((entity_key, "fact", eff))
        anchor_updates.append((entity_key, age))
        # Reset clock: store the decayed value, mark it reinforced now.
        parsed["last_reinforced_at"] = now.isoformat()
        rows.append((key, parsed))
    if not updates:
        return 0
    with db.write_txn() as txn:
        db.volume_set_many(updates, conn=txn)
        db.decay_anchor_set_many(anchor_updates, conn=txn)
        db.fact_set_many(rows, conn=txn)
    return len(updates)


def _decay_semantic(
    now: datetime,
    readonly_semantic: Callable[[], Iterable[SemanticRow]] | None,
    writable_semantic: Callable[[], Iterable[SemanticRow]],
    writable_semantic_update: Callable[[list[SemanticRow], sqlite3.Connection], None],
) -> int:
    """Decay semantic memories from both a read-only and a writable provider.

    Read-only rows (e.g. ChromaDB's own sqlite file) cannot carry their clock,
    so it lives in the ``semantic_reinforced`` table instead of the metadata.
    """
    sem_vols = db.volume_map("semantic")  # materialized
    anchors = db.decay_anchor_map()  # materialized
    default = default_for("semantic")
    updates: list[tuple[str, str, float]] = []
    anchor_updates: list[tuple[str, float]] = []
    clock_updates: list[str] = []
    writable_updates: list[SemanticRow] = []

    if readonly_semantic is not None:
        clocks = db.semantic_reinforced_map()  # materialized: avoids N queries
        for doc_id, _text, meta in readonly_semantic():
            entity_key = zset_key("semantic", doc_id)
            last_reinforced = (
                clocks.get(doc_id) or meta.get("last_reinforced_at") or meta.get("date", "")
            )
            stored = sem_vols.get(entity_key, default)
            step = _advance(entity_key, "semantic", stored, last_reinforced, now, anchors)
            if step is None:
                continue
            eff, age = step
            updates.append((entity_key, "semantic", eff))
            anchor_updates.append((entity_key, age))
            clock_updates.append(doc_id)

    for doc_id, text, meta in writable_semantic():
        entity_key = zset_key("semantic", doc_id)
        last_reinforced = meta.get("last_reinforced_at", meta.get("date", ""))
        stored = sem_vols.get(entity_key, default)
        step = _advance(entity_key, "semantic", stored, last_reinforced, now, anchors)
        if step is None:
            continue
        eff, age = step
        updates.append((entity_key, "semantic", eff))
        anchor_updates.append((entity_key, age))
        meta["last_reinforced_at"] = now.isoformat()
        writable_updates.append((doc_id, text, meta))

    if not updates:
        return 0
    with db.write_txn() as txn:
        db.volume_set_many(updates, conn=txn)
        db.decay_anchor_set_many(anchor_updates, conn=txn)
        writable_semantic_update(writable_updates, txn)
        for doc_id in clock_updates:
            db.semantic_reinforced_set(doc_id, now.isoformat(), conn=txn)
    return len(updates)


def _decay_docs(now: datetime) -> int:
    """Decay docs. Their clock lives in ``doc_reinforced``, keyed ``doc:<name>``."""
    doc_vols = db.volume_map("doc")  # materialized
    anchors = db.decay_anchor_map()  # materialized
    updates: list[tuple[str, str, float]] = []
    anchor_updates: list[tuple[str, float]] = []
    stamps: list[str] = []
    for entity_key, stored in doc_vols.items():
        last_r = db.doc_reinforced_get(entity_key)
        step = _advance(entity_key, "doc", stored, last_r, now, anchors)
        if step is None:
            continue
        eff, age = step
        updates.append((entity_key, "doc", eff))
        anchor_updates.append((entity_key, age))
        stamps.append(entity_key)
    if not updates:
        return 0
    with db.write_txn() as txn:
        db.volume_set_many(updates, conn=txn)
        db.decay_anchor_set_many(anchor_updates, conn=txn)
        for entity_key in stamps:
            db.doc_reinforced_set(entity_key, now.isoformat(), conn=txn)
    return len(updates)


def sleep(
    readonly_semantic: Callable[[], Iterable[SemanticRow]] | None = None,
    writable_semantic: Callable[[], Iterable[SemanticRow]] | None = None,
    writable_semantic_update: (
        Callable[[list[SemanticRow], sqlite3.Connection], None] | None
    ) = None,
    log_event: LogEvent | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one decay cycle over facts, semantic memories and docs.

    Unlike a fixed multiplier this computes the ACTUAL decay from elapsed time,
    stores the decayed value and resets the clock. Memories are NEVER deleted,
    only made quieter (floor: MIN_VOLUME).

    Running nightly must land on the SAME volume as running once a month: each
    entity's step is charged as a ratio against its accumulated decay age (see
    :func:`decay_step`), never as a fresh full-strength decay of an already
    decayed value. ``now`` is the single instant the whole pass measures
    against — repeating a pass with the same ``now`` is a no-op.

    Each layer keeps its OWN transaction and its own error boundary: a failure
    in one layer must not roll back the layers that already succeeded. Every
    layer also MATERIALIZES its rows BEFORE looping — issuing UPDATEs while a
    cursor still walks ``volumes`` makes SQLite re-visit relocated rows, which
    drives every volume to the floor. Counters are only bumped AFTER the
    transaction commits, so a rollback never reports phantom decay.

    The semantic providers are injected so this module stays free of ChromaDB:
    ``server.py`` supplies its Chroma-backed iterators, ``dream.py`` accepts the
    store-only defaults.

    Returns per-layer counts plus ``total_decayed``.
    """
    now = as_local_naive(now or datetime.now())
    writable_semantic = writable_semantic or _default_writable_semantic
    writable_semantic_update = writable_semantic_update or _default_writable_update

    stats = {"fact": 0, "semantic": 0, "doc": 0, "total_decayed": 0}

    with contextlib.suppress(Exception):
        stats["fact"] = _decay_facts(now)
    with contextlib.suppress(Exception):
        stats["semantic"] = _decay_semantic(
            now, readonly_semantic, writable_semantic, writable_semantic_update
        )
    with contextlib.suppress(Exception):
        stats["doc"] = _decay_docs(now)

    stats["total_decayed"] = stats["fact"] + stats["semantic"] + stats["doc"]

    if log_event is not None:
        log_event("sleep_cycle", "decay", float(stats["total_decayed"]), "all")
    return stats


def format_sleep_report(stats: dict[str, int]) -> str:
    """Human-readable one-liner for a :func:`sleep` result."""
    return (
        f"Sleep cycle complete. Decayed: "
        f"{stats['fact']} facts, {stats['semantic']} semantic, {stats['doc']} docs "
        f"({stats['total_decayed']} total). "
        f"Formula: V * (1 + t/τ)^(-α), floor={MIN_VOLUME}"
    )
