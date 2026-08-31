#!/usr/bin/env python3
"""Consolidation daemon — the memory system's sleep cycle.

Hooks write raw observations all day (L0 causal memories). Nothing in the hot
path is allowed to think: writes must stay cheap. This daemon is where the
thinking happens, offline, exactly once per night.

Five passes, in order — each one depends on the previous:

  Pass 1  Ingest      normalize unprocessed L0 records, fill missing cause/effect
  Pass 2  Compress    L0 -> L1 episodes -> L2 thematic digests -> L3 axioms
  Pass 3  Reconcile   promote beliefs, resolve contradictions via supersession
  Pass 4  Decay       volume.sleep() — the power-law forgetting pass
  Pass 5  Forget      drop L0 rows older than the TTL that already have a parent

Compression is deliberately LOSSY. An L1 episode summarizes its L0 members and
then those members are deleted (Pass 5). What survives is the abstraction, not
the transcript — that is the whole point: raw traces grow without bound, and a
memory that never forgets is a memory that cannot generalize.

Single-writer discipline
------------------------
This process takes an exclusive ``flock`` on ``<db>-dream.lock`` before it
touches anything. SQLite's own locking would serialize the individual writes,
but a consolidation run is only coherent as a WHOLE: two daemons interleaving
Pass 2 and Pass 3 would promote beliefs from half-built episodes. Non-blocking
by default — if a run is already in progress, this one exits quietly rather
than queueing up behind it.

Usage::

    python dream.py --nightly          # launchd / cron entrypoint (quiet)
    python dream.py                    # manual trigger (verbose)
    python dream.py --dry-run          # report what would change, write nothing
    python dream.py --json             # machine-readable stats
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import db
import volume

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Causal layers. 0 is the raw trace; 3 is a standing principle.
L0_RAW, L1_EPISODE, L2_DIGEST, L3_AXIOM = 0, 1, 2, 3

#: Minimum members required before a layer is worth abstracting over. One
#: memory is not a pattern — promoting it would just rename the original.
MIN_MEMBERS = {L1_EPISODE: 2, L2_DIGEST: 2, L3_AXIOM: 2}

#: Confidence assigned to a synthesized parent, as a fraction of its members'
#: mean confidence. Abstraction loses detail, so it must also lose certainty.
ABSTRACTION_DISCOUNT = 0.9

#: Characters of each member text folded into a synthesized summary.
SUMMARY_MEMBER_CHARS = 160
SUMMARY_MAX_MEMBERS = 8

#: L0 rows with a parent are deleted after this many days (Pass 5).
DEFAULT_L0_TTL_DAYS = 7

#: How much MORE confident an incumbent belief must be to survive a newer,
#: contradicting observation. Below this margin recency wins: the world changed
#: more often than the observer was wrong.
CONTRADICTION_MARGIN = 0.2

#: Cap on how many unprocessed L0 rows one run will ingest, so a runaway
#: producer cannot turn a nightly job into an all-night job.
DEFAULT_INGEST_LIMIT = 5000

#: A session that never gained a second member is not a pattern — but it must
#: not sit at the head of the ingest window forever either. Orphans are the
#: OLDEST rows, so ``ORDER BY observed_at ASC LIMIT n`` hands them the whole
#: budget every single night and fresh records are never reached. Once a lone
#: record is this old no sibling is still coming, so it is promoted as a
#: singleton episode: the budget frees and Pass 5 can eventually reclaim it.
SINGLETON_EPISODE_AFTER_DAYS = 2.0

#: How long the store must have been quiet before a scheduled poll does work.
#: Correctness no longer rides on this — ``SESSION_QUIET_HOURS`` decides what may
#: be folded — but Pass 4 rewrites hundreds of rows and there is no reason to do
#: that under someone's fingers. Measured here, the longest gap inside a live
#: session is 26 minutes, so 45 clears every real pause.
POLL_QUIET_MINUTES = 45.0

#: A whole day without a successful pass forces one regardless of quiet. Just
#: over 24h, so the once-a-day rule normally decides and this stays a fallback
#: for the day that was missed entirely — a machine that was off, most often.
MAX_STALE_HOURS = 26.0

#: A session is only folded into an episode once it has been silent this long.
#: Without this gate the ladder is a function of how OFTEN this runs rather than
#: of what happened: a pass landing mid-session freezes whatever rows exist at
#: that instant, and ``_synthetic_id`` hashes the member set, so the fragment
#: can never be merged back. Replaying one real day at 1, 9 and 27 passes
#: produced 12/26/42 episodes and 0/4/8 axioms from identical input — the
#: cadence, not the evidence, was deciding what became a standing principle.
#: The longest gap measured inside a live session here is 26 minutes, so six
#: hours only ever admits a session that is genuinely over, and every cadence
#: at or above one pass per window yields the same ladder.
SESSION_QUIET_HOURS = 6.0

#: "X because Y" / "X -> Y" style cues used to recover a cause/effect pair from
#: free text when the hook did not supply one.
_CAUSE_EFFECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?P<effect>.+?)\s+because\s+(?P<cause>.+)$", re.I | re.S),
    re.compile(r"^(?P<cause>.+?)\s*(?:->|→|=>)\s*(?P<effect>.+)$", re.S),
    re.compile(r"^(?P<cause>.+?)\s+(?:so|therefore|hence)\s+(?P<effect>.+)$", re.I | re.S),
    re.compile(r"^(?P<effect>.+?)\s+потому что\s+(?P<cause>.+)$", re.I | re.S),
    re.compile(r"^(?P<cause>.+?)\s+поэтому\s+(?P<effect>.+)$", re.I | re.S),
)


# ---------------------------------------------------------------------------
# Single-writer lock
# ---------------------------------------------------------------------------


def lock_path() -> Path:
    """Sidecar lock file next to the database: ``memory.db-dream.lock``."""
    return db.DB_PATH.parent / f"{db.DB_PATH.name}-dream.lock"


class DreamLockBusy(RuntimeError):
    """Raised when another consolidation run already holds the lock."""


@contextmanager
def single_writer(blocking: bool = False) -> Iterator[Path]:
    """Hold an exclusive flock for the whole run.

    The lock file is intentionally never unlinked: deleting it would let a
    second process create a fresh inode and flock *that* while we still hold
    the old one, which is exactly the race the lock exists to prevent.
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            raise DreamLockBusy(f"another dream run holds {path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {_now_iso()}\n".encode())
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# When to run
# ---------------------------------------------------------------------------


