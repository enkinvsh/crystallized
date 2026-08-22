"""SQLite storage layer for the opencode-memory MCP server (v2.0).

Handles durable storage for:
1. Facts, volumes, doc reinforcement, semantic fallbacks, event log (v1)
2. Causal memories and bi-temporal belief state with automatic supersession (v2)

Design notes:
* ONE shared connection guarded by an RLock. check_same_thread=False.
* isolation_level=None (autocommit) + explicit BEGIN IMMEDIATE in write_txn.
* The volumes index is (layer) ONLY.
* PRAGMA user_version migration runner.
* Bi-temporal belief state uses partial unique index for active beliefs.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".config" / "opencode" / "memory" / "memory.db"
DB_PATH: Path = Path(os.environ.get("OPENCODE_MEMORY_DB") or _DEFAULT_DB_PATH)

EVENTS_MAXLEN = 100000
_EVENTS_TRIM_PROBABILITY = 0.001
_DEFAULT_TTL_DAYS = 60

# ---------------------------------------------------------------------------
# Schemas & Migrations
# ---------------------------------------------------------------------------

_V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS facts (
  key                TEXT PRIMARY KEY,
  value              TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  last_reinforced_at TEXT NOT NULL,
  ttl_days           INTEGER NOT NULL,
  expires_at         TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS volumes (
  entity_key TEXT PRIMARY KEY,
  layer      TEXT NOT NULL,
  volume     REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_volumes_layer ON volumes(layer);

CREATE TABLE IF NOT EXISTS doc_reinforced (
  entity_key         TEXT PRIMARY KEY,
  last_reinforced_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS semantic_fallback (
  doc_id TEXT PRIMARY KEY, text TEXT NOT NULL, metadata TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS semantic_reinforced (
  doc_id             TEXT PRIMARY KEY,
  last_reinforced_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  entry_id TEXT, event_type TEXT, volume REAL, layer TEXT, timestamp TEXT);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id);
"""

_V2_SCHEMA_SQL = """
-- Episodic / causal trace (L0 raw -> L3 principle)
CREATE TABLE IF NOT EXISTS causal_memories (
  id          TEXT PRIMARY KEY,
  text        TEXT NOT NULL,
  layer       INTEGER NOT NULL DEFAULT 0,  -- 0: raw, 1: episode, 2: pattern, 3: principle
  cause       TEXT,
  effect      TEXT,
  confidence  REAL NOT NULL DEFAULT 0.5,
  source_ref  TEXT,
  session_id  TEXT,
  observed_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  parent_id   TEXT REFERENCES causal_memories(id) ON DELETE SET NULL,
  tags        TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_causal_layer    ON causal_memories(layer);
CREATE INDEX IF NOT EXISTS idx_causal_session  ON causal_memories(session_id);
CREATE INDEX IF NOT EXISTS idx_causal_parent   ON causal_memories(parent_id);
CREATE INDEX IF NOT EXISTS idx_causal_observed ON causal_memories(observed_at);

-- Bi-temporal belief state with explicit supersession
CREATE TABLE IF NOT EXISTS belief_state (
  id            TEXT PRIMARY KEY,
  subject       TEXT NOT NULL,      -- normalized entity (e.g. "project.testing")
  predicate     TEXT NOT NULL,      -- property (e.g. "test_runner")
  object        TEXT NOT NULL,      -- value / directive (e.g. "vitest")
  status        TEXT NOT NULL DEFAULT 'active',  -- active | superseded | exception | disputed
  confidence    REAL NOT NULL DEFAULT 0.8,
  valid_from    TEXT NOT NULL,
  valid_to      TEXT,
  recorded_at   TEXT NOT NULL,
  superseded_by TEXT REFERENCES belief_state(id) ON DELETE SET NULL,
  supersedes    TEXT,
  evidence_id   TEXT,
  source        TEXT NOT NULL DEFAULT 'observer');
CREATE UNIQUE INDEX IF NOT EXISTS idx_belief_active
  ON belief_state(subject, predicate) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_belief_subject ON belief_state(subject);
CREATE INDEX IF NOT EXISTS idx_belief_status  ON belief_state(status);
"""

