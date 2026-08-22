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


def normalize_subject(text: str) -> str:
    """Collapse free text into a stable belief subject key."""
    flat = " ".join((text or "").split()).lower()
    flat = re.sub(r"[^\w./-]+", "_", flat, flags=re.UNICODE).strip("_")
    return flat[:120] or "unknown"


# ---------------------------------------------------------------------------
# Pass 1 — Ingest
# ---------------------------------------------------------------------------


def _unprocessed_l0(limit: int) -> list[dict]:
    """Raw records that no episode has claimed yet (``parent_id IS NULL``)."""
    with db.read_conn() as c:
        rows = c.execute(
            """
            SELECT * FROM causal_memories
            WHERE layer = ? AND parent_id IS NULL
            ORDER BY observed_at ASC
            LIMIT ?
            """,
            (L0_RAW, limit),
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


def _promote(
    members: list[dict],
    layer: int,
    headline: str,
    tags: str | None = None,
    dry_run: bool = False,
) -> str | None:
    """Create one parent row at ``layer`` and reparent its members onto it."""
    if len(members) < MIN_MEMBERS[layer]:
        return None
    member_ids = [m["id"] for m in members]
    parent_id = _synthetic_id(layer, member_ids)
    if dry_run:
        return parent_id

    with db.write_txn() as txn:
        db.causal_insert(
            id=parent_id,
            text=_summarize(members, headline),
            layer=layer,
            cause=members[0].get("cause"),
            effect=members[-1].get("effect"),
            confidence=min(1.0, _mean_confidence(members) * ABSTRACTION_DISCOUNT),
            source_ref=f"dream:pass2:l{layer}",
            session_id=members[0].get("session_id"),
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


def pass2_compress(l0_rows: list[dict], dry_run: bool = False) -> dict:
    """Build the abstraction ladder: episodes, then digests, then axioms.

    Each rung only consumes rows that no higher rung has claimed, so the ladder
    is stable across runs: yesterday's episodes are not re-episoded tonight.
    """
    stats = {"l1": 0, "l2": 0, "l3": 0}

    # L0 -> L1: one episode per session.
    for session, members in sorted(_group_l0_by_session(l0_rows).items()):
        label = session if session.startswith("day:") else f"session {session}"
        if _promote(members, L1_EPISODE, f"Episode ({label}):", dry_run=dry_run):
            stats["l1"] += 1

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


def belief_from(memory: dict) -> tuple[str, str, str, float] | None:
    """Project a causal memory onto a ``(subject, predicate, object)`` triple.

    Only records that carry BOTH a cause and an effect qualify: a belief is a
    claim about how the world reacts, and half of one is just an observation.
    """
    cause = (memory.get("cause") or "").strip()
    effect = (memory.get("effect") or "").strip()
    if not cause or not effect:
        return None
    confidence = float(memory.get("confidence") or 0.5)
    return normalize_subject(cause), "causes", " ".join(effect.split()), confidence


def _belief_candidates() -> list[dict]:
    """Abstracted memories (L1+), newest first — the freshest claim is the one
    that gets to speak for its subject this run."""
    with db.read_conn() as c:
        rows = c.execute(
            "SELECT * FROM causal_memories WHERE layer >= ? "
            "ORDER BY observed_at DESC, id DESC",
            (L1_EPISODE,),
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
    """Delete aged L0 rows that already have a parent.

    The parent check is what makes this safe rather than destructive: a raw
    record is only forgotten once its content survives inside an episode.
    """
    if dry_run:
        with db.read_conn() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) FROM causal_memories WHERE layer = 0 "
                    "AND parent_id IS NOT NULL "
                    "AND observed_at < datetime('now', '-' || ? || ' days')",
                    (l0_ttl_days,),
                ).fetchone()[0]
            )
    return db.causal_delete_l0_with_parent(l0_ttl_days)


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
        f"  Pass 2 compress:  {p2['l1']} episodes, {p2['l2']} digests, {p2['l3']} axioms\n"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db:
        db.set_db_path(args.db)
    else:
        db.init_schema()

    try:
        with single_writer(blocking=args.wait):
            stats = consolidate(
                l0_ttl_days=args.l0_ttl_days,
                ingest_limit=args.ingest_limit,
                dry_run=args.dry_run,
            )
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
