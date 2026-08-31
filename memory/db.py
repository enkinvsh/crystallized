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
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # numpy is imported lazily at runtime; see embedding_load
    import numpy as np

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".config" / "opencode" / "memory" / "memory.db"
DB_PATH: Path = Path(os.environ.get("OPENCODE_MEMORY_DB") or _DEFAULT_DB_PATH)

EVENTS_MAXLEN = 100000
_EVENTS_TRIM_PROBABILITY = 0.001
_DEFAULT_TTL_DAYS = 60

#: ``causal_memories.tags`` values marking a row as operational telemetry rather
#: than lived experience. Vocabulary of the table, not behaviour of any one
#: module: the hook writes these tags and consolidation must not mistake the
#: rows carrying them for something that was learned. One place to edit.
TELEMETRY_TAGS: frozenset[str] = frozenset({"session-summary", "tool-error"})


def telemetry_tag_predicate(*, match: bool) -> tuple[str, list[str]]:
    """Boolean SQL expression testing ``causal_memories.tags`` for telemetry.

    The ONLY place that knows ``tags`` is comma-joined and must therefore be
    compared in its DELIMITED form: a bare ``LIKE '%tool-error%'`` would also
    catch ``tool-error-recovered``, which is a real observation about a retry
    that worked. Consolidation excludes these rows and the reaper collects them,
    so the two must agree on the membership test exactly.

    ``match=True`` yields "carries at least one telemetry tag", ``match=False``
    its negation. Tag values are bound, never interpolated — they are data.
    """
    tags = sorted(TELEMETRY_TAGS)
    op, joiner = ("LIKE", " OR ") if match else ("NOT LIKE", " AND ")
    clause = joiner.join(f"',' || COALESCE(tags, '') || ',' {op} ?" for _ in tags)
    return f"({clause})", [f"%,{tag},%" for tag in tags]


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

_V4_REPAIR_SQL = r"""
-- Data repair, not schema: null the causal pairs this project's own observer
-- fabricated, and drop the beliefs Pass 3 minted out of them.
--
-- A tool-error row always said `tool_call:<tool> -> tool_error`; a session
-- summary always said `session_end -> session_summary`; a friction row always
-- said `user_message -> <type>`, restating the detector's own precondition.
-- None of the three was observed -- each is a branch of the hook restated as a
-- fact -- so an episode built from them agreed unanimously, `_coherent_pair`
-- passed the tautology up the ladder, and Pass 3 promoted it. Those sites now
-- write NULL, but Pass 3 reads rows and not source code: every store already
-- holding the backlog goes on minting the same beliefs forever.
--
-- The literals below are deliberately HARDCODED. Do not "DRY this up" against
-- TELEMETRY_TAGS or any other live constant: a migration is a historical
-- record of a repair applied at one point in time, and if the tag set gains a
-- member next year this statement must still describe exactly what it did.
--
-- Matched on the PAIR, at EVERY layer, and never on tags. `pass3_reconcile`
-- selects `layer >= 1`, so a synthesized parent that inherited the tautology
-- re-mints the belief after the belief row is deleted; and `_promote` gives an
-- L2 digest `tags = <dominant tag>`, so an inherited pair can sit on a row
-- whose tags have collapsed to just `observer`. Keying off tags would walk
-- straight past it -- which is also why nothing here needs the delimited tag
-- form, and why a `tool-error-recovered` row survives: it never carried one of
-- these pairs in the first place.
--
-- `_` is a LIKE wildcard, hence ESCAPE. Without it `tool\_call\_%` would also
-- match anything merely SHAPED like `toolXcallY`.

UPDATE causal_memories
   SET cause = NULL, effect = NULL
 WHERE cause LIKE 'tool\_call:%' ESCAPE '\'
   AND effect = 'tool_error';

UPDATE causal_memories
   SET cause = NULL, effect = NULL
 WHERE cause = 'session_end'
   AND effect = 'session_summary';

-- Friction keeps its effect: `hit['type']` is a real classification and is what
-- observer.py still writes. Only the invented cause goes.
UPDATE causal_memories
   SET cause = NULL
 WHERE cause = 'user_message';

-- Targeted on the triple, never on `source`: a dream-authored belief about
-- something real must survive. The subject spellings are what `belief_from` ->
-- `normalize_subject` actually produce -- ':' is outside [\w./-] and folds to
-- '_', so 'tool_call:Bash' becomes 'tool_call_bash'. The tool name varies, so
-- that subject is matched by prefix. For the friction shape the object is left
-- free: the subject 'user_message' can only have come from the one fabricating
-- site, whichever of the friction types happened to land in the object.
-- Superseded rows go too; supersession chains never cross a subject boundary,
-- so no surviving belief is left pointing at a deleted one.
DELETE FROM belief_state
 WHERE predicate = 'causes'
   AND ( (subject LIKE 'tool\_call\_%' ESCAPE '\' AND object = 'tool_error')
      OR (subject = 'session_end' AND object = 'session_summary')
      OR (subject = 'user_message') );
"""