_V3_SCHEMA_SQL = """
-- Accumulated decay age, in hours, since the last genuine reinforcement.
-- A power law does NOT compose: applying it per-step to an already-decayed
-- value forgets far faster than applying it once over the total age. Keeping
-- the running age lets sleep() advance along the ORIGINAL curve instead.
CREATE TABLE IF NOT EXISTS decay_anchor (
  entity_key TEXT PRIMARY KEY,
  age_hours  REAL NOT NULL DEFAULT 0);
"""

MIGRATIONS: dict[int, str] = {
    1: _V1_SCHEMA_SQL,
    2: _V2_SCHEMA_SQL,
    3: _V3_SCHEMA_SQL,
}

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_db: sqlite3.Connection | None = None
_db_lock = threading.RLock()
_txn_depth = 0


def get_db() -> sqlite3.Connection:
    """Return the process-wide SQLite connection, opening it if needed."""
    global _db
    with _db_lock:
        if _db is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(
                str(DB_PATH),
                check_same_thread=False,
                isolation_level=None,
                timeout=5.0,
            )
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            _db = c
            _init_schema_on(c)
        return _db


def close_db() -> None:
    """Close the shared connection and reset transaction bookkeeping."""
    global _db, _txn_depth
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except sqlite3.Error:
                pass
        _db = None
        _txn_depth = 0


def set_db_path(path: str | Path) -> None:
    """Repoint the store at a different database file. FOR TESTS."""
    global DB_PATH
    with _db_lock:
        close_db()
        DB_PATH = Path(path)
        init_schema()


def _init_schema_on(conn: sqlite3.Connection) -> None:
    cur_row = conn.execute("PRAGMA user_version").fetchone()
    cur_ver = int(cur_row[0]) if cur_row else 0
    for v in sorted(MIGRATIONS):
        if v > cur_ver:
            conn.executescript(MIGRATIONS[v])
            conn.execute(f"PRAGMA user_version = {v}")


def init_schema() -> None:
    """Create tables/indexes and run pending migrations. Idempotent."""
    with _db_lock:
        _init_schema_on(get_db())


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@contextmanager
def write_txn() -> Iterator[sqlite3.Connection]:
    """Exclusive write transaction with explicit BEGIN IMMEDIATE."""
    global _txn_depth
    with _db_lock:
        c = get_db()
        if _txn_depth > 0:
            _txn_depth += 1
            try:
                yield c
            finally:
                _txn_depth -= 1
            return

        c.execute("BEGIN IMMEDIATE")
        _txn_depth = 1
        try:
            yield c
        except BaseException:
            _txn_depth = 0
            c.execute("ROLLBACK")
            raise
        else:
            _txn_depth = 0
            c.execute("COMMIT")


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    """Read access to the shared connection, serialized by the lock."""
    with _db_lock:
        yield get_db()


def _exec(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> None:
    """Run a write statement, joining the caller's txn or opening our own."""
    if conn is not None:
        conn.execute(sql, params)
        return
    with write_txn() as c:
        c.execute(sql, params)


# ---------------------------------------------------------------------------
# Facts (Layer 1)
# ---------------------------------------------------------------------------

_FACT_OPTIONAL_FIELDS = ("updated_at", "last_reinforced_at", "ttl_days", "expires_at")


def _row_to_fact(row: sqlite3.Row) -> dict:
    d: dict = {"value": row["value"]}
    for f in _FACT_OPTIONAL_FIELDS:
        if row[f] is not None:
            d[f] = row[f]
    return d


def fact_get(key: str) -> dict | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT key, value, updated_at, last_reinforced_at, ttl_days, expires_at "
            "FROM facts WHERE key = ?",
            (key,),
        ).fetchone()
    return _row_to_fact(row) if row is not None else None