def state_path() -> Path:
    """Sidecar recording the last SUCCESSFUL consolidation."""
    return db.DB_PATH.parent / f"{db.DB_PATH.name}-dream.state"


def read_last_success() -> datetime | None:
    """The last completed pass, or None when there is no readable record.

    Deliberately NOT the lock file. That one is stamped on acquisition, before
    the work — so a ``--dry-run`` advances it, and so does a pass that dies in
    the middle. A daemon crash-looping every poll would keep satisfying the
    staleness fallback and disable the very failsafe meant to catch it. An
    unreadable record therefore counts as "never ran": erring toward one extra
    pass is free, erring toward silence is not.
    """
    try:
        return _parse_observed(state_path().read_text().strip())
    except OSError:
        return None


def write_last_success(moment: datetime) -> None:
    """Record a completed pass atomically — a torn file must read as 'never'."""
    path = state_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{moment.isoformat()}\n")
    os.replace(tmp, path)


def _exclude_telemetry() -> tuple[str, list[str]]:
    """``AND``-prefixed form of the shared predicate, for this module's L0 queries.

    The membership test itself lives in ``db.telemetry_tag_predicate`` because
    ``pass5_forget`` needs its exact negation to collect what is skipped here.
    Two spellings of "is this row telemetry" would eventually disagree, and the
    disagreement is silent: rows either consolidated twice or reaped forever.
    """
    clause, params = db.telemetry_tag_predicate(match=False)
    return f" AND {clause}", params


def newest_l0() -> datetime | None:
    """When the store last took in a raw observation WORTH WAITING ON.

    This is the agent's clock, not the human's: HID idle time would call a long
    unattended task "quiet". ``events`` would be worse still — naive local time
    against this UTC one is a five-hour error, and it is a ring buffer besides.

    Telemetry is excluded because it made this clock measure the wrong thing.
    A session end bumps it every time the user stops typing and a failing Bash
    bumps it mid-thought; together they are 371 of the store's 394 L0 rows. So
    "raw data is still arriving" read as "the agent is alive", every 15-minute
    poll was talked out of working for as long as anyone was working, and in the
    last 24h the only pass that landed came in through ``MAX_STALE_HOURS``.
    Quiet has to mean no signal arrived, not that the agent stopped breathing.
    """
    clause, params = _exclude_telemetry()
    with db.read_conn() as c:
        row = c.execute(
            "SELECT MAX(observed_at) AS newest FROM causal_memories "
            f"WHERE layer = 0{clause}",
            params,
        ).fetchone()
    return _parse_observed(row["newest"] if row else None)