_V5_SCHEMA_SQL = """
-- Vector store. ONE ROW PER CHUNK, never one per record: the encoder's context
-- is 128 tokens, about 363 characters of Russian, while the average fact here
-- is 2697 characters and the longest is 9575. One vector per row would embed
-- only each row's opening -- 4% of that longest fact -- and a lesson that
-- states its rule at the END would be permanently unreachable.
--
-- `vec` is float32 little-endian and ALREADY NORMALIZED, so cosine similarity
-- is a plain dot product and nothing has to renormalise at query time.
--
-- `model` is recorded per row so that a model swap is DETECTABLE. Vectors from
-- two different models share no space, and mixing them returns confident
-- nonsense rather than an error, so search filters on this column instead of
-- trusting the table to be homogeneous.
CREATE TABLE IF NOT EXISTS embeddings (
  kind       TEXT NOT NULL,   -- fact | semantic | causal | doc
  key        TEXT NOT NULL,
  chunk_ix   INTEGER NOT NULL,
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (kind, key, chunk_ix));
CREATE INDEX IF NOT EXISTS idx_embeddings_kind  ON embeddings(kind);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model);
"""

MIGRATIONS: dict[int, str] = {
    1: _V1_SCHEMA_SQL,
    2: _V2_SCHEMA_SQL,
    3: _V3_SCHEMA_SQL,
    4: _V4_REPAIR_SQL,
    5: _V5_SCHEMA_SQL,
}

# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

_db: sqlite3.Connection | None = None
_db_lock = threading.RLock()
_txn_depth = 0


def _py_lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _register_functions(conn: sqlite3.Connection) -> None:
    """Expose Python's Unicode-aware ``str.lower`` to SQL as ``py_lower``.

    SQLite's built-in ``lower()`` folds ASCII and nothing else, so
    ``lower(txt) LIKE lower('%гипотеза%')`` does not match a row storing
    "ГИПОТЕЗА" while the same expression matches "HYPOTHESIS" perfectly --
    silently, and only for the alphabet this store mostly speaks. Registered
    here rather than per query because every caller shares this connection.
    """
    conn.create_function("py_lower", 1, _py_lower, deterministic=True)


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
            _register_functions(c)
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