def fact_all() -> dict[str, dict]:
    with read_conn() as c:
        rows = c.execute(
            "SELECT key, value, updated_at, last_reinforced_at, ttl_days, expires_at "
            "FROM facts ORDER BY key"
        ).fetchall()
    return {row["key"]: _row_to_fact(row) for row in rows}


def fact_keys() -> list[str]:
    with read_conn() as c:
        rows = c.execute("SELECT key FROM facts ORDER BY key").fetchall()
    return [row["key"] for row in rows]


def _fact_params(key: str, parsed: dict) -> tuple:
    now_iso = datetime.now().isoformat()
    return (
        key,
        parsed.get("value", ""),
        parsed.get("updated_at") or now_iso,
        parsed.get("last_reinforced_at") or now_iso,
        parsed.get("ttl_days") if parsed.get("ttl_days") is not None else _DEFAULT_TTL_DAYS,
        parsed.get("expires_at") or now_iso,
    )


_FACT_UPSERT_SQL = (
    "INSERT INTO facts(key, value, updated_at, last_reinforced_at, ttl_days, expires_at) "
    "VALUES(?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET "
    "value = excluded.value, "
    "updated_at = excluded.updated_at, "
    "last_reinforced_at = excluded.last_reinforced_at, "
    "ttl_days = excluded.ttl_days, "
    "expires_at = excluded.expires_at"
)


def fact_set(key: str, parsed: dict, conn: sqlite3.Connection | None = None) -> None:
    _exec(conn, _FACT_UPSERT_SQL, _fact_params(key, parsed))


def fact_set_many(
    items: list[tuple[str, dict]], conn: sqlite3.Connection | None = None
) -> None:
    if not items:
        return
    params = [_fact_params(k, p) for k, p in items]
    if conn is not None:
        conn.executemany(_FACT_UPSERT_SQL, params)
        return
    with write_txn() as c:
        c.executemany(_FACT_UPSERT_SQL, params)


def fact_delete(key: str, conn: sqlite3.Connection | None = None) -> bool:
    sql = "DELETE FROM facts WHERE key = ?"
    if conn is not None:
        return conn.execute(sql, (key,)).rowcount > 0
    with write_txn() as c:
        return c.execute(sql, (key,)).rowcount > 0


def fact_count() -> int:
    with read_conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM facts").fetchone()[0])


# ---------------------------------------------------------------------------
# Volumes & Decay
# ---------------------------------------------------------------------------


def volume_get(entity_key: str) -> float | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT volume FROM volumes WHERE entity_key = ?", (entity_key,)
        ).fetchone()
    return float(row["volume"]) if row is not None else None


_VOLUME_UPSERT_SQL = (
    "INSERT INTO volumes(entity_key, layer, volume) VALUES(?, ?, ?) "
    "ON CONFLICT(entity_key) DO UPDATE SET volume = excluded.volume"
)


def volume_set(
    entity_key: str,
    layer: str,
    volume: float,
    conn: sqlite3.Connection | None = None,
) -> None:
    _exec(conn, _VOLUME_UPSERT_SQL, (entity_key, layer, float(volume)))


def volume_set_many(
    rows: list[tuple[str, str, float]], conn: sqlite3.Connection | None = None
) -> None:
    if not rows:
        return
    params = [(ek, layer, float(vol)) for ek, layer, vol in rows]
    if conn is not None:
        conn.executemany(_VOLUME_UPSERT_SQL, params)
        return
    with write_txn() as c:
        c.executemany(_VOLUME_UPSERT_SQL, params)


def volume_delete(entity_key: str, conn: sqlite3.Connection | None = None) -> None:
    _exec(conn, "DELETE FROM volumes WHERE entity_key = ?", (entity_key,))
    _exec(conn, "DELETE FROM decay_anchor WHERE entity_key = ?", (entity_key,))