def should_run(now: datetime | None = None) -> tuple[bool, str]:
    """Decide whether this poll consolidates, and say why either way.

    Time is the trigger, not a hook and not a fixed hour. A hook cannot fire for
    a machine that was asleep; an hour cannot fire for one that was off, because
    launchd drops a missed calendar slot entirely once the machine is powered
    down. Asking "how long since the last successful pass" survives both.

    A successful pass is capped at one per calendar day. Pass 2 no longer cares
    how often it runs, but the rungs above it still have no settling gate of
    their own — without the cap, cadence would go back to deciding how many
    digests and axioms a day produces.
    """
    now = now or datetime.now(UTC)
    last = read_last_success()
    if last is None:
        return True, "no successful pass on record"

    stale_hours = (now - last).total_seconds() / 3600.0
    if stale_hours >= MAX_STALE_HOURS:
        return True, f"{stale_hours:.0f}h since the last pass"
    if last.astimezone().date() == now.astimezone().date():
        return False, "already consolidated today"

    newest = newest_l0()
    if newest is None:
        return True, "nothing has arrived to wait on"
    quiet_minutes = (now - newest).total_seconds() / 60.0
    if quiet_minutes < POLL_QUIET_MINUTES:
        return False, f"only {quiet_minutes:.0f}m quiet"
    return True, f"{quiet_minutes:.0f}m quiet"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _synthetic_id(layer: int, member_ids: list[str]) -> str:
    """Deterministic id for a synthesized parent.

    Derived from its members, so re-running the daemon on unchanged input
    rebuilds the SAME row (causal_insert upserts) instead of growing a new
    duplicate abstraction every night.
    """
    digest = hashlib.sha1("|".join(sorted(member_ids)).encode()).hexdigest()[:16]
    return f"l{layer}:{digest}"


def _split_tags(raw: str | None) -> list[str]:
    return [t for t in (raw or "").split(",") if t]


def _merge_tags(rows: list[dict]) -> str:
    """Union of member tags, most frequent first, capped to keep rows small."""
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for tag in _split_tags(row.get("tags")):
            counts[tag] += 1
    ordered = sorted(counts, key=lambda t: (-counts[t], t))
    return ",".join(ordered[:12])


def _mean_confidence(rows: list[dict]) -> float:
    if not rows:
        return 0.5
    total = sum(float(r.get("confidence") or 0.5) for r in rows)
    return total / len(rows)


def _summarize(rows: list[dict], headline: str) -> str:
    """Fold member texts into one lossy summary line-set."""
    parts = [headline]
    for row in rows[:SUMMARY_MAX_MEMBERS]:
        text = " ".join((row.get("text") or "").split())
        if len(text) > SUMMARY_MEMBER_CHARS:
            text = text[:SUMMARY_MEMBER_CHARS] + "…"
        parts.append(f"- {text}")
    if len(rows) > SUMMARY_MAX_MEMBERS:
        parts.append(f"- (+{len(rows) - SUMMARY_MAX_MEMBERS} more)")
    return "\n".join(parts)


def extract_cause_effect(text: str) -> tuple[str, str] | None:
    """Recover a ``(cause, effect)`` pair from free text, or None.

    Strictly heuristic and strictly optional: a miss simply leaves the record
    without a causal pair, which only means it will not be promoted to a belief
    in Pass 3. Guessing wrong here would poison belief state, so the patterns
    are narrow on purpose.
    """
    flat = " ".join((text or "").split())
    if not flat:
        return None
    for pattern in _CAUSE_EFFECT_PATTERNS:
        m = pattern.match(flat)
        if m:
            cause = m.group("cause").strip(" .;,")
            effect = m.group("effect").strip(" .;,")
            if cause and effect:
                return cause, effect
    return None