def _like_escape(value: str) -> str:
    """Neutralise LIKE metacharacters so a query is matched literally.

    Paired with ``ESCAPE '\\'`` at every call site: without it a query like
    ``test_runner`` silently also matches ``testXrunner``.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def causal_all(limit: int = 2000) -> list[dict]:
    """Every causal row worth searching, for callers that match in Python.

    The mirror of :func:`fact_all`, and for the same reason: ``recall`` matches
    on token overlap as well as substring, and that tokenizer lives in
    ``server`` -- importing it here would point the store at its own consumer.
    So the store hands over rows and the caller decides what a match is, which
    is the split the facts layer has always used.

    Telemetry is excluded HERE rather than by each caller, so the encoding of
    ``tags`` stays known to exactly one function no matter who reads the layer.

    Ordered by confidence so that if ``limit`` ever truncates, it truncates the
    machine residue rather than the hand-written lessons.
    """
    telemetry, tag_params = telemetry_tag_predicate(match=False)
    with read_conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM causal_memories
            WHERE {telemetry}
            ORDER BY confidence DESC, observed_at DESC
            LIMIT ?
            """,
            (*tag_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def causal_search(query: str, limit: int = 50) -> list[dict]:
    """Text search over the causal layer: ``text``, ``cause``, ``effect``, ``id``.

    ``causal_query`` filters by layer and session only, so until this existed
    nothing in the system could find a lesson by what it SAYS -- which is why
    ``recall`` never searched here and the layer was write-only in practice.

    ``id`` is searched deliberately. Hand-authored rows carry descriptive ids
    like ``2026-08-23-unmeasured-risk-stated-as-fact``; that is the same search
    surface a fact key is, and skipping it loses the most deliberate rows first.

    Telemetry is excluded through the shared predicate. Those rows record that
    the agent ran, not what it learned; they are trigger fuel and must never
    occupy a retrieval slot.

    Ordered by confidence, which is the honest ranking signal here: hand-written
    lessons sit at 0.9 and machine-folded rows at 0.27-0.31, so deliberate
    knowledge floats above automatic residue with no special case for either.
    """
    needle = f"%{_like_escape(query.lower())}%"
    telemetry, tag_params = telemetry_tag_predicate(match=False)
    with read_conn() as c:
        rows = c.execute(
            f"""
            SELECT * FROM causal_memories
            WHERE (   py_lower(text)                 LIKE ? ESCAPE '\\'
                   OR py_lower(COALESCE(cause, ''))  LIKE ? ESCAPE '\\'
                   OR py_lower(COALESCE(effect, '')) LIKE ? ESCAPE '\\'
                   OR py_lower(id)                   LIKE ? ESCAPE '\\')
              AND {telemetry}
            ORDER BY confidence DESC, observed_at DESC
            LIMIT ?
            """,
            (needle, needle, needle, needle, *tag_params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def causal_delete_l0_reapable(l0_ttl_days: int = 7, dry_run: bool = False) -> int:
    """Lossy compaction: drop aged L0 rows that are consumed, or unconsumable.

    The parent check is what makes this compaction rather than data loss: a raw
    record is only forgotten once its content survives inside an episode.

    Telemetry is exempt from that check because it can never satisfy it. Those
    rows are excluded from consolidation by design — they record that the agent
    ran, not what was learned — so nothing ever claims them and their
    ``parent_id`` stays NULL for good. Demanding a parent would therefore make
    them immortal, at roughly 90 rows a day. There is no content of theirs to
    survive; for them the TTL alone is the whole rule.

    ``dry_run`` counts against the SAME predicate instead of deleting, so the
    number reported and the number removed cannot drift apart.
    """
    telemetry, tag_params = telemetry_tag_predicate(match=True)
    where = f"""
        WHERE layer = 0
          AND (parent_id IS NOT NULL OR {telemetry})
          AND observed_at < datetime('now', '-' || ? || ' days')
    """
    params = (*tag_params, l0_ttl_days)
    if dry_run:
        with read_conn() as c:
            return int(
                c.execute(f"SELECT COUNT(*) FROM causal_memories {where}", params).fetchone()[0]
            )
    with write_txn() as c:
        return c.execute(f"DELETE FROM causal_memories {where}", params).rowcount


# ---------------------------------------------------------------------------
# Embeddings (vector store — one row per chunk)
# ---------------------------------------------------------------------------

EMBEDDING_KINDS = ("fact", "semantic", "causal", "doc")


def embedding_upsert(
    kind: str,
    key: str,
    chunks: list[bytes],
    model: str,
    dim: int,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Replace EVERY chunk of one key, atomically.

    Delete-then-insert rather than INSERT OR REPLACE: a re-embedded record can
    yield FEWER chunks than before (an edit that shortens it), and replacing
    row-by-row would leave the surplus tail behind as orphaned vectors that
    still answer searches. Wiping the key first is what makes the backfill
    re-runnable.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (kind, key, ix, model, dim, vec, now_iso)
        for ix, vec in enumerate(chunks)
    ]

    def _run(c: sqlite3.Connection) -> None:
        c.execute("DELETE FROM embeddings WHERE kind = ? AND key = ?", (kind, key))
        if rows:
            c.executemany(
                "INSERT INTO embeddings "
                "(kind, key, chunk_ix, model, dim, vec, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    if conn is not None:
        _run(conn)
    else:
        with write_txn() as c:
            _run(c)
    return len(rows)


def embedding_delete(
    kind: str, key: str, conn: sqlite3.Connection | None = None
) -> int:
    """Drop every chunk belonging to one key."""
    sql = "DELETE FROM embeddings WHERE kind = ? AND key = ?"
    if conn is not None:
        return conn.execute(sql, (kind, key)).rowcount
    with write_txn() as c:
        return c.execute(sql, (kind, key)).rowcount


def embedding_load(
    kind: str | None = None, model: str | None = None
) -> tuple[list[dict], np.ndarray]:
    """Chunk metadata plus the stacked, normalized matrix.

    Returns ``(rows, matrix)`` where ``matrix[i]`` is the vector of ``rows[i]``
    and every vector is already unit length, so a query is scored by one
    ``matrix @ q`` and nothing renormalises per search.

    ``numpy`` is imported HERE rather than at module scope on purpose. This
    module is imported by ``observer.py``, whose whole run must fit in
    ``BUDGET_MS`` (150 ms), and numpy costs ~27 ms to import — a fifth of that
    budget, spent on every tool call, for a table the hooks never read.
    """
    import numpy as np

    clauses: list[str] = []
    params: list[str] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if model is not None:
        clauses.append("model = ?")
        params.append(model)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with read_conn() as c:
        raw = c.execute(
            f"SELECT kind, key, chunk_ix, model, dim, vec FROM embeddings "
            f"{where} ORDER BY kind, key, chunk_ix",
            tuple(params),
        ).fetchall()

    rows = [dict(r) for r in raw]
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)

    dim = int(rows[0]["dim"])
    matrix = np.frombuffer(
        b"".join(r["vec"] for r in rows), dtype="<f4"
    ).reshape(len(rows), dim)
    return rows, matrix


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
    """Active beliefs, optionally scoped to a subject NAMESPACE.

    ``subject`` matches the exact key or any dotted child of it, so
    ``"dropweb"`` reaches ``dropweb.media_shutter`` the way the ``belief_list_active``
    tool has always claimed it did. Exact-match-only made every namespaced
    subject unreachable except by naming it in full, which defeats the point of
    a namespace. The separator is anchored so ``"drop"`` never captures
    ``"dropweb"``.
    """
    with read_conn() as c:
        if subject:
            rows = c.execute(
                "SELECT * FROM belief_state WHERE (subject = ? OR subject LIKE ? ESCAPE '\\') "
                "AND status = 'active' ORDER BY subject, predicate",
                (subject, f"{_like_escape(subject)}.%"),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM belief_state WHERE status = 'active' ORDER BY subject, predicate"
            ).fetchall()
    return [dict(r) for r in rows]