def volume_map(layer: str) -> dict[str, float]:
    with read_conn() as c:
        rows = c.execute(
            "SELECT entity_key, volume FROM volumes WHERE layer = ?", (layer,)
        ).fetchall()
    return {row["entity_key"]: float(row["volume"]) for row in rows}


def decay_anchor_map() -> dict[str, float]:
    with read_conn() as c:
        rows = c.execute("SELECT entity_key, age_hours FROM decay_anchor").fetchall()
    return {row["entity_key"]: float(row["age_hours"]) for row in rows}


def decay_anchor_set_many(
    rows: list[tuple[str, float]], conn: sqlite3.Connection | None = None
) -> None:
    if not rows:
        return
    sql = (
        "INSERT INTO decay_anchor(entity_key, age_hours) VALUES(?, ?) "
        "ON CONFLICT(entity_key) DO UPDATE SET age_hours = excluded.age_hours"
    )
    params = [(ek, float(age)) for ek, age in rows]
    if conn is not None:
        conn.executemany(sql, params)
        return
    with write_txn() as c:
        c.executemany(sql, params)


def decay_anchor_delete(entity_key: str, conn: sqlite3.Connection | None = None) -> None:
    _exec(conn, "DELETE FROM decay_anchor WHERE entity_key = ?", (entity_key,))


def volume_all_sorted() -> list[tuple[str, float]]:
    with read_conn() as c:
        rows = c.execute(
            "SELECT entity_key, volume FROM volumes "
            "ORDER BY volume ASC, entity_key ASC"
        ).fetchall()
    return [(row["entity_key"], float(row["volume"])) for row in rows]


# ---------------------------------------------------------------------------
# doc_reinforced & semantic fallbacks
# ---------------------------------------------------------------------------


def doc_reinforced_get(entity_key: str) -> str | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT last_reinforced_at FROM doc_reinforced WHERE entity_key = ?",
            (entity_key,),
        ).fetchone()
    return row["last_reinforced_at"] if row is not None else None


def doc_reinforced_set(
    entity_key: str, iso: str, conn: sqlite3.Connection | None = None
) -> None:
    _exec(
        conn,
        "INSERT INTO doc_reinforced(entity_key, last_reinforced_at) VALUES(?, ?) "
        "ON CONFLICT(entity_key) DO UPDATE SET "
        "last_reinforced_at = excluded.last_reinforced_at",
        (entity_key, iso),
    )


def doc_reinforced_delete(
    entity_key: str, conn: sqlite3.Connection | None = None
) -> None:
    _exec(conn, "DELETE FROM doc_reinforced WHERE entity_key = ?", (entity_key,))


def semantic_set(
    doc_id: str,
    text: str,
    metadata: dict,
    conn: sqlite3.Connection | None = None,
) -> None:
    _exec(
        conn,
        "INSERT INTO semantic_fallback(doc_id, text, metadata) VALUES(?, ?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET "
        "text = excluded.text, metadata = excluded.metadata",
        (doc_id, text, json.dumps(metadata, ensure_ascii=False)),
    )


def _loads_metadata(raw: str) -> dict:
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def semantic_get(doc_id: str) -> dict | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT text, metadata FROM semantic_fallback WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    if row is None:
        return None
    return {"text": row["text"], "metadata": _loads_metadata(row["metadata"])}


def semantic_iter() -> Iterator[tuple[str, str, dict]]:
    with read_conn() as c:
        rows = c.execute(
            "SELECT doc_id, text, metadata FROM semantic_fallback"
        ).fetchall()
    for row in rows:
        yield row["doc_id"], row["text"], _loads_metadata(row["metadata"])


def semantic_delete(doc_id: str, conn: sqlite3.Connection | None = None) -> None:
    _exec(conn, "DELETE FROM semantic_fallback WHERE doc_id = ?", (doc_id,))