#: Cap for one segment of a generated belief subject.
#:
#: The old cap was 120, and it was not a cap but a guillotine: all 95 subjects
#: dream had written hit it to the character, because the input was a whole
#: sentence and the key had to stay unique per row. A key that must encode its
#: own row to stay unique is not a key — `belief_get_active` could never be
#: called with one, only a full table scan could find it.
SUBJECT_SEGMENT_CHARS: int = 48

#: Predicate under which a distilled axiom is asserted.
AXIOM_PREDICATE: str = "axiom"

#: Cap on the object side of a projected belief.
BELIEF_OBJECT_CHARS: int = 600


def normalize_subject(text: str, limit: int = SUBJECT_SEGMENT_CHARS) -> str:
    """Collapse free text into a stable belief subject key."""
    flat = " ".join((text or "").split()).lower()
    flat = re.sub(r"[^\w./-]+", "_", flat, flags=re.UNICODE).strip("_")
    return flat[:limit] or "unknown"


# ---------------------------------------------------------------------------
# Pass 1 — Ingest
# ---------------------------------------------------------------------------


def _unprocessed_l0(limit: int) -> list[dict]:
    """Raw records that no episode has claimed yet (``parent_id IS NULL``).

    Telemetry is excluded IN THE QUERY, never by filtering the returned list.
    Those rows are never promoted, so nothing ever sets their ``parent_id``:
    selecting them and discarding them afterwards would hand 371 rows the oldest
    end of every ``LIMIT``-sized window for the whole of their TTL, on every
    single run. That is the starvation ``SINGLETON_EPISODE_AFTER_DAYS`` exists
    to cure, with no cure available — a telemetry row has no singleton rescue
    waiting for it. Their only exit is ``pass5_forget``, which reaps them on age
    ALONE precisely because they will never earn a parent to be reaped behind.
    """
    clause, params = _exclude_telemetry()
    with db.read_conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM causal_memories
            WHERE layer = ? AND parent_id IS NULL{clause}
            ORDER BY observed_at ASC
            LIMIT ?
            """,
            (L0_RAW, *params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def pass1_ingest(limit: int = DEFAULT_INGEST_LIMIT, dry_run: bool = False) -> dict:
    """Normalize unprocessed L0 records and hand them to Pass 2.

    Hooks are fire-and-forget: they capture text and little else. Here we do the
    work they skipped — recover cause/effect pairs where the shape of the
    sentence allows it — so the later passes have structure to reason over.
    """
    rows = _unprocessed_l0(limit)
    enriched = 0
    updates: list[tuple[str, str, str]] = []

    for row in rows:
        if row.get("cause") or row.get("effect"):
            continue
        pair = extract_cause_effect(row.get("text") or "")
        if pair is None:
            continue
        cause, effect = pair
        row["cause"], row["effect"] = cause, effect
        updates.append((cause, effect, row["id"]))
        enriched += 1

    if updates and not dry_run:
        with db.write_txn() as txn:
            txn.executemany(
                "UPDATE causal_memories SET cause = ?, effect = ? WHERE id = ?",
                updates,
            )

    return {"scanned": len(rows), "enriched": enriched, "rows": rows}


# ---------------------------------------------------------------------------
# Pass 2 — Lossy compression L0 -> L1 -> L2 -> L3
# ---------------------------------------------------------------------------


def _group_l0_by_session(rows: list[dict]) -> dict[str, list[dict]]:
    """Episodes are sessions. Sessionless rows fall back to their calendar day."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = row.get("session_id") or f"day:{(row.get('observed_at') or '')[:10]}"
        groups[key].append(row)
    return groups