def semantic_count() -> int:
    with read_conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM semantic_fallback").fetchone()[0])


def semantic_reinforced_get(doc_id: str) -> str | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT last_reinforced_at FROM semantic_reinforced WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    return row["last_reinforced_at"] if row is not None else None


def semantic_reinforced_set(
    doc_id: str, iso: str, conn: sqlite3.Connection | None = None
) -> None:
    _exec(
        conn,
        "INSERT INTO semantic_reinforced(doc_id, last_reinforced_at) VALUES(?, ?) "
        "ON CONFLICT(doc_id) DO UPDATE SET "
        "last_reinforced_at = excluded.last_reinforced_at",
        (doc_id, iso),
    )


def semantic_reinforced_delete(
    doc_id: str, conn: sqlite3.Connection | None = None
) -> None:
    _exec(conn, "DELETE FROM semantic_reinforced WHERE doc_id = ?", (doc_id,))


def semantic_reinforced_map() -> dict[str, str]:
    with read_conn() as c:
        rows = c.execute(
            "SELECT doc_id, last_reinforced_at FROM semantic_reinforced"
        ).fetchall()
    return {row["doc_id"]: row["last_reinforced_at"] for row in rows}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _trim_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)",
        (EVENTS_MAXLEN,),
    )


def event_log(
    entry_id: str,
    event_type: str,
    volume: float,
    layer: str,
    timestamp: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    sql = (
        "INSERT INTO events(entry_id, event_type, volume, layer, timestamp) "
        "VALUES(?, ?, ?, ?, ?)"
    )
    params = (entry_id, event_type, float(volume), layer, timestamp)
    if conn is not None:
        conn.execute(sql, params)
        if random.random() < _EVENTS_TRIM_PROBABILITY:
            _trim_events(conn)
        return
    with write_txn() as c:
        c.execute(sql, params)
        if random.random() < _EVENTS_TRIM_PROBABILITY:
            _trim_events(c)


# ---------------------------------------------------------------------------
# Causal Memories (v2.0)
# ---------------------------------------------------------------------------


def causal_insert(
    id: str,
    text: str,
    layer: int = 0,
    cause: str | None = None,
    effect: str | None = None,
    confidence: float = 0.5,
    source_ref: str | None = None,
    session_id: str | None = None,
    observed_at: str | None = None,
    parent_id: str | None = None,
    tags: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    obs_iso = observed_at or now_iso
    sql = """
    INSERT INTO causal_memories (
      id, text, layer, cause, effect, confidence, source_ref, session_id,
      observed_at, recorded_at, parent_id, tags
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
      confidence = excluded.confidence,
      tags = excluded.tags
    """
    params = (
        id, text, layer, cause, effect, confidence, source_ref,
        session_id, obs_iso, now_iso, parent_id, tags
    )
    _exec(conn, sql, params)


def causal_get(id: str) -> dict | None:
    with read_conn() as c:
        row = c.execute("SELECT * FROM causal_memories WHERE id = ?", (id,)).fetchone()
    return dict(row) if row is not None else None


def causal_query(
    layer: int | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    clauses = []
    params: list = []
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with read_conn() as c:
        rows = c.execute(
            f"SELECT * FROM causal_memories {where_sql} ORDER BY observed_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def causal_delete_l0_with_parent(l0_ttl_days: int = 7) -> int:
    """Lossy compaction: clean raw L0 memories whose parent L1 exists and is old."""
    with write_txn() as c:
        cur = c.execute(
            """
            DELETE FROM causal_memories
            WHERE layer = 0
              AND parent_id IS NOT NULL
              AND observed_at < datetime('now', '-' || ? || ' days')
            """,
            (l0_ttl_days,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Belief State (v2.0 Bi-Temporal & Supersession)
# ---------------------------------------------------------------------------


def _belief_id_taken(c: sqlite3.Connection, id: str) -> bool:
    return c.execute("SELECT 1 FROM belief_state WHERE id = ?", (id,)).fetchone() is not None


def _belief_version_id(c: sqlite3.Connection, base_id: str) -> str:
    """First free ``<base_id>#v<n>`` primary key.

    Callers legitimately re-use an id (it is documented as "identifier for this
    belief assertion"), but the row it named may already be parked in history as
    a superseded version, or may belong to a different subject entirely. Minting
    a distinct version id keeps every previous version intact instead of
    colliding on the primary key.
    """
    n = 2
    while _belief_id_taken(c, f"{base_id}#v{n}"):
        n += 1
    return f"{base_id}#v{n}"


def belief_assert(
    id: str,
    subject: str,
    predicate: str,
    object_val: str,
    confidence: float = 0.8,
    valid_from: str | None = None,
    evidence_id: str | None = None,
    source: str = "observer",
    conn: sqlite3.Connection | None = None,
) -> str:
    """Assert a new active belief, atomically superseding any incumbent.

    Returns the primary key the assertion actually landed on — usually ``id``,
    but a distinct ``<id>#v<n>`` when that key was already taken by an earlier
    version (see :func:`_belief_version_id`).

    Re-asserting the id that IS the current incumbent updates that row in place:
    superseding a row by itself would back-link ``superseded_by`` to its own id
    and then re-insert the very primary key it just wrote.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    v_from = valid_from or now_iso

    def _do(c: sqlite3.Connection) -> str:
        # Find existing active incumbent
        incumbent = c.execute(
            "SELECT id FROM belief_state WHERE subject = ? AND predicate = ? AND status = 'active'",
            (subject, predicate),
        ).fetchone()

        # Same row re-asserted: update in place, never supersede by itself.
        if incumbent is not None and incumbent["id"] == id:
            c.execute(
                "UPDATE belief_state SET object = ?, confidence = ?, valid_from = ?, "
                "valid_to = NULL, recorded_at = ?, status = 'active', "
                "superseded_by = NULL, evidence_id = ?, source = ? WHERE id = ?",
                (object_val, confidence, v_from, now_iso, evidence_id, source, id),
            )
            return id

        supersedes_id = incumbent["id"] if incumbent is not None else None

        # Resolve the new row's key BEFORE superseding, so the incumbent's
        # superseded_by back-link points at the row that actually gets written.
        new_id = _belief_version_id(c, id) if _belief_id_taken(c, id) else id

        if supersedes_id is not None:
            c.execute(
                "UPDATE belief_state SET status = 'superseded', valid_to = ?, superseded_by = ? WHERE id = ?",
                (v_from, new_id, supersedes_id),
            )

        c.execute(
            """
            INSERT INTO belief_state (
              id, subject, predicate, object, status, confidence,
              valid_from, valid_to, recorded_at, superseded_by, supersedes,
              evidence_id, source
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, ?, NULL, ?, ?, ?)
            """,
            (new_id, subject, predicate, object_val, confidence, v_from, now_iso, supersedes_id, evidence_id, source),
        )
        return new_id

    if conn is not None:
        return _do(conn)
    with write_txn() as c:
        return _do(c)


def belief_get_active(subject: str, predicate: str) -> dict | None:
    with read_conn() as c:
        row = c.execute(
            "SELECT * FROM belief_state WHERE subject = ? AND predicate = ? AND status = 'active'",
            (subject, predicate),
        ).fetchone()
    return dict(row) if row is not None else None


def belief_all_active(subject: str | None = None) -> list[dict]:
    with read_conn() as c:
        if subject:
            rows = c.execute(
                "SELECT * FROM belief_state WHERE subject = ? AND status = 'active' ORDER BY subject, predicate",
                (subject,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM belief_state WHERE status = 'active' ORDER BY subject, predicate"
            ).fetchall()
    return [dict(r) for r in rows]