def _group_by_dominant_tag(rows: list[dict]) -> dict[str, list[dict]]:
    """Themes are tags. A row with no tags is untagged, not unthemed."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        tags = _split_tags(row.get("tags"))
        groups[tags[0] if tags else "untagged"].append(row)
    return groups


def _coherent_pair(members: list[dict]) -> tuple[str | None, str | None]:
    """Inherit a cause/effect pair ONLY when every member states the same one.

    Taking ``members[0].cause`` with ``members[-1].effect`` welds the cause of
    one trace onto the effect of another — at L2/L3 those traces come from
    different sessions and different topics entirely. Pass 3 then promotes that
    invention to an ACTIVE belief, so the memory ends up asserting a causal
    claim nobody ever observed. Diverse members therefore carry NO pair: a
    missing belief is recoverable, a fabricated one is not.

    Silence is diversity too. Discarding the members that state no pair and then
    agreeing with whoever is left lets a digest of twelve episodes inherit the
    pair of the one episode that had one, and assert it on behalf of the other
    eleven — observed here as ``l2:94ef7d7debec6f62`` claiming
    ``tool_call:Bash -> tool_error`` on the strength of a single member. A pair
    is inherited only when EVERY member states it.
    """
    paired = [
        (" ".join((m.get("cause") or "").split()), " ".join((m.get("effect") or "").split()))
        for m in members
        if (m.get("cause") or "").strip() and (m.get("effect") or "").strip()
    ]
    if not members or len(paired) != len(members):
        return (None, None)
    pairs = set(paired)
    return pairs.pop() if len(pairs) == 1 else (None, None)


def _shared_session(members: list[dict]) -> str | None:
    """The members' session id when they agree, else None — never an arbitrary one."""
    sessions = {m.get("session_id") for m in members}
    return sessions.pop() if len(sessions) == 1 else None


def _promote(
    members: list[dict],
    layer: int,
    headline: str,
    tags: str | None = None,
    dry_run: bool = False,
    min_members: int | None = None,
) -> str | None:
    """Create one parent row at ``layer`` and reparent its members onto it."""
    required = MIN_MEMBERS[layer] if min_members is None else max(1, min_members)
    if len(members) < required:
        return None
    member_ids = [m["id"] for m in members]
    parent_id = _synthetic_id(layer, member_ids)
    if dry_run:
        return parent_id

    cause, effect = _coherent_pair(members)
    with db.write_txn() as txn:
        db.causal_insert(
            id=parent_id,
            text=_summarize(members, headline),
            layer=layer,
            cause=cause,
            effect=effect,
            confidence=min(1.0, _mean_confidence(members) * ABSTRACTION_DISCOUNT),
            source_ref=f"dream:pass2:l{layer}",
            session_id=_shared_session(members),
            observed_at=min(m.get("observed_at") or "" for m in members) or None,
            tags=tags if tags is not None else _merge_tags(members),
            conn=txn,
        )
        # Reparent AFTER the parent exists: parent_id is a FK.
        txn.executemany(
            "UPDATE causal_memories SET parent_id = ? WHERE id = ?",
            [(parent_id, mid) for mid in member_ids],
        )
    return parent_id


def _orphans_at(layer: int) -> list[dict]:
    with db.read_conn() as c:
        rows = c.execute(
            "SELECT * FROM causal_memories WHERE layer = ? AND parent_id IS NULL "
            "ORDER BY observed_at ASC",
            (layer,),
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_observed(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_settled(members: list[dict], now: datetime, after_days: float) -> bool:
    """True once the group's NEWEST member is old enough that no sibling is coming.

    An unreadable clock counts as settled: a row that cannot be dated must not
    be allowed to hold the ingest budget hostage forever.
    """
    newest = max((_parse_observed(m.get("observed_at")) for m in members), default=None,
                 key=lambda dt: dt or datetime.min.replace(tzinfo=UTC))
    if newest is None:
        return True
    return (now - newest).total_seconds() >= after_days * 86400.0


def pass2_compress(
    l0_rows: list[dict],
    dry_run: bool = False,
    now: datetime | None = None,
    singleton_after_days: float = SINGLETON_EPISODE_AFTER_DAYS,
    quiet_hours: float = SESSION_QUIET_HOURS,
) -> dict:
    """Build the abstraction ladder: episodes, then digests, then axioms.

    Each rung only consumes rows that no higher rung has claimed, so the ladder
    is stable across runs: yesterday's episodes are not re-episoded tonight.

    A session is not folded until it has been silent for ``quiet_hours``. An
    episode is meant to describe a whole session, and a pass landing mid-session
    would mint one from whatever rows happened to exist at that instant; since
    the member set can no longer change once the window has elapsed, the ladder
    comes out the same whether this runs once a day or nine times — see
    ``SESSION_QUIET_HOURS``.

    A session below ``MIN_MEMBERS`` normally waits for a sibling that may still
    arrive. Once it is older than ``singleton_after_days`` that wait is over and
    it is promoted alone — see ``SINGLETON_EPISODE_AFTER_DAYS``.
    """
    now = now or datetime.now(UTC)
    stats = {"l1": 0, "l1_singletons": 0, "l1_deferred": 0, "l2": 0, "l3": 0}

    # L0 -> L1: one episode per session, once that session has fallen silent.
    for session, members in sorted(_group_l0_by_session(l0_rows).items()):
        if not _is_settled(members, now, quiet_hours / 24.0):
            stats["l1_deferred"] += 1
            continue
        label = session if session.startswith("day:") else f"session {session}"
        singleton = len(members) < MIN_MEMBERS[L1_EPISODE] and _is_settled(
            members, now, singleton_after_days
        )
        if _promote(
            members,
            L1_EPISODE,
            f"Episode ({label}):",
            dry_run=dry_run,
            min_members=1 if singleton else None,
        ):
            stats["l1"] += 1
            stats["l1_singletons"] += int(singleton)

    # L1 -> L2: one thematic digest per dominant tag.
    for tag, members in sorted(_group_by_dominant_tag(_orphans_at(L1_EPISODE)).items()):
        if _promote(members, L2_DIGEST, f"Theme «{tag}»:", tags=tag, dry_run=dry_run):
            stats["l2"] += 1

    # L2 -> L3: an axiom is a theme that kept recurring across digests.
    for tag, members in sorted(_group_by_dominant_tag(_orphans_at(L2_DIGEST)).items()):
        if _promote(members, L3_AXIOM, f"Axiom «{tag}»:", tags=tag, dry_run=dry_run):
            stats["l3"] += 1

    return stats


# ---------------------------------------------------------------------------
# Pass 3 — Contradiction resolution & belief promotion
# ---------------------------------------------------------------------------


def _axiom_theme(memory: dict) -> str:
    """The namespace an axiom belongs to: its first tag, or a bare fallback."""
    first = str(memory.get("tags") or "").split(",")[0].strip()
    return normalize_subject(first) if first else AXIOM_PREDICATE


def _axiom_claim(said: str) -> str:
    """The discriminating head of an axiom, with its own title stripped.

    A synthesized axiom opens with ``Axiom «theme»:`` and then its bullets. That
    header only restates the theme, so leaving it in would make every axiom
    under one theme share a prefix and discriminate nothing.
    """
    body = re.sub(r"^\s*axiom\s*«[^»]*»\s*:?\s*", "", said, flags=re.IGNORECASE)
    return body.lstrip("-–—•* \t")


def belief_from(memory: dict) -> tuple[str, str, str, float] | None:
    """Project a memory onto a ``(subject, predicate, object)`` triple.

    ONLY axioms qualify. A belief answers "what is currently true of X"; every
    rung below L3 answers "what happened once", and those are different shapes.
    Forcing the lower rungs in is what produced 95 of the live store's 96
    beliefs with a whole sentence for a subject and ``causes`` for a predicate:
    a log entry has no identity smaller than itself, so the key had to grow
    until it swallowed the row, and then nothing could look it up.

    The subject is a dotted namespace, ``<theme>.<claim>``, which is the
    convention the schema already documents (``user.preferences``,
    ``dropweb.testing``) and which ``belief_all_active`` enumerates by prefix.
    Nine dropweb axioms therefore stay nine addressable beliefs rather than
    eight superseding each other into one arbitrary survivor.
    """
    if int(memory.get("layer") or 0) != L3_AXIOM:
        return None
    said = " ".join(str(memory.get("text") or "").split())
    if not said:
        return None
    theme = _axiom_theme(memory)
    claim = normalize_subject(_axiom_claim(said))
    subject = theme if claim == "unknown" else f"{theme}.{claim}"
    confidence = float(memory.get("confidence") or 0.5)
    return subject, AXIOM_PREDICATE, said[:BELIEF_OBJECT_CHARS], confidence


def _belief_candidates() -> list[dict]:
    """Axioms, newest first — the freshest distillation speaks for its subject.

    Scoped to L3 because that is the only rung ``belief_from`` accepts; selecting
    L1 and L2 as well would read thousands of rows to discard every one.
    """
    with db.read_conn() as c:
        rows = c.execute(
            "SELECT * FROM causal_memories WHERE layer = ? "
            "ORDER BY observed_at DESC, id DESC",
            (L3_AXIOM,),
        ).fetchall()
    return [dict(r) for r in rows]


def belief_id(subject: str, predicate: str, object_val: str, valid_from: str) -> str:
    """Deterministic belief id.

    ``valid_from`` is part of the digest on purpose. Without it a belief that
    flip-flops (A -> B -> A) would try to re-insert a primary key that already
    sits in the table as a superseded row. With it, every assertion is a
    distinct historical record while a RE-run of the same evidence still
    produces the same id — idempotent, but not amnesiac.
    """
    raw = f"{subject}|{predicate}|{object_val}|{valid_from}"
    return f"belief:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"


def pass3_reconcile(dry_run: bool = False) -> dict:
    """Promote consolidated memories into belief state, resolving conflicts.

    Candidates are first collapsed to ONE winner per ``(subject, predicate)``:
    the most recent observation. Two contradicting memories from the same night
    must not both be asserted — asserting them in sequence would leave the older
    one as a superseded row that the next run would try to insert all over
    again. Reconciliation is a decision, not a replay.

    The winner is then compared to the incumbent belief:

    * no incumbent            -> assert
    * incumbent agrees        -> unchanged (never churn a stable belief)
    * incumbent disagrees     -> the newer claim wins UNLESS the incumbent is
      more than CONTRADICTION_MARGIN more confident, in which case the claim is
      rejected and the incumbent stands. Recency usually beats confidence: the
      world changes more often than the observer was wrong.

    Supersession is handled by ``db.belief_assert``, which atomically flips the
    incumbent to ``status='superseded'`` and back-links ``superseded_by`` — the
    old belief is never destroyed, only dated. History has to stay inspectable
    or "why did I change my mind" becomes unanswerable.
    """
    stats = {
        "asserted": 0,
        "superseded": 0,
        "unchanged": 0,
        "rejected": 0,
        "shadowed": 0,
    }

    winners: dict[tuple[str, str], tuple[dict, tuple[str, str, str, float]]] = {}
    for memory in _belief_candidates():  # newest first
        triple = belief_from(memory)
        if triple is None:
            continue
        key = (triple[0], triple[1])
        if key in winners:
            stats["shadowed"] += 1
            continue
        winners[key] = (memory, triple)

    for memory, (subject, predicate, object_val, confidence) in winners.values():
        incumbent = db.belief_get_active(subject, predicate)
        if incumbent is not None:
            if incumbent["object"] == object_val:
                stats["unchanged"] += 1
                continue
            if float(incumbent.get("confidence") or 0.0) > confidence + CONTRADICTION_MARGIN:
                stats["rejected"] += 1
                continue

        valid_from = memory.get("observed_at") or _now_iso()
        if not dry_run:
            db.belief_assert(
                id=belief_id(subject, predicate, object_val, valid_from),
                subject=subject,
                predicate=predicate,
                object_val=object_val,
                confidence=confidence,
                valid_from=valid_from,
                evidence_id=memory["id"],
                source="dream",
            )
        stats["asserted"] += 1
        if incumbent is not None:
            stats["superseded"] += 1

    return stats


# ---------------------------------------------------------------------------
# Pass 4 / Pass 5
# ---------------------------------------------------------------------------


def pass4_decay(dry_run: bool = False) -> dict[str, int]:
    """Run the shared power-law decay pass (identical to the server's sleep tool)."""
    if dry_run:
        return {"fact": 0, "semantic": 0, "doc": 0, "total_decayed": 0}
    return volume.sleep()


def pass5_forget(l0_ttl_days: int = DEFAULT_L0_TTL_DAYS, dry_run: bool = False) -> int:
    """Delete aged L0 rows that consolidation is finished with.

    A real observation is finished with once an episode carries its content —
    that is the parent check, and it is what makes this compaction rather than
    data loss. Telemetry is finished with the moment it is written, since no
    episode will ever claim it; see ``db.causal_delete_l0_reapable``, which owns
    both halves of that rule so the dry-run count cannot drift from the delete.
    """
    return db.causal_delete_l0_reapable(l0_ttl_days, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def consolidate(
    l0_ttl_days: int = DEFAULT_L0_TTL_DAYS,
    ingest_limit: int = DEFAULT_INGEST_LIMIT,
    dry_run: bool = False,
) -> dict:
    """Run all five passes in order. Assumes the caller holds the writer lock."""
    started = datetime.now(UTC)

    ingest = pass1_ingest(limit=ingest_limit, dry_run=dry_run)
    compress = pass2_compress(ingest["rows"], dry_run=dry_run)
    reconcile = pass3_reconcile(dry_run=dry_run)
    decay = pass4_decay(dry_run=dry_run)
    forgotten = pass5_forget(l0_ttl_days=l0_ttl_days, dry_run=dry_run)

    return {
        "started_at": started.isoformat(),
        "duration_sec": round(
            (datetime.now(UTC) - started).total_seconds(), 3
        ),
        "dry_run": dry_run,
        "pass1_ingest": {"scanned": ingest["scanned"], "enriched": ingest["enriched"]},
        "pass2_compress": compress,
        "pass3_reconcile": reconcile,
        "pass4_decay": decay,
        "pass5_forget": {"l0_deleted": forgotten, "ttl_days": l0_ttl_days},
    }


def format_report(stats: dict) -> str:
    p1, p2, p3 = stats["pass1_ingest"], stats["pass2_compress"], stats["pass3_reconcile"]
    p4, p5 = stats["pass4_decay"], stats["pass5_forget"]
    prefix = "[dry-run] " if stats.get("dry_run") else ""
    return (
        f"{prefix}Dream cycle complete in {stats['duration_sec']}s\n"
        f"  Pass 1 ingest:    {p1['scanned']} scanned, {p1['enriched']} enriched\n"
        f"  Pass 2 compress:  {p2['l1']} episodes ({p2.get('l1_singletons', 0)} singleton), "
        f"{p2['l2']} digests, {p2['l3']} axioms"
        f" — {p2.get('l1_deferred', 0)} sessions still live\n"
        f"  Pass 3 reconcile: {p3['asserted']} asserted, {p3['superseded']} superseded, "
        f"{p3['rejected']} rejected, {p3['unchanged']} unchanged, {p3['shadowed']} shadowed\n"
        f"  Pass 4 decay:     {p4['total_decayed']} volumes "
        f"({p4['fact']} facts, {p4['semantic']} semantic, {p4['doc']} docs)\n"
        f"  Pass 5 forget:    {p5['l0_deleted']} L0 records older than {p5['ttl_days']}d"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dream.py",
        description="Batch memory consolidation daemon for crystallized.",
    )
    parser.add_argument(
        "--nightly",
        action="store_true",
        help="Scheduled run (launchd/cron): quiet unless something changed or failed.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable stats.")
    parser.add_argument("--db", help="Override the database path (default: $OPENCODE_MEMORY_DB).")
    parser.add_argument(
        "--l0-ttl-days",
        type=int,
        default=DEFAULT_L0_TTL_DAYS,
        help=f"Age after which parented L0 records are dropped (default: {DEFAULT_L0_TTL_DAYS}).",
    )
    parser.add_argument(
        "--ingest-limit",
        type=int,
        default=DEFAULT_INGEST_LIMIT,
        help=f"Max L0 records ingested per run (default: {DEFAULT_INGEST_LIMIT}).",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Block on the writer lock instead of exiting when a run is in progress.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Consolidate now, ignoring the quiet window and the once-a-day cap.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db:
        db.set_db_path(args.db)
    else:
        db.init_schema()

    # A dry run writes nothing, so it cannot skew the ladder and is never gated.
    gated = not (args.force or args.dry_run)
    if gated:
        go, why = should_run()
        if not go:
            if not args.nightly:
                print(f"dream: skipped — {why} (--force overrides)", file=sys.stderr)
            return 0

    try:
        with single_writer(blocking=args.wait):
            # Two polls can both pass the gate before either takes the lock.
            if gated and not should_run()[0]:
                return 0
            stats = consolidate(
                l0_ttl_days=args.l0_ttl_days,
                ingest_limit=args.ingest_limit,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                write_last_success(datetime.now(UTC))
    except DreamLockBusy as exc:
        # Not an error: the nightly job overlapping a manual run is normal.
        print(f"dream: skipped — {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001 — a daemon must report, not traceback
        print(f"dream: FAILED — {exc!r}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    elif not args.nightly or stats["pass1_ingest"]["scanned"]:
        print(format_report(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
