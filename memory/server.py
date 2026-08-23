"""
MCP Memory Server for opencode.

Three-layer persistent memory:
1. Facts (SQLite) — fast key-value store for project facts, preferences, decisions
2. Semantic (ChromaDB) — vector search for finding relevant past conversations
3. Documents (filesystem) — structured markdown notes and architecture docs

Cross-layer tools:
- recall: unified search across all 3 layers in one call
- memory_context: compact metadata snapshot for context scenting

Internal Query Socket:
- Unix socket at /tmp/opencode-memory-query.sock
- Serves semantic search to memory-inject.py hook
- Piggybacks on warm encoder — zero cold start for hooks
"""

import contextlib
import difflib
import hashlib
import json
import math  # noqa: F401 — used in volume decay
import os
import re
import socket as _socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np
import chromadb
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

import db
import volume

_MEMORY_HOME = Path.home() / ".config" / "opencode" / "memory"
_DEFAULT_NOTES_DIR = _MEMORY_HOME / "notes"
_DEFAULT_CHROMA_DIR = _MEMORY_HOME / "chroma_db"
_DEFAULT_QUERY_SOCKET = Path("/tmp/opencode-memory-query.sock")
_DEFAULT_IDENTITY_PATH = _MEMORY_HOME / "identity.json"

NOTES_DIR = Path(os.environ.get("OPENCODE_MEMORY_NOTES_DIR") or _DEFAULT_NOTES_DIR)
CHROMA_DIR = Path(os.environ.get("OPENCODE_MEMORY_CHROMA_DIR") or _DEFAULT_CHROMA_DIR)
QUERY_SOCKET = Path(os.environ.get("OPENCODE_MEMORY_SOCKET") or _DEFAULT_QUERY_SOCKET)
IDENTITY_PATH = Path(
    os.environ.get("OPENCODE_MEMORY_IDENTITY") or _DEFAULT_IDENTITY_PATH
)

# ---------------------------------------------------------------------------
# Volume constants (power-law decay system)
# ---------------------------------------------------------------------------
#
# The decay model lives in volume.py so that server.py and the nightly
# consolidation daemon (dream.py) share ONE implementation. These names are
# re-exported for backwards compatibility with existing callers and tests.

MIN_VOLUME = volume.MIN_VOLUME
MAX_VOLUME = volume.MAX_VOLUME
DEFAULT_VOLUME = volume.DEFAULT_VOLUME
DECAY_ALPHA = volume.DECAY_ALPHA
DECAY_TAU = volume.DECAY_TAU

mcp = FastMCP("opencode-memory")

# ---------------------------------------------------------------------------
# Lazy singletons — initialized on first use
# ---------------------------------------------------------------------------

_chroma_collection: chromadb.Collection | None = None
_encoder: SentenceTransformer | None = None


def get_encoder() -> SentenceTransformer:
    """The one live SentenceTransformer in this process. Cache-first.

    ``local_files_only=True`` is tried FIRST because sentence-transformers
    otherwise revalidates every config file against huggingface.co even when
    the model is fully cached: measured 12.6 s against 0.8 s on this machine,
    and 57 s on a slow link. That turns a network stall into a memory stall.

    It falls back to a networked load rather than setting ``HF_HUB_OFFLINE``,
    which would be a hard failure on a machine that has never downloaded the
    model — and an env var would silently change behaviour for every other
    library in the process too.
    """
    global _encoder
    if _encoder is None:
        try:
            _encoder = SentenceTransformer(EMBED_MODEL_NAME, local_files_only=True)
        except Exception:
            _encoder = SentenceTransformer(EMBED_MODEL_NAME)
    return _encoder


def get_collection() -> chromadb.Collection:
    global _chroma_collection
    if _chroma_collection is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _chroma_collection = client.get_or_create_collection(
            "memories",
            metadata={"hnsw:space": "cosine"},
        )
    return _chroma_collection


# ---------------------------------------------------------------------------
# Safe semantic fallbacks
# ---------------------------------------------------------------------------
#
# In-process ChromaDB API has been observed to silently kill the Python
# process on this install (telemetry/sqlite VFS interaction — Python exits
# with no exception, so try/except cannot catch it). Treat the Chroma API
# as unsafe by default:
#   - Read existing memories via direct read-only sqlite3 from chroma.sqlite3
#   - Store new memories in the store's semantic_fallback table (db.semantic_*)
#   - Search via substring match on stored text
# Set OPENCODE_MEMORY_DISABLE_CHROMA_API=0 to re-enable the Chroma API path
# once the upstream crash is fixed.
#
# NOTE: chroma.sqlite3 below is a DIFFERENT database from the memory store in
# db.py — _sqlite_ro_conn/_sqlite_iter_memories/_sqlite_count_memories read
# ChromaDB's own file read-only and never touch the memory store.

CHROMA_SQLITE = CHROMA_DIR / "chroma.sqlite3"

#: Floor for vector hits in `recall`. Trims obvious garbage only. It is NOT a
#: relevance gate and must never be turned into one: measured on the live
#: store, an unrelated fact scored 0.584 against a query whose true causal
#: answer scored 0.462, so no single global cutoff separates signal from noise
#: ACROSS kinds. Per-kind top-N is what does the real work; this only stops the
#: tail of pure noise from being rendered. (Defined here rather than beside the
#: rest of the vector code because `recall` takes it as a default argument.)
VECTOR_MIN_SCORE = 0.35


def _chroma_api_disabled() -> bool:
    return os.getenv("OPENCODE_MEMORY_DISABLE_CHROMA_API", "1") != "0"


def _sqlite_ro_conn() -> sqlite3.Connection | None:
    if not CHROMA_SQLITE.exists():
        return None
    uri = f"file:{CHROMA_SQLITE}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _sqlite_count_memories() -> int:
    conn = _sqlite_ro_conn()
    if conn is None:
        return 0
    try:
        cur = conn.execute(
            "SELECT count(DISTINCT id) FROM embedding_metadata "
            "WHERE key = 'chroma:document'"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _sqlite_iter_memories():
    """Yield (doc_id, document_text, metadata_dict) from chroma.sqlite3.

    Metadata is reassembled from embedding_metadata rows. The document text
    lives in the chroma:document key.
    """
    conn = _sqlite_ro_conn()
    if conn is None:
        return
    by_id: dict[str, dict] = {}
    try:
        cur = conn.execute(
            "SELECT e.embedding_id, em.key, em.string_value, em.int_value, em.float_value "
            "FROM embeddings e JOIN embedding_metadata em ON e.id = em.id"
        )
        for embedding_id, key, sval, ival, fval in cur:
            entry = by_id.setdefault(embedding_id, {})
            if sval is not None:
                entry[key] = sval
            elif ival is not None:
                entry[key] = ival
            elif fval is not None:
                entry[key] = fval
    except sqlite3.Error:
        by_id = {}
    finally:
        conn.close()

    for doc_id, fields in by_id.items():
        doc = fields.pop("chroma:document", "")
        yield doc_id, doc, fields


def _semantic_remember_fallback(doc_id: str, text: str, metadata: dict) -> None:
    """Store a memory in the store's semantic_fallback table."""
    db.semantic_set(doc_id, text, metadata)

def _iter_semantic_fallback():
    """Yield (doc_id, text, metadata) for every semantic_fallback row.

    Rows are materialized up front so callers may write while iterating.
    """
    try:
        rows = list(db.semantic_iter())
    except Exception:
        return
    yield from rows


def _safe_count_semantic() -> int:
    sqlite_count = _sqlite_count_memories()
    try:
        fallback_count = db.semantic_count()
    except Exception:
        fallback_count = 0
    return sqlite_count + fallback_count


def _memory_sort_ts(meta: dict) -> float:
    """Sortable recency key for a semantic memory from its metadata.

    Prefers numeric `timestamp`; falls back to parsing the `date` ISO string.
    """
    if not meta:
        return 0.0
    ts = meta.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return float(ts)
        except ValueError:
            pass
    parsed = _parse_ts(meta.get("date"))
    return parsed.timestamp() if parsed else 0.0


def _safe_collect_recent_tags(limit: int = 20) -> set[str]:
    """Tags from the newest `limit` semantic memories (chroma.sqlite3 + fallback).

    Candidates from both sources are sorted by recency DESC before slicing, so
    the newest memories drive the tag set rather than arbitrary iteration order.
    """
    candidates: list[tuple[float, dict]] = []
    for _doc_id, _doc, meta in _sqlite_iter_memories():
        candidates.append((_memory_sort_ts(meta or {}), meta or {}))
    for _doc_id, _doc, meta in _iter_semantic_fallback():
        candidates.append((_memory_sort_ts(meta or {}), meta or {}))
    candidates.sort(key=lambda x: x[0], reverse=True)

    tags: set[str] = set()
    for _ts, meta in candidates[:limit]:
        tags_val = meta.get("tags")
        if isinstance(tags_val, str) and tags_val:
            for t in tags_val.split(","):
                t = t.strip()
                if t:
                    tags.add(t)
    return tags


_SAFE_STOPWORDS = {
    "и", "в", "не", "на", "я", "с", "что", "а", "по", "это", "к", "но",
    "он", "из", "за", "то", "все", "как", "или", "мы", "ты", "от", "бы",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "it", "to", "in", "and", "of", "for", "on", "at", "by", "that",
    "this", "these", "those", "with", "from", "as", "i", "you", "we",
    "do", "does", "did", "can", "could", "would", "should", "my", "me",
    "so", "no", "not", "or", "but", "if", "than", "then",
    "давай", "нужно", "хочу", "можно", "пожалуйста", "сделай", "покажи",
    "ладно", "ок", "да", "нет", "ну", "вот", "тут", "там", "еще", "уже",
}

_TOKEN_SPLIT_RE = re.compile(r"[\s\-_/.,!?()\[\]{}:;\"'`]+")


def _normalize_for_match(text: str) -> str:
    """Lowercase + collapse hyphens/underscores/punctuation to spaces.

    Makes `one-button` match `one button` and `provider-neutral` match
    `provider neutral` etc.
    """
    if not text:
        return ""
    return _TOKEN_SPLIT_RE.sub(" ", text.lower())


def _tokenize_query(query: str) -> list[str]:
    """Extract meaningful tokens from a natural-language query.

    Drops short tokens (<3 chars) and stopwords. Lowercased. Order is
    preserved but duplicates are removed.
    """
    normalized = _normalize_for_match(query)
    out: list[str] = []
    seen: set[str] = set()
    for tok in normalized.split():
        if len(tok) < 3 or tok in _SAFE_STOPWORDS or tok in seen:
            continue
        out.append(tok)
        seen.add(tok)
    return out


def _token_overlap_count(query_tokens: list[str], haystack_normalized: str) -> int:
    """Count distinct query tokens that appear as substrings in normalized text."""
    if not query_tokens or not haystack_normalized:
        return 0
    return sum(1 for t in query_tokens if t in haystack_normalized)


def _overlap_threshold(query_tokens: list[str]) -> int:
    """Minimum number of token matches required to count as a hit."""
    n = len(query_tokens)
    if n == 0:
        return 0
    if n <= 2:
        return n
    return max(2, (n + 2) // 3)


def _safe_substring_search(query: str, n_results: int = 5) -> list[tuple]:
    """Substring + token-overlap search across chroma.sqlite3 docs + fallback table.

    Scoring per candidate:
      - exact substring (whole query) → +1000 boost
      - + matched_token_count (overlap with meaningful query tokens)
      - + eff_volume / 1000 (tiebreak)
    Candidates below the overlap threshold are dropped unless the exact
    substring matched.
    """
    raw = query.strip()
    raw_lower = raw.lower()
    query_norm = _normalize_for_match(raw)
    tokens = _tokenize_query(raw)
    threshold = _overlap_threshold(tokens)

    candidates: list[tuple] = []
    for source in (_sqlite_iter_memories(), _iter_semantic_fallback()):
        for doc_id, doc, meta in source:
            doc_norm = _normalize_for_match(doc or "")
            substr_hit = bool(raw_lower) and (
                raw_lower in (doc or "").lower() or query_norm in doc_norm
            )
            overlap = _token_overlap_count(tokens, doc_norm) if doc_norm else 0
            if not substr_hit and (not tokens or overlap < threshold):
                continue
            eff_vol = _effective_volume(
                "semantic", doc_id, meta.get("last_reinforced_at") if meta else None
            )
            score = (1000.0 if substr_hit else 0.0) + float(overlap) + eff_vol / 1000.0
            candidates.append((score, doc, meta, eff_vol, doc_id))

    if not raw_lower:
        # Empty-query path: return loudest memories
        for source in (_sqlite_iter_memories(), _iter_semantic_fallback()):
            for doc_id, doc, meta in source:
                eff_vol = _effective_volume(
                    "semantic", doc_id,
                    meta.get("last_reinforced_at") if meta else None,
                )
                candidates.append((eff_vol, doc, meta, eff_vol, doc_id))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [(doc, meta, eff_vol, doc_id) for _s, doc, meta, eff_vol, doc_id in candidates[:n_results]]


# ---------------------------------------------------------------------------
# Volume helpers
# ---------------------------------------------------------------------------


# The decay / reinforcement model lives in volume.py so that this server and
# the nightly consolidation daemon (dream.py) can never drift apart. The
# underscored names below are thin aliases kept for the rest of this module.

_zset_key = volume.zset_key
_get_volume = volume.get_volume
_set_volume = volume.set_volume
_parse_ts = volume.parse_ts
_decayed = volume.decayed
_decay_volume = volume.decay_volume
_effective_volume = volume.effective_volume
_bulk_effective_volumes = volume.bulk_effective_volumes


def _reinforce(
    layer: str,
    entry_id: str,
    quality: float = 1.0,
    last_reinforced_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> float:
    """Reinforce volume on recall. Headroom-scaled diminishing returns.

    Thin wrapper over volume.reinforce that wires in this module's event log.
    """
    return volume.reinforce(
        layer,
        entry_id,
        quality=quality,
        last_reinforced_at=last_reinforced_at,
        conn=conn,
        log_event=_log_memory_event,
    )


# ---------------------------------------------------------------------------
# Rendering / budgeting helpers (pure)
# ---------------------------------------------------------------------------


def _preview(value: str, limit: int, key: str | None = None) -> str:
    """Single-line preview truncated to `limit` chars.

    When truncated and `key` is given, appends a hint naming get_fact("<key>").
    """
    single = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if len(single) <= limit:
        return single
    hidden = len(single) - limit
    hint = f', get_fact("{key}")' if key else ""
    return single[:limit] + f"… [+{hidden} chars{hint}]"


class _BudgetedLines:
    """Accumulate lines under a UTF-8 byte budget, reserving room for a footer.

    Ordering is preserved: once a candidate line would overflow (budget minus
    footer reserve), the builder stops and every further line is counted as
    omitted rather than reordered in.
    """

    def __init__(self, budget_bytes: int, footer_reserve: int = 200):
        self.budget = budget_bytes
        self.reserve = footer_reserve
        self.lines: list[str] = []
        self.used = 0
        self.omitted = 0
        self._stopped = False

    def add(self, line: str) -> bool:
        if self._stopped:
            self.omitted += 1
            return False
        size = len(line.encode("utf-8")) + 1  # + newline
        if self.used + size > self.budget - self.reserve:
            self._stopped = True
            self.omitted += 1
            return False
        self.lines.append(line)
        self.used += size
        return True

    def render(self, footer: str | None = None) -> str:
        body = "\n".join(self.lines)
        if self.omitted and footer:
            return (body + "\n" if body else "") + footer
        return body


def _clip_output(text: str, max_bytes: int = 45000) -> str:
    """UTF-8-safe last-resort guard.

    Clips `text` to at most `max_bytes` bytes WITHOUT splitting a multibyte
    character, appending a marker when clipping is applied.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "\n[output clipped]"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    clipped = encoded[:budget].decode("utf-8", errors="ignore")
    return clipped + marker


# ---------------------------------------------------------------------------
# Cyrillic word detection (cross-lingual embedding gap mitigation)
# ---------------------------------------------------------------------------

_SINGLE_CYRILLIC_RE = re.compile(r"^[а-яА-ЯёЁ\-]+$")


def _is_single_cyrillic_word(query: str) -> bool:
    """Detect queries that are a single Cyrillic word.

    Single rare Cyrillic words produce form-dominant embeddings in
    paraphrase-multilingual-MiniLM-L12-v2 (cosine to noise: 0.42-0.81)
    due to 10x training data imbalance (EN ~60% vs RU ~6% of internet).
    Two+ words cross the transition threshold (4.2x improvement) and work.

    See: journal/2026-03-27-embedding-geometry
    """
    words = query.strip().split()
    return len(words) == 1 and bool(_SINGLE_CYRILLIC_RE.match(words[0]))


def _substring_search_semantic(query: str, n_results: int = 5) -> list[tuple]:
    """Fallback semantic search: substring match on stored document text.

    Used when embedding-based search produces noise (single Cyrillic words).
    Returns list of (doc, meta, eff_vol, doc_id) tuples sorted by volume.
    """
    if _chroma_api_disabled():
        return _safe_substring_search(query, n_results)

    collection = get_collection()
    count = collection.count()
    if count == 0:
        return []

    query_lower = query.strip().lower()

    # Get all documents (~235 entries as of 2026-03-27 — fast enough)
    all_data = collection.get(include=["documents", "metadatas"])

    matches = []
    for i, doc_id in enumerate(all_data["ids"]):
        doc = all_data["documents"][i] if all_data["documents"] else ""
        meta = all_data["metadatas"][i] if all_data["metadatas"] else {}

        if query_lower in doc.lower():
            eff_vol = _effective_volume(
                "semantic", doc_id, meta.get("last_reinforced_at") if meta else None
            )
            matches.append((doc, meta, eff_vol, doc_id))

    # Sort by volume (highest first) — best proxy without semantic score
    matches.sort(key=lambda x: x[2], reverse=True)
    return matches[:n_results]


# ---------------------------------------------------------------------------
# Event logging (CLS retrofit hook)
# ---------------------------------------------------------------------------


def _log_memory_event(
    entry_id: str,
    event_type: str,
    volume: float,
    layer: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Log memory events to the store's event log for future CLS/analytics.

    Events: create, recall, reinforce, decay, update, delete_attempt

    When `conn` is supplied the insert joins the caller's transaction and
    failures PROPAGATE: a failed statement inside an open transaction would
    otherwise silently poison every later write in that transaction. Only the
    standalone (conn=None) path stays best-effort.
    """
    timestamp = datetime.now().isoformat()
    if conn is not None:
        db.event_log(entry_id, event_type, volume, layer, timestamp, conn=conn)
        return
    try:
        db.event_log(entry_id, event_type, volume, layer, timestamp)
    except Exception:
        pass  # non-critical, best-effort logging


# ===================================================================
# Layer 1 — Facts (SQLite)
# ===================================================================


PROJECT_NAMES: tuple[str, ...] = (
    "thready",
    "dropweb",
    "dropcars",
    "bedolaga",
    "remnawave",
    "remna_fleet",
    "ofm",
    "marisha",
    "keksx",
    "focus",
    "telemt",
    "silkwai",
    "zencab",
    "pena",
    "pixel5",
    "dcw",
    "flclash",
    "newsbot",
    "ranetka",
    "opencode",
    "zenbot",
    "nano",
    "iigor4ever",
    "nolan",
    "dating",
)


def _fact_group(key: str) -> str:
    normalized = key.casefold()
    matches = [p for p in PROJECT_NAMES if normalized == p or normalized.startswith(f"{p}_")]
    if matches:
        return max(matches, key=len)
    return normalized.partition("_")[0] or "other"


def _get_ttl_days(key: str) -> int:
    key_lower = key.lower()
    if any(p in key_lower for p in ("_ip", "_server", "_domain", "_host", "_url")):
        return 14
    if any(p in key_lower for p in ("_config", "_setting", "_env")):
        return 30
    if any(p in key_lower for p in ("_decision", "_plan", "_todo")):
        return 30
    if any(p in key_lower for p in ("_preference", "_style", "_name", "_identity")):
        return 90
    if any(p in key_lower for p in PROJECT_NAMES):
        return 180
    return 60


@mcp.tool()
def save_fact(key: str, value: str, ttl_days: int | None = None) -> str:
    """Save a quick fact (user name, project name, tech stack choice, key decision).
    Facts persist across sessions and are instantly retrievable by key.

    Args:
        key: Short descriptive key like "user_name", "db_choice", "project_lang"
        value: The fact value
        ttl_days: Optional TTL in days. Auto-detected from key pattern if not provided.
    """
    existing = db.fact_get(key)
    contradiction_msg = ""

    if existing:
        old_value = existing.get("value", "")
        if old_value and old_value != value:
            short_old = old_value[:50] + "..." if len(old_value) > 50 else old_value
            short_new = value[:50] + "..." if len(value) > 50 else value
            contradiction_msg = f"\n⚠️ OVERWRITE: '{short_old}' → '{short_new}'"

    effective_ttl = ttl_days if ttl_days is not None else _get_ttl_days(key)
    expires_at = (datetime.now() + timedelta(days=effective_ttl)).isoformat()

    data = {
        "value": value,
        "updated_at": datetime.now().isoformat(),
        "last_reinforced_at": datetime.now().isoformat(),
        "ttl_days": effective_ttl,
        "expires_at": expires_at,
    }
    # Fact row, volume seeding and event log are one logical unit.
    with db.write_txn() as txn:
        db.fact_set(key, data, conn=txn)
        existing_vol = db.volume_get(_zset_key("fact", key))
        if existing_vol is None:
            _set_volume("fact", key, DEFAULT_VOLUME["fact"], conn=txn)
            _log_memory_event(key, "create", DEFAULT_VOLUME["fact"], "fact", conn=txn)
        else:
            _log_memory_event(key, "update", existing_vol, "fact", conn=txn)
    _invalidate_fact_embeddings()
    return f"Saved fact: {key} = {value} (TTL: {effective_ttl}d){contradiction_msg}"


FACT_CHUNK_CHARS = 20000


def _fact_freshness(parsed: dict) -> str:
    exp_dt = _parse_ts(parsed.get("expires_at"))
    if exp_dt is None:
        return "no expiry"
    now = datetime.now()
    if now > exp_dt:
        return f"expired {(now - exp_dt).days}d ago"
    return f"fresh (expires in {(exp_dt - now).days}d)"


def _get_fact_header(key: str, parsed: dict, eff_vol: float, stored: float) -> str:
    return (
        f"Fact: {key}\n"
        f"Group: {_fact_group(key)}\n"
        f"Updated: {parsed.get('updated_at', '')}\n"
        f"Freshness: {_fact_freshness(parsed)}\n"
        f"Volume: {eff_vol:.1f} effective / {stored:.1f} stored\n"
        f"TTL: {parsed.get('ttl_days', '?')}d · "
        f"Last reinforced: {parsed.get('last_reinforced_at', '')}"
    )


@mcp.tool()
def list_facts(prefix: str = "", limit: int = 50, full: bool = False) -> str:
    """List saved facts filtered by a case-insensitive KEY PREFIX, newest first.

    Bounded output — does NOT dump everything and does NOT reinforce. Scope to a
    project with prefix (e.g. "dropweb_"); empty prefix lists all facts. Open a
    single fact's full value with get_fact("key").

    Args:
        prefix: Case-insensitive key prefix. Empty = all facts.
        limit: Max entries to show (clamped 1..200).
        full: Show full values instead of 200-char previews.
    """
    limit = max(1, min(200, limit))
    try:
        facts = _gather_facts()
    except Exception:
        return "No facts stored yet."
    if not facts:
        return "No facts stored yet."
    pfx = prefix.casefold()
    pool = [f for f in facts if f["key"].casefold().startswith(pfx)] if pfx else facts
    total = len(pool)
    if total == 0:
        return f'No facts matching prefix "{prefix}".'
    pool.sort(key=lambda f: f["updated_at"], reverse=True)
    selected = pool[:limit]

    label = f'Facts matching prefix "{prefix}"' if prefix else "All facts"
    header = f"{label} · showing {len(selected)} of {total} · newest first"

    builder = _BudgetedLines(40000)
    for f in selected:
        fresh = "expired" if f["expired"] else "fresh"
        date = f["updated_at"][:10] or "unknown"
        body = f["val"] if full else _preview(f["val"], 200, f["key"])
        if not builder.add(f"  {date} · vol {f['vol']:.1f} · {fresh}\n    {f['key']}: {body}"):
            break

    shown = len(builder.lines)
    omitted_count = total - shown
    out = header + "\n" + "\n".join(builder.lines)
    if omitted_count > 0:
        if shown < len(selected):
            next_key = selected[shown]["key"]
        elif len(pool) > limit:
            next_key = pool[limit]["key"]
        else:
            next_key = selected[-1]["key"] if selected else ""
        if full and shown < len(selected):
            out += f'\n…stopped at byte budget; continue with get_fact("{next_key}")'
        else:
            out += (
                f'\n…{omitted_count} more omitted. '
                f'Continue with list_facts(prefix="{prefix}") or get_fact("{next_key}")'
            )
    return _clip_output(out)


@mcp.tool()
def get_fact(key: str, offset: int = 0) -> str:
    """Fetch ONE fact by exact key with its FULL value (chunked when large).

    On the first chunk (offset==0) the fact is reinforced at quality 0.75;
    continuation chunks (offset>0) do NOT reinforce. Values longer than 20,000
    chars are returned in slices — follow the printed Continue hint.

    Args:
        key: Exact fact key.
        offset: Character offset into the value for continuation (default 0).
    """
    parsed = db.fact_get(key)
    if not parsed:
        try:
            all_keys = db.fact_keys()
        except Exception:
            all_keys = []
        kcf = key.casefold()
        ordered = difflib.get_close_matches(key, all_keys, n=5, cutoff=0.6)
        ordered += [k for k in all_keys if kcf in k.casefold()]
        suggestions: list[str] = []
        for k in ordered:
            if k not in suggestions:
                suggestions.append(k)
            if len(suggestions) >= 5:
                break
        msg = f"Fact not found: {key}"
        if suggestions:
            msg += "\nDid you mean:\n" + "\n".join(f"  {s}" for s in suggestions)
        return msg

    value = str(parsed.get("value", ""))
    total = len(value)

    if offset < 0 or offset >= max(total, 1):
        if not (offset == 0 and total == 0):
            return (
                f"Invalid offset {offset} for fact {key} "
                f"(value length {total}). Use 0 <= offset < {total}."
            )

    last_reinf = parsed.get("last_reinforced_at")
    eff_vol = _effective_volume("fact", key, last_reinf)
    stored = _get_volume("fact", key)

    if offset == 0:
        _reinforce("fact", key, quality=0.75, last_reinforced_at=last_reinf)
        parsed["last_reinforced_at"] = datetime.now().isoformat()
        try:
            db.fact_set(key, parsed)
        except Exception:
            pass

    end = min(offset + FACT_CHUNK_CHARS, total)
    chunk = value[offset:end]

    out_parts: list[str] = []
    if offset == 0:
        out_parts.append(_get_fact_header(key, parsed, eff_vol, stored))
    if total > FACT_CHUNK_CHARS:
        body = f"Value chunk: characters {offset}–{end} of {total}\n\n{chunk}"
        if end < total:
            body += f'\n\nContinue: get_fact("{key}", offset={end})'
        out_parts.append(body)
    else:
        out_parts.append(chunk)
    return _clip_output("\n\n".join(out_parts))


@mcp.tool()
def delete_fact(key: str) -> str:
    """Delete a fact that is no longer relevant.

    Args:
        key: The fact key to delete
    """
    removed = db.fact_delete(key)
    if removed:
        db.volume_delete(_zset_key("fact", key))
        _invalidate_fact_embeddings()
        return f"Deleted fact: {key}"
    return f"No fact found for key: {key}"


# ===================================================================
# Layer 2 — Semantic Memory (ChromaDB + sentence-transformers)
# ===================================================================


def _auto_detect_tags(text: str) -> list[str]:
    """Detect project names in text for automatic tagging. Closes the 21-52% tag gap."""
    text_lower = text.lower()
    return [p for p in PROJECT_NAMES if p in text_lower]


def _merge_tags(explicit: str, auto: list[str]) -> str:
    explicit_list = [t.strip() for t in explicit.split(",") if t.strip()]
    explicit_lower = {t.lower() for t in explicit_list}
    for t in auto:
        if t not in explicit_lower:
            explicit_list.append(t)
    return ",".join(explicit_list)


@mcp.tool()
def remember(text: str, tags: str = "") -> str:
    """Store a piece of information for later semantic search.
    Use this for conversation summaries, decisions with reasoning,
    architectural notes, debugging insights — anything worth remembering.

    Args:
        text: The text to remember (be descriptive — this is what gets searched)
        tags: Comma-separated tags for filtering, e.g. "architecture,database"
    """
    doc_id = hashlib.md5(text.encode()).hexdigest()[:16]
    final_tags = _merge_tags(tags, _auto_detect_tags(text))
    metadata = {
        "timestamp": time.time(),
        "date": datetime.now().isoformat(),
        "tags": final_tags,
        "last_reinforced_at": datetime.now().isoformat(),
    }

    if _chroma_api_disabled():
        _semantic_remember_fallback(doc_id, text, metadata)
        backend = "sqlite-fallback"
    else:
        encoder = get_encoder()
        embedding = encoder.encode(text).tolist()
        get_collection().upsert(
            ids=[doc_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[metadata],
        )
        backend = "chroma"

    # Volume seeding and its event log are one logical unit.
    with db.write_txn() as txn:
        _set_volume("semantic", doc_id, DEFAULT_VOLUME["semantic"], conn=txn)
        _log_memory_event(
            doc_id, "create", DEFAULT_VOLUME["semantic"], "semantic", conn=txn
        )
    return f"Remembered (id={doc_id}, {backend}): {text[:80]}..."


@mcp.tool()
def search_memory(query: str, n_results: int = 5) -> str:
    """Search past memories by meaning (semantic search).
    Use when the user references something from the past, or you need
    to recall a decision, discussion, or context from earlier sessions.

    Args:
        query: Natural language query describing what you're looking for
        n_results: How many results to return (default 5)
    """
    if _chroma_api_disabled():
        if _safe_count_semantic() == 0:
            return "No memories stored yet."
        matches = _safe_substring_search(query, n_results)
        if not matches:
            return "No relevant memories found."
        lines = []
        for doc, meta, eff_vol, doc_id in matches:
            date = meta.get("date", "unknown") if meta else "unknown"
            tags = meta.get("tags", "") if meta else ""
            tag_str = f" [{tags}]" if tags else ""
            lines.append(
                f"  [substr] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
            )
        return _clip_output(f"Found {len(lines)} memories:\n" + "\n".join(lines))

    collection = get_collection()

    count = collection.count()
    if count == 0:
        return "No memories stored yet."

    if _is_single_cyrillic_word(query):
        matches = _substring_search_semantic(query, n_results)
        if not matches:
            return "No relevant memories found."
        lines = []
        for doc, meta, eff_vol, doc_id in matches:
            date = meta.get("date", "unknown") if meta else "unknown"
            tags = meta.get("tags", "") if meta else ""
            tag_str = f" [{tags}]" if tags else ""
            lines.append(
                f"  [substr] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
            )
        return _clip_output(f"Found {len(lines)} memories:\n" + "\n".join(lines))

    encoder = get_encoder()
    query_embedding = encoder.encode(query).tolist()

    actual_n = min(n_results, count)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=actual_n,
        include=["documents", "metadatas", "distances"],
    )

    if not results["documents"] or not results["documents"][0]:
        return "No relevant memories found."

    docs = results["documents"][0]
    metas = results["metadatas"][0] if results["metadatas"] else []
    dists = results["distances"][0] if results["distances"] else []

    scored_results = []
    for i, doc in enumerate(docs):
        dist = dists[i] if i < len(dists) else 0.0
        meta = metas[i] if i < len(metas) else {}

        semantic_sim = 1 - dist
        if semantic_sim < 0.35:
            continue
        doc_id = results["ids"][0][i]

        eff_vol = _effective_volume(
            "semantic", doc_id, meta.get("last_reinforced_at") if meta else None
        )
        norm_volume = eff_vol / MAX_VOLUME

        age_hours = 0.0
        timestamp = meta.get("timestamp", 0) if meta else 0
        if timestamp:
            age_hours = (time.time() - timestamp) / 3600.0
        recency = (1 + age_hours / 24.0) ** (-0.3)

        composite = 0.50 * semantic_sim + 0.30 * norm_volume + 0.20 * recency
        scored_results.append((doc, meta, composite, semantic_sim, eff_vol, doc_id))

    scored_results.sort(key=lambda x: x[2], reverse=True)

    lines = []
    for doc, meta, composite, semantic_sim, eff_vol, doc_id in scored_results:
        date = meta.get("date", "unknown") if meta else "unknown"
        tags = meta.get("tags", "") if meta else ""
        tag_str = f" [{tags}]" if tags else ""
        lines.append(
            f"  [{composite:.2f}] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
        )

    return _clip_output(f"Found {len(lines)} memories:\n" + "\n".join(lines))


# ===================================================================
# Layer 3 — Documents (filesystem markdown)
# ===================================================================


@mcp.tool()
def save_doc(folder: str, name: str, content: str) -> str:
    """Save a structured document (architecture notes, checklists, meeting notes).
    Documents are markdown files organized in folders.

    Args:
        folder: Category folder, e.g. "architecture", "decisions", "context"
        name: Document name (without .md extension)
        content: Markdown content of the document
    """
    path = NOTES_DIR / folder
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / f"{name}.md"
    file_path.write_text(content, encoding="utf-8")
    # Volume, reinforcement stamp and event log are one logical unit.
    with db.write_txn() as txn:
        _set_volume("doc", f"{folder}/{name}", DEFAULT_VOLUME["doc"], conn=txn)
        db.doc_reinforced_set(
            f"doc:{folder}/{name}", datetime.now().isoformat(), conn=txn
        )
        _log_memory_event(
            f"{folder}/{name}", "create", DEFAULT_VOLUME["doc"], "doc", conn=txn
        )
    return f"Saved document: {folder}/{name}.md ({len(content)} chars)"


@mcp.tool()
def read_doc(folder: str, name: str, offset: int = 0) -> str:
    """Read a previously saved document, chunked for large files.

    Values longer than 20,000 chars are returned in slices; follow the printed
    Continue hint. offset==0 (default) preserves backward-compatible behavior.

    Args:
        folder: Category folder
        name: Document name (without .md extension)
        offset: Character offset into the document for continuation (default 0).
    """
    file_path = NOTES_DIR / folder / f"{name}.md"
    if not file_path.exists():
        return f"Document not found: {folder}/{name}.md"
    text = file_path.read_text(encoding="utf-8")
    total = len(text)
    if offset < 0 or (offset >= total and total > 0):
        return f"Invalid offset {offset} for {folder}/{name} (length {total})."
    if total <= FACT_CHUNK_CHARS:
        return text
    end = min(offset + FACT_CHUNK_CHARS, total)
    body = f"[chars {offset}–{end} of {total}]\n\n{text[offset:end]}"
    if end < total:
        body += f'\n\n… Continue: read_doc("{folder}", "{name}", offset={end})'
    return body


@mcp.tool()
def list_docs(folder: str = "") -> str:
    """List all saved documents, optionally filtered by folder.

    Args:
        folder: Optional folder to list. If empty, lists all folders and their docs.
    """
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    if folder:
        path = NOTES_DIR / folder
        if not path.exists():
            return f"Folder not found: {folder}"
        docs = [f.stem for f in path.glob("*.md")]
        if not docs:
            return f"No documents in {folder}/"
        return f"Documents in {folder}/:\n" + "\n".join(f"  {d}" for d in sorted(docs))

    lines = []
    for d in sorted(NOTES_DIR.iterdir()):
        if d.is_dir():
            docs = sorted(f.stem for f in d.glob("*.md"))
            if docs:
                lines.append(f"  {d.name}/")
                for doc in docs:
                    lines.append(f"    {doc}")
    if not lines:
        return "No documents stored yet."
    return _clip_output("All documents:\n" + "\n".join(lines))


@mcp.tool()
def delete_doc(folder: str, name: str) -> str:
    """Delete a document that is no longer needed.

    Args:
        folder: Category folder
        name: Document name (without .md extension)
    """
    file_path = NOTES_DIR / folder / f"{name}.md"
    if file_path.exists():
        file_path.unlink()
        # NOTE: behavior preservation — the matching doc_reinforced row is
        # deliberately NOT removed here. That pre-existing leak is the source
        # of the known doc_reinforced-vs-doc-volume orphan count and must not
        # be "fixed" as part of the store migration.
        db.volume_delete(_zset_key("doc", f"{folder}/{name}"))
        return f"Deleted: {folder}/{name}.md"
    return f"Document not found: {folder}/{name}.md"


# ===================================================================
# Cross-layer — Unified Search & Context Scenting
# ===================================================================


@mcp.tool()
def recall(
    query: str, n_results: int = 5, min_score: float = VECTOR_MIN_SCORE
) -> str:
    """Search every memory layer at once. Call this FIRST to find anything
    from past sessions. It searches:
    1. Facts (SQLite) — substring match on keys and values
    2. Semantic memories (ChromaDB) — meaning-based vector search
    3. Documents (filesystem) — filename and content substring match
    4. Causal memories (SQLite) — lessons and episodes, matched on text,
       cause, effect and id; loudest confidence first
    5. Beliefs (SQLite) — currently ACTIVE beliefs only; superseded ones
       are history and are not returned

    Operational telemetry (session summaries, tool errors) is deliberately
    excluded from layer 4: it records that the agent ran, not what it learned.

    Args:
        query: What you're looking for (natural language or keyword)
        n_results: Max semantic results to return (default 5)
    """
    n_results = max(1, min(10, n_results))
    sections = []
    # Keys already rendered by the literal matchers, so the vector top-up can
    # skip them rather than printing a record twice under two regimes.
    shown: dict[str, set[str]] = {k: set() for k in db.EMBEDDING_KINDS}
    query_lower = query.lower()
    query_cf = query.casefold()

    try:
        all_facts: dict[str, dict] = db.fact_all()
        tokens = _tokenize_query(query)
        threshold = _overlap_threshold(tokens)
        now = datetime.now()

        candidates: list[dict] = []
        keys_for_vol: list[str] = []
        lr_for_vol: list[str | None] = []
        for k, parsed in all_facts.items():
            val = parsed.get("value", "")
            k_cf = k.casefold()
            key_sub = query_cf in k_cf
            val_sub = query_cf in val.casefold()
            haystack = _normalize_for_match(f"{k} {val}")
            overlap = _token_overlap_count(tokens, haystack) if tokens else 0
            if not (key_sub or val_sub or (tokens and overlap >= threshold)):
                continue
            exp_dt = _parse_ts(parsed.get("expires_at"))
            candidates.append(
                {
                    "key": k,
                    "val": val,
                    "exact": k_cf == query_cf,
                    "key_sub": key_sub,
                    "val_sub": val_sub,
                    "overlap": overlap,
                    "expired": bool(exp_dt and now > exp_dt),
                    "updated_at": parsed.get("updated_at", ""),
                    "last_reinf": parsed.get("last_reinforced_at"),
                }
            )
            keys_for_vol.append(k)
            lr_for_vol.append(parsed.get("last_reinforced_at"))

        if candidates:
            vols = _bulk_effective_volumes("fact", keys_for_vol, lr_for_vol)
            for c, vol in zip(candidates, vols):
                c["vol"] = vol
            candidates.sort(
                key=lambda c: (
                    1 if c["exact"] else 0,
                    1 if c["key_sub"] else 0,
                    1 if c["val_sub"] else 0,
                    c["overlap"],
                    0 if c["expired"] else 1,
                    c["vol"],
                    c["updated_at"],
                    c["key"],
                ),
                reverse=True,
            )
            selected = candidates[:n_results]
            lines_facts = [
                f"  {c['key']}: {_preview(c['val'], 400, c['key'])} (vol: {c['vol']:.1f})"
                for c in selected
            ]
            # ONE transaction for the whole reinforcement pass: without it this
            # loop issues ~3 writes per selected fact (volume + event + row).
            with db.write_txn() as txn:
                for c in selected:
                    _reinforce(
                        "fact",
                        c["key"],
                        quality=0.5,
                        last_reinforced_at=c["last_reinf"],
                        conn=txn,
                    )
                    # db.fact_all() handed out ONE dict per key and the
                    # candidate rows above still reference it. Re-fetch a
                    # FRESH dict before mutating so the candidate entry (and
                    # the rendered lines) cannot be corrupted by this write.
                    fresh = db.fact_get(c["key"])
                    if fresh is None:
                        continue
                    fresh["last_reinforced_at"] = datetime.now().isoformat()
                    db.fact_set(c["key"], fresh, conn=txn)
            shown["fact"].update(c["key"] for c in selected)
            sections.append("Facts:\n" + "\n".join(lines_facts))
    except Exception:
        pass

    try:
        if _chroma_api_disabled():
            matches = _safe_substring_search(query, n_results)
            mem_lines = []
            for doc, meta, eff_vol, doc_id in matches:
                lr = meta.get("last_reinforced_at") if meta else None
                _reinforce("semantic", doc_id, quality=0.5, last_reinforced_at=lr)
                date = meta.get("date", "unknown") if meta else "unknown"
                tags = meta.get("tags", "") if meta else ""
                tag_str = f" [{tags}]" if tags else ""
                shown["semantic"].add(doc_id)
                mem_lines.append(
                    f"  [substr] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
                )
            if mem_lines:
                sections.append("Semantic memories:\n" + "\n".join(mem_lines))
        else:
            collection = get_collection()
            count = collection.count()
            if count > 0:
                if _is_single_cyrillic_word(query):
                    # Substring fallback: embedding model produces form-dominant
                    # vectors for single Cyrillic words (noise at 0.42-0.81).
                    matches = _substring_search_semantic(query, n_results)
                    mem_lines = []
                    for doc, meta, eff_vol, doc_id in matches:
                        lr = meta.get("last_reinforced_at") if meta else None
                        _reinforce("semantic", doc_id, quality=0.5, last_reinforced_at=lr)
                        meta["last_reinforced_at"] = datetime.now().isoformat()
                        try:
                            collection.update(ids=[doc_id], metadatas=[meta])
                        except Exception:
                            pass
                        date = meta.get("date", "unknown") if meta else "unknown"
                        tags = meta.get("tags", "") if meta else ""
                        tag_str = f" [{tags}]" if tags else ""
                        shown["semantic"].add(doc_id)
                        mem_lines.append(
                            f"  [substr] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
                        )
                    if mem_lines:
                        sections.append("Semantic memories:\n" + "\n".join(mem_lines))
                else:
                    encoder = get_encoder()
                    query_embedding = encoder.encode(query).tolist()
                    actual_n = min(n_results, count)
                    results = collection.query(
                        query_embeddings=[query_embedding],
                        n_results=actual_n,
                        include=["documents", "metadatas", "distances"],
                    )
                    if results["documents"] and results["documents"][0]:
                        docs = results["documents"][0]
                        metas = results["metadatas"][0] if results["metadatas"] else []
                        dists = results["distances"][0] if results["distances"] else []
                        mem_lines = []
                        for i, doc in enumerate(docs):
                            dist = dists[i] if i < len(dists) else 0.0
                            meta = metas[i] if i < len(metas) else {}
                            score = 1 - dist
                            if score < 0.35:
                                continue

                            doc_id = results["ids"][0][i]
                            lr_prev = meta.get("last_reinforced_at") if meta else None
                            eff_vol = _effective_volume("semantic", doc_id, lr_prev)
                            _reinforce(
                                "semantic", doc_id, quality=0.5, last_reinforced_at=lr_prev
                            )
                            meta["last_reinforced_at"] = datetime.now().isoformat()
                            try:
                                collection.update(ids=[doc_id], metadatas=[meta])
                            except Exception:
                                pass

                            date = meta.get("date", "unknown") if meta else "unknown"
                            tags = meta.get("tags", "") if meta else ""
                            tag_str = f" [{tags}]" if tags else ""
                            shown["semantic"].add(doc_id)
                            mem_lines.append(
                                f"  [{score:.2f}] id={doc_id} ({date}){tag_str} (vol: {eff_vol:.1f}) {_preview(doc, 600)}"
                            )
                        if mem_lines:
                            sections.append("Semantic memories:\n" + "\n".join(mem_lines))
    except Exception:
        pass

    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        doc_matches = []
        for md_file in sorted(NOTES_DIR.rglob("*.md")):
            if len(doc_matches) >= n_results:
                break
            rel_path = md_file.relative_to(NOTES_DIR)
            name_match = (
                query_lower in md_file.stem.lower()
                or query_lower in str(rel_path).lower()
            )
            content = md_file.read_text(encoding="utf-8")
            content_match = query_lower in content.lower()
            if name_match or content_match:
                preview = content[:200].replace("\n", " ").strip()
                rel_display = str(rel_path.with_suffix(""))
                shown["doc"].add(rel_display)
                doc_matches.append(f"  {rel_display}: {preview}...")
        if doc_matches:
            sections.append("Documents:\n" + "\n".join(doc_matches))
    except Exception:
        pass

    try:
        # Same rule as the facts block: contiguous substring OR enough token
        # overlap. Substring alone missed «ГИПОТЕЗА НЕ ИЗМЕРЕНО риск» against a
        # row storing «ГИПОТЕЗА, НЕ ИЗМЕРЕНО:» — every token present, contiguity
        # broken by one comma and one colon. Matching happens here, in ONE pass
        # over rows the store already filtered, so there is a single ranking
        # regime and nothing to deduplicate.
        c_tokens = _tokenize_query(query)
        c_threshold = _overlap_threshold(c_tokens)
        causal_cands: list[dict] = []
        for r in db.causal_all():
            blob = f"{r['id']} {r['text']} {r['cause'] or ''} {r['effect'] or ''}"
            sub = query_cf in blob.casefold()
            overlap = (
                _token_overlap_count(c_tokens, _normalize_for_match(blob))
                if c_tokens
                else 0
            )
            if not (sub or (c_tokens and overlap >= c_threshold)):
                continue
            r["_exact"] = r["id"].casefold() == query_cf
            r["_sub"] = sub
            r["_overlap"] = overlap
            causal_cands.append(r)

        # Confidence leads: a 0.90 hand-written lesson must outrank a 0.27
        # machine-folded row however many tokens the residue happens to share.
        causal_cands.sort(
            key=lambda r: (
                r["confidence"],
                1 if r["_exact"] else 0,
                1 if r["_sub"] else 0,
                r["_overlap"],
                r["observed_at"],
                r["id"],
            ),
            reverse=True,
        )
        shown["causal"].update(r["id"] for r in causal_cands[:n_results])
        causal_lines = [
            f"  [L{r['layer']} {r['confidence']:.2f}] {r['id']}: "
            f"{_preview(r['text'], 400)}"
            for r in causal_cands[:n_results]
        ]
        if causal_lines:
            sections.append("Causal memories:\n" + "\n".join(causal_lines))
    except Exception:
        pass

    try:
        belief_lines = []
        for b in db.belief_all_active():
            haystack = f"{b['subject']} {b['predicate']} {b['object']}"
            if query_cf not in haystack.casefold():
                continue
            belief_lines.append(
                f"  {b['subject']} -[{b['predicate']}]-> {b['object']} "
                f"(conf: {b['confidence']:.2f}, src: {b['source']})"
            )
            if len(belief_lines) >= n_results:
                break
        if belief_lines:
            sections.append("Beliefs:\n" + "\n".join(belief_lines))
    except Exception:
        pass

    # Vector top-up: ONE encode, then one search per kind. Per-kind rather
    # than global because chunk counts differ by an order of magnitude between
    # layers (30.2 chunks per document against 1.9 per causal row on the live
    # store), so a global max-over-chunks would let verbose prose outscore a
    # precise lesson on a query the lesson answers exactly. `Beliefs:` is left
    # out on purpose: a subject/predicate/object triple is a slot lookup.
    # Encode only if there is something to compare against. An empty embeddings
    # table cannot produce a hit, and the forward pass IS the cost of a search
    # — on a cold process it also drags in a ~57 s model load — so a store with
    # no vectors must not pay for a feature it is not using.
    try:
        has_vectors = bool(_get_vector_cache()["rows"])
    except Exception:
        has_vectors = False

    if has_vectors:
        try:
            query_vector = embed_query(query)
            for title, kind in (
                ("Facts", "fact"),
                ("Semantic memories", "semantic"),
                ("Documents", "doc"),
                ("Causal memories", "causal"),
            ):
                _vector_topup(
                    sections, title, kind, query_vector,
                    shown[kind], n_results, min_score,
                )
        except Exception:
            pass

    if not sections:
        return f"Nothing found across all memory layers for: {query}"

    return _clip_output(
        f'Recall results for "{query}":\n\n' + "\n\n".join(sections)
    )


def _gather_facts() -> list[dict]:
    """Read all facts once, WITHOUT mutation or reinforcement.

    Returns dicts with key/value/group/updated_at/expired/vol (effective).
    """
    now = datetime.now()
    all_facts: dict[str, dict] = db.fact_all()
    tmp: list[dict] = []
    keys: list[str] = []
    lrs: list[str | None] = []
    for k, parsed in all_facts.items():
        updated_at = parsed.get("updated_at", "")
        lr = parsed.get("last_reinforced_at")
        exp_dt = _parse_ts(parsed.get("expires_at"))
        tmp.append(
            {
                "key": k,
                "val": str(parsed.get("value", "")),
                "group": _fact_group(k),
                "updated_at": updated_at,
                "updated_dt": _parse_ts(updated_at),
                "expired": bool(exp_dt and now > exp_dt),
                "last_reinf": lr,
            }
        )
        keys.append(k)
        lrs.append(lr)
    vols = _bulk_effective_volumes("fact", keys, lrs)
    for f, vol in zip(tmp, vols):
        f["vol"] = vol
    return tmp


def _mmdd(f: dict) -> str:
    dt = f.get("updated_dt")
    return dt.strftime("%m-%d") if dt else "??-??"


def _diversify(items: list[dict], cap: int, total: int) -> list[dict]:
    out: list[dict] = []
    per: dict[str, int] = {}
    for it in items:
        if len(out) >= total:
            break
        g = it["group"]
        if per.get(g, 0) >= cap:
            continue
        per[g] = per.get(g, 0) + 1
        out.append(it)
    return out


def _semantic_snapshot() -> tuple[int, str, set[str]]:
    if _chroma_api_disabled():
        return _safe_count_semantic(), "safe-mode", _safe_collect_recent_tags(20)
    collection = get_collection()
    count = collection.count()
    tags: set[str] = set()
    if count > 0:
        recent = collection.peek(min(20, count))
        metadatas = recent.get("metadatas") or []
        for meta in metadatas:
            tags_val = meta.get("tags") if meta else None
            if isinstance(tags_val, str) and tags_val:
                for t in tags_val.split(","):
                    t = t.strip()
                    if t:
                        tags.add(t)
    return count, "chroma", tags


def _memory_context_zoom(project: str, facts: list[dict]) -> str:
    proj = project.casefold()
    matched = [f for f in facts if f["group"] == proj]
    if not matched:
        counts: dict[str, int] = {}
        for f in facts:
            counts[f["group"]] = counts.get(f["group"], 0) + 1
        ordered = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)[:15]
        lines = [f"  {name}  {cnt} facts" for name, cnt in ordered]
        return _clip_output(
            f'No facts in group "{project}". Largest available groups:\n'
            + "\n".join(lines)
        )
    matched.sort(key=lambda f: f["updated_at"], reverse=True)
    builder = _BudgetedLines(40000)
    for f in matched:
        fresh = "expired" if f["expired"] else "fresh"
        block = (
            f"  {_mmdd(f)} · vol {f['vol']:.1f} · {fresh}\n"
            f"    {f['key']}: {_preview(f['val'], 250, f['key'])}"
        )
        builder.add(block)
    footer = (
        f'…{builder.omitted} more. Use list_facts(prefix="{proj}_") '
        f'or get_fact("<key>")'
        if builder.omitted
        else None
    )
    header = f"{project} zoom · {len(matched)} facts · non-reinforcing\n"
    return _clip_output(header + builder.render(footer))


@mcp.tool()
def memory_context(project: str = "") -> str:
    """Compact, NON-REINFORCING snapshot of memory — for orientation.

    Call at session start. Reads only; never changes volumes or timestamps.
    Zero-arg gives a bounded overview (Projects/Recent/Salient/Freshness/
    Semantic/Documents). Pass project="<group>" to zoom into one
    project's facts. Drill deeper with get_fact("key"), list_facts(prefix=...),
    or recall("query").

    Args:
        project: Optional fact-group name to zoom into (e.g. "dropweb").
    """
    try:
        facts = _gather_facts()
    except Exception:
        return "Memory context:\n\n  (memory store unavailable)"

    if project:
        return _memory_context_zoom(project, facts)

    groups: dict[str, dict] = {}
    for f in facts:
        g = groups.setdefault(f["group"], {"count": 0, "latest": "", "expired": 0})
        g["count"] += 1
        if f["updated_at"] > g["latest"]:
            g["latest"] = f["updated_at"]
        if f["expired"]:
            g["expired"] += 1
    ordered_groups = sorted(
        groups.items(), key=lambda x: (x[1]["count"], x[0]), reverse=True
    )

    try:
        sem_count, sem_mode, sem_tags = _semantic_snapshot()
    except Exception:
        sem_count, sem_mode, sem_tags = 0, "unavailable", set()

    folders: list[tuple[str, list[str]]] = []
    total_docs = 0
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        dated: list[tuple[float, str, list[str]]] = []
        for folder in NOTES_DIR.iterdir():
            if folder.is_dir():
                md_files = list(folder.glob("*.md"))
                if md_files:
                    total_docs += len(md_files)
                    newest = max(f.stat().st_mtime for f in md_files)
                    dated.append((newest, folder.name, sorted(f.stem for f in md_files)))
        dated.sort(reverse=True)  # most recently touched folders first
        folders = [(name, docs) for _mt, name, docs in dated]
    except Exception:
        folders = []

    sections: list[str] = []

    sections.append(
        f"{len(facts)} facts · {sem_count} semantic · {total_docs} docs · "
        f"{sem_mode}\n"
        'Drill down: memory_context(project="dropweb") · get_fact("key") · '
        'list_facts(prefix="dropweb_") · recall("query")'
    )

    proj_lines = []
    for name, g in ordered_groups[:15]:
        line = f"  {name}  {g['count']} facts · latest {g['latest'][:10]}"
        if g["expired"]:
            line += f" · {g['expired']} expired"
        proj_lines.append(line)
    rest = ordered_groups[15:]
    if rest:
        rest_count = sum(g["count"] for _n, g in rest)
        proj_lines.append(f"  other  {rest_count} facts across {len(rest)} groups")
    sections.append("Projects:\n" + "\n".join(proj_lines))

    by_updated = sorted(facts, key=lambda f: f["updated_at"], reverse=True)
    recent = _diversify(by_updated, 2, 15)
    recent_keys = {f["key"] for f in recent}
    if recent:
        rlines = [
            f"  [{f['group']} · {_mmdd(f)}] {f['key']}: {_preview(f['val'], 140, f['key'])}"
            for f in recent
        ]
        sections.append("Recent:\n" + "\n".join(rlines))

    by_vol = sorted(
        (f for f in facts if f["key"] not in recent_keys),
        key=lambda f: f["vol"],
        reverse=True,
    )
    salient = _diversify(by_vol, 2, 15)
    if len(salient) < 15:
        chosen = {f["key"] for f in salient}
        for f in by_vol:
            if len(salient) >= 15:
                break
            if f["key"] in chosen:
                continue
            salient.append(f)
            chosen.add(f["key"])
    if salient:
        slines = [
            f"  [vol {f['vol']:.1f} · {f['group']}] {f['key']}: {_preview(f['val'], 140, f['key'])}"
            for f in salient
        ]
        sections.append("Salient:\n" + "\n".join(slines))

    expired_total = sum(1 for f in facts if f["expired"])
    exp_by_group = sorted(
        ((g["expired"], name) for name, g in groups.items() if g["expired"] > 0),
        reverse=True,
    )[:3]
    oldest = min((f["updated_at"] for f in facts if f["updated_at"]), default="")
    top_exp = " ".join(f"{name}({cnt})" for cnt, name in exp_by_group) or "none"
    sections.append(
        f"Freshness:\n  {expired_total} expired · top: {top_exp} · "
        f"oldest update {oldest[:10] or 'n/a'}"
    )

    tag_str = ", ".join(sorted(sem_tags)) if sem_tags else "none"
    sections.append(f"Semantic ({sem_count} · {sem_mode}):\n  recent tags: {tag_str}")

    if folders:
        dlines = []
        for name, docs in folders[:20]:
            teaser = docs[:2]
            names = ", ".join(teaser)
            remaining = len(docs) - len(teaser)
            if remaining > 0:
                names += f", +{remaining} more"
            dlines.append(f"  {name}/ ({len(docs)}): {names}")
        if len(folders) > 20:
            dlines.append(f"  +{len(folders) - 20} more folders")
        sections.append(f"Documents ({total_docs}):\n" + "\n".join(dlines))
    else:
        sections.append("Documents: (none)")

    return _clip_output("Memory context:\n\n" + "\n\n".join(sections), 12000)


# ===================================================================
# Volume tools — reinforce, sleep, export/import identity
# ===================================================================


@mcp.tool()
def reinforce(key: str, layer: str = "fact") -> str:
    """Explicitly boost a memory's volume — marks it as important to identity.
    Stronger than passive recall reinforcement (quality=1.0 vs 0.5).

    Args:
        key: For facts: the fact key. For semantic: the memory id. For docs: 'folder/name'.
        layer: Which layer: 'fact', 'semantic', or 'doc'
    """
    if layer not in ("fact", "semantic", "doc"):
        return f"Unknown layer: {layer}. Use 'fact', 'semantic', or 'doc'."

    old_vol = _get_volume(layer, key)

    prev_lr: str | None = None
    fact_parsed: dict | None = None
    sem_obj: dict | None = None
    sem_meta: dict | None = None
    if layer == "fact":
        fact_parsed = db.fact_get(key)
        if not fact_parsed:
            return f"Fact not found: {key}"
        prev_lr = fact_parsed.get("last_reinforced_at")
    elif layer == "semantic":
        if _chroma_api_disabled():
            try:
                sem_obj = db.semantic_get(key)
                if sem_obj:
                    sem_meta = sem_obj.get("metadata", {}) or {}
                    prev_lr = sem_meta.get("last_reinforced_at")
                else:
                    prev_lr = db.semantic_reinforced_get(key)
            except Exception:
                pass
        else:
            try:
                result = get_collection().get(ids=[key], include=["metadatas"])
                if result["ids"]:
                    sem_meta = result["metadatas"][0]
                    prev_lr = sem_meta.get("last_reinforced_at") if sem_meta else None
                else:
                    return f"Memory not found: {key}"
            except Exception:
                pass
    elif layer == "doc":
        try:
            prev_lr = db.doc_reinforced_get(f"doc:{key}")
        except Exception:
            prev_lr = None

    new_vol = _reinforce(layer, key, quality=1.0, last_reinforced_at=prev_lr)

    now_iso = datetime.now().isoformat()
    if layer == "fact" and fact_parsed is not None:
        fact_parsed["last_reinforced_at"] = now_iso
        db.fact_set(key, fact_parsed)
    elif layer == "semantic":
        if _chroma_api_disabled():
            if sem_obj is not None:
                try:
                    sem_meta = sem_obj.get("metadata", {}) or {}
                    sem_meta["last_reinforced_at"] = now_iso
                    sem_obj["metadata"] = sem_meta
                    db.semantic_set(key, sem_obj.get("text", ""), sem_meta)
                except Exception:
                    pass
            else:
                try:
                    db.semantic_reinforced_set(key, now_iso)
                except Exception:
                    pass
        elif sem_meta is not None:
            try:
                sem_meta["last_reinforced_at"] = now_iso
                get_collection().update(ids=[key], metadatas=[sem_meta])
            except Exception:
                pass
    elif layer == "doc":
        db.doc_reinforced_set(f"doc:{key}", now_iso)

    _log_memory_event(key, "reinforce", new_vol, layer)
    return f"Reinforced {layer}:{key} → volume {old_vol:.1f} → {new_vol:.1f}"


def _chroma_semantic_rows() -> list[tuple[str, str, dict]]:
    """Semantic rows straight from the ChromaDB API (metadata only)."""
    collection = get_collection()
    all_data = collection.get(include=["metadatas"])
    metadatas = all_data.get("metadatas") or []
    rows: list[tuple[str, str, dict]] = []
    for i, doc_id in enumerate(all_data["ids"]):
        meta = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
        rows.append((doc_id, "", meta))
    return rows


def _chroma_semantic_update(
    rows: list[tuple[str, str, dict]], conn: sqlite3.Connection
) -> None:
    """Best-effort metadata write-back to ChromaDB.

    Deliberately swallowed: the store holds the truth and ChromaDB read-repairs,
    so a cache write must never roll back the committed volume updates.
    """
    if not rows:
        return
    with contextlib.suppress(Exception):
        get_collection().update(
            ids=[r[0] for r in rows], metadatas=[r[2] for r in rows]
        )


@mcp.tool()
def sleep() -> str:
    """Run memory decay cycle. Applies power-law decay based on time since last reinforcement.

    Unlike a fixed multiplier, this computes ACTUAL decay from elapsed time:
    V_eff = V_stored * (1 + t_hours / τ)^(-α)
    Then stores the decayed value and resets the clock.

    Call periodically (once per session or via oh-my-loop).
    Memories are NEVER deleted, only made quieter. Floor: 0.01.

    The math itself lives in volume.py; this tool only supplies the semantic
    providers, which differ depending on whether the ChromaDB API is usable.
    """
    if _chroma_api_disabled():
        # chroma.sqlite3 is opened read-only, so those rows keep their clock in
        # the semantic_reinforced table; semantic_fallback rows own theirs.
        stats = volume.sleep(
            readonly_semantic=_sqlite_iter_memories,
            writable_semantic=_iter_semantic_fallback,
            log_event=_log_memory_event,
        )
    else:
        stats = volume.sleep(
            writable_semantic=_chroma_semantic_rows,
            writable_semantic_update=_chroma_semantic_update,
            log_event=_log_memory_event,
        )

    return volume.format_sleep_report(stats)


@mcp.tool()
def export_identity() -> str:
    """Export the volume map — this IS the personality fingerprint.
    Same data + different volumes = different person.
    Save this to preserve identity across system rebuilds.

    Exports: all volumes from the volume store + distribution statistics.
    """
    identity: dict = {
        "exported_at": datetime.now().isoformat(),
        "version": 2,
        "volumes": {},
        "stats": {},
    }

    # Export all volumes from the volumes table (source of truth).
    # volume_all_sorted() is ORDER BY volume ASC, entity_key ASC — identical to
    # the sorted-set range this used to read. identity["volumes"] is a plain
    # dict, so JSON key order == insertion order: keeping this ordering keeps
    # the exported file byte-identical to the pre-migration export, and diffable.
    try:
        all_entries = db.volume_all_sorted()
        for entry_key, score in all_entries:
            identity["volumes"][entry_key] = round(score, 4)

        # Distribution stats
        scores = [s for _, s in all_entries]
        if scores:
            scores.sort(reverse=True)
            identity["stats"] = {
                "total_entries": len(scores),
                "max_volume": scores[0],
                "min_volume": scores[-1],
                "mean_volume": sum(scores) / len(scores),
                "median_volume": scores[len(scores) // 2],
                "top_10_avg": sum(scores[:10]) / min(10, len(scores)),
                "bottom_10_avg": sum(scores[-10:]) / min(10, len(scores)),
            }
    except Exception as e:
        return f"Export failed: {e}"

    export_path = IDENTITY_PATH
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(json.dumps(identity, indent=2, ensure_ascii=False))

    total = len(identity["volumes"])
    return (
        f"Identity exported to {export_path} ({total} entries)\n"
        f"Stats: {json.dumps(identity['stats'], indent=2)}"
    )


@mcp.tool()
def import_identity(path: str = "") -> str:
    """Import a volume map — restores personality from a previous export.

    Args:
        path: Path to identity.json. Default: ~/.config/opencode/memory/identity.json
    """
    if not path:
        path = str(IDENTITY_PATH)

    import_path = Path(path)
    if not import_path.exists():
        return f"Identity file not found: {path}"

    identity = json.loads(import_path.read_text())

    if identity.get("version") != 2:
        return f"Unsupported identity version: {identity.get('version')}. Expected 2."

    volumes = identity.get("volumes", {})
    if not volumes:
        return "Identity file has no volumes."

    # Batch import in ONE transaction. The layer is the entity_key prefix up to
    # the FIRST colon only (maxsplit=1) — doc names may themselves contain ':'.
    rows = [
        (entry_key, entry_key.split(":", 1)[0], float(vol))
        for entry_key, vol in volumes.items()
    ]
    with db.write_txn() as txn:
        db.volume_set_many(rows, conn=txn)

    return (
        f"Identity imported from {path}. "
        f"Restored {len(volumes)} volume entries. "
        f"Stats: {json.dumps(identity.get('stats', {}), indent=2)}"
    )


# ===================================================================

# ===================================================================
# Layer 5 — Causal Memories & Belief State (v2.0)
# ===================================================================


@mcp.tool()
def belief_assert(
    id: str,
    subject: str,
    predicate: str,
    object_val: str,
    confidence: float = 0.8,
    valid_from: str = "",
    evidence_id: str = "",
    source: str = "observer",
) -> str:
    """Assert a new active belief, atomically superseding any incumbent active belief.

    Args:
        id: Unique identifier for this belief assertion
        subject: Target entity/domain (e.g., 'user.preferences', 'dropweb.testing')
        predicate: Property or constraint (e.g., 'test_runner', 'linter')
        object_val: Value or directive (e.g., 'vitest', 'biome')
        confidence: Confidence score 0.0 to 1.0 (default: 0.8)
        valid_from: ISO timestamp for validity start (default: current UTC time)
        evidence_id: Optional ID of causal_memory or transcript line as evidence
        source: Source of assertion ('observer', 'user', 'dream')
    """
    db.belief_assert(
        id=id,
        subject=subject,
        predicate=predicate,
        object_val=object_val,
        confidence=confidence,
        valid_from=valid_from or None,
        evidence_id=evidence_id or None,
        source=source,
    )
    return f"Belief asserted: {subject}.{predicate} = {object_val} (status=active, confidence={confidence})"


@mcp.tool()
def belief_get_active(subject: str, predicate: str) -> str:
    """Get currently active belief for a subject and predicate.

    Args:
        subject: Target entity (e.g. 'user.preferences')
        predicate: Property name (e.g. 'test_runner')
    """
    row = db.belief_get_active(subject, predicate)
    if not row:
        return f"No active belief found for {subject}.{predicate}"
    return json.dumps(row, indent=2, ensure_ascii=False)


@mcp.tool()
def belief_list_active(subject: str = "") -> str:
    """List all currently active beliefs, optionally filtered by subject.

    Args:
        subject: Optional subject prefix to filter (e.g. 'user', 'dropweb')
    """
    rows = db.belief_all_active(subject=subject or None)
    if not rows:
        return f"No active beliefs{' for ' + subject if subject else ''}."
    return json.dumps(rows, indent=2, ensure_ascii=False)


@mcp.tool()
def causal_log(
    id: str,
    text: str,
    layer: int = 0,
    cause: str = "",
    effect: str = "",
    confidence: float = 0.5,
    source_ref: str = "",
    session_id: str = "",
    parent_id: str = "",
    tags: str = "",
) -> str:
    """Log an episodic/causal memory transition.

    Args:
        id: Unique identifier
        text: Description of transition / lesson
        layer: 0=raw observation, 1=episode, 2=pattern, 3=principle
        cause: Trigger context / root cause
        effect: Action taken or outcome
        confidence: Confidence score 0.0 to 1.0
        source_ref: Transcript path or reference
        session_id: Session identifier
        parent_id: Parent memory ID (for hierarchical consolidation)
        tags: Comma-separated tags
    """
    db.causal_insert(
        id=id,
        text=text,
        layer=layer,
        cause=cause or None,
        effect=effect or None,
        confidence=confidence,
        source_ref=source_ref or None,
        session_id=session_id or None,
        parent_id=parent_id or None,
        tags=tags,
    )
    return f"Causal memory logged: [{id}] layer={layer} (confidence={confidence})"


@mcp.tool()
def causal_list(layer: int = -1, session_id: str = "", limit: int = 50) -> str:
    """List causal memories by layer and session.

    Args:
        layer: Layer filter: 0=raw, 1=episode, 2=pattern, 3=principle (-1 for all)
        session_id: Optional session ID filter
        limit: Max entries to return (default 50)
    """
    l_filter = None if layer < 0 else layer
    s_filter = session_id or None
    rows = db.causal_query(layer=l_filter, session_id=s_filter, limit=limit)
    if not rows:
        return "No causal memories found matching criteria."
    return json.dumps(rows, indent=2, ensure_ascii=False)


# ===================================================================
# Internal Query Socket (for memory-inject.py hook)
# ===================================================================
# QUERY_SOCKET is configured at the top of this module from
# OPENCODE_MEMORY_SOCKET with a /tmp fallback.

# ---------------------------------------------------------------------------
# Vector retrieval (Stage A) — chunked embeddings over the whole store
# ---------------------------------------------------------------------------

EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

#: Encoder context is 128 tokens. A window of 120 leaves room for the two
#: special tokens the tokenizer adds back when the chunk is encoded, so a chunk
#: never silently loses its tail to truncation.
CHUNK_TOKENS = 120
#: ~27% overlap. A rule split across a boundary has to survive INTACT in at
#: least one chunk; measured Russian lessons state the rule in one sentence of
#: roughly 20-30 tokens, so 32 covers it.
CHUNK_OVERLAP_TOKENS = 32

_vector_cache: dict | None = None
_vector_lock = threading.Lock()


class _OffsetTokenizer(Protocol):
    """The one HuggingFace fast-tokenizer call the chunker needs."""

    def __call__(
        self,
        text: str,
        add_special_tokens: bool = False,
        return_offsets_mapping: bool = True,
    ) -> dict: ...


def _chunk_spans(
    offsets: list[tuple[int, int]], window: int, stride: int
) -> list[tuple[int, int]]:
    """Sliding character spans over a token offset mapping. Pure."""
    n = len(offsets)
    if n == 0:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while start < n:
        end = min(start + window, n)
        spans.append((offsets[start][0], offsets[end - 1][1]))
        if end == n:
            break
        start += stride
    return spans


def _chunk_by_tokens(
    text: str, tokenizer: _OffsetTokenizer | None = None
) -> list[str]:
    """Split text into overlapping windows of at most ``CHUNK_TOKENS`` tokens.

    By TOKENS, not characters. Russian tokenizes to far more tokens per
    character than English, so a character window sized on English silently
    truncates Russian — the same shape of bug as SQLite's ASCII-only ``lower()``.
    Measured on this store: the encoder's 128-token context is about 363
    characters of Russian, the average fact is 2697 characters and the longest
    is 9575, so a single vector per record would embed 4% of the longest one.

    The tokenizer is injectable so the algorithm can be tested without loading
    the model; by default it is the warm encoder's own.
    """
    if not text:
        return []
    tok = tokenizer if tokenizer is not None else get_encoder().tokenizer
    encoded = tok(text, add_special_tokens=False, return_offsets_mapping=True)
    offsets = [tuple(o) for o in encoded["offset_mapping"]]
    stride = max(1, CHUNK_TOKENS - CHUNK_OVERLAP_TOKENS)
    return [text[a:b] for a, b in _chunk_spans(offsets, CHUNK_TOKENS, stride)]


def embed_chunks(text: str) -> list[bytes]:
    """Chunk, encode and normalize — the bytes ``db.embedding_upsert`` stores.

    Uses the warm singleton encoder. Nothing in this project may construct a
    second ``SentenceTransformer``: the hook budget assumes exactly one live
    model, inside this process.
    """
    chunks = _chunk_by_tokens(text)
    if not chunks:
        return []
    matrix = get_encoder().encode(
        chunks, show_progress_bar=False, normalize_embeddings=True
    )
    return [np.asarray(v, dtype="<f4").tobytes() for v in matrix]


def _invalidate_vector_cache() -> None:
    global _vector_cache
    with _vector_lock:
        _vector_cache = None


def _get_vector_cache() -> dict:
    global _vector_cache
    with _vector_lock:
        if _vector_cache is None:
            rows, matrix = db.embedding_load(model=EMBED_MODEL_NAME)
            _vector_cache = {"rows": rows, "matrix": matrix}
        return _vector_cache


def embed_query(query: str) -> np.ndarray:
    """Normalized query vector from the warm singleton encoder.

    Split out so one `recall` can encode ONCE and reuse the vector across every
    per-kind search: the forward pass is ~95% of a search's cost (12.6 ms of
    which 0.6 ms is the matmul, on a 30k-chunk matrix), so encoding per section
    would quadruple the price of the whole call for nothing.
    """
    q = np.asarray(get_encoder().encode(query), dtype=np.float32)
    norm = float(np.linalg.norm(q))
    return q / norm if norm > 0 else q


def vector_search(
    query: str | None = None,
    kind: str | None = None,
    n_results: int = 10,
    min_score: float = 0.0,
    query_vector: np.ndarray | None = None,
) -> list[tuple[str, str, float]]:
    """Nearest chunks to ``query``, collapsed to one hit per record.

    Returns ``(kind, key, score)`` sorted by descending cosine. Every stored
    vector is already unit length, so the whole search is one ``matrix @ q``;
    at this store's scale (~20k chunks, ~30 MB) an exact brute-force matmul
    beats any ANN index and needs no index to maintain.

    Scores are the MAX over a record's chunks, never the mean. A 9575-character
    fact whose single relevant chunk is the entire point would be averaged into
    the noise by its six irrelevant ones.

    Rows embedded by a different model are excluded at load time: two models
    share no vector space, and mixing them returns confident nonsense rather
    than an error.

    CACHE: the matrix is held in-process and rebuilt only when
    ``_invalidate_vector_cache()`` is called, which every writer of the
    ``embeddings`` table must do after committing. Nothing here notices a
    write made by another process.
    """
    cache = _get_vector_cache()
    rows, matrix = cache["rows"], cache["matrix"]
    if not rows:
        return []

    if query_vector is None:
        if query is None:
            raise ValueError("vector_search needs either query or query_vector")
        query_vector = embed_query(query)

    scores = matrix @ query_vector

    best: dict[tuple[str, str], float] = {}
    for row, score in zip(rows, scores, strict=True):
        if kind is not None and row["kind"] != kind:
            continue
        ident = (row["kind"], row["key"])
        value = float(score)
        if value > best.get(ident, -2.0):
            best[ident] = value

    hits = [
        (k, key, score)
        for (k, key), score in best.items()
        if score >= min_score
    ]
    hits.sort(key=lambda h: (-h[2], h[0], h[1]))
    return hits[:n_results]


def _semantic_document(doc_id: str) -> str | None:
    """Text of one semantic memory, from the fallback table or Chroma's file.

    The two stores are DISJOINT: Chroma holds what was written before safe-mode
    and `semantic_fallback` everything written since. Either can be the only
    home of a given id, so both are consulted. Chroma is read through read-only
    sqlite3, never its API.
    """
    row = db.semantic_get(doc_id)
    if row and row.get("text"):
        return row["text"]
    conn = _sqlite_ro_conn()
    if conn is None:
        return None
    try:
        cur = conn.execute(
            "SELECT string_value FROM embedding_metadata "
            "WHERE id = ? AND key = 'chroma:document'",
            (doc_id,),
        )
        hit = cur.fetchone()
        return hit[0] if hit else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _render_vector_line(kind: str, key: str, score: float) -> str | None:
    """One result line, marked so the retrieval regime that found it is visible.

    The existing `[substr]` prefix is what made this whole class of defect
    diagnosable; blending vector hits into the same undifferentiated list would
    throw that away.
    """
    tag = f"  [vec {score:.2f}]"
    if kind == "fact":
        row = db.fact_get(key)
        return f"{tag} {key}: {_preview(row.get('value', ''), 400, key)}" if row else None
    if kind == "causal":
        row = db.causal_get(key)
        if not row:
            return None
        return (
            f"{tag} [L{row['layer']} {row['confidence']:.2f}] {key}: "
            f"{_preview(row['text'], 400)}"
        )
    if kind == "doc":
        path = NOTES_DIR / f"{key}.md"
        if not path.exists():
            return None
        return f"{tag} {key}: {_preview(path.read_text(encoding='utf-8'), 200)}"
    if kind == "semantic":
        text = _semantic_document(key)
        return f"{tag} id={key} {_preview(text, 600)}" if text else None
    return None


def _extend_section(sections: list[str], title: str, extra: list[str]) -> None:
    """Append lines to an existing section, or start one if it is absent."""
    if not extra:
        return
    header = f"{title}:\n"
    for i, section in enumerate(sections):
        if section.startswith(header):
            sections[i] = section + "\n" + "\n".join(extra)
            return
    sections.append(header + "\n".join(extra))


def _vector_topup(
    sections: list[str],
    title: str,
    kind: str,
    query_vector: np.ndarray,
    shown: set[str],
    n_results: int,
    min_score: float,
) -> None:
    """Add vector hits for one kind, skipping whatever the section already shows.

    Appended AFTER the existing lines and never merged into their ordering, so
    an exact or substring hit cannot be pushed out by a vector one.
    """
    extra: list[str] = []
    for _kind, key, score in vector_search(
        kind=kind,
        n_results=n_results * 4,
        min_score=min_score,
        query_vector=query_vector,
    ):
        if key in shown:
            continue
        line = _render_vector_line(kind, key, score)
        if line is None:
            continue
        extra.append(line)
        shown.add(key)
        if len(extra) >= n_results:
            break
    _extend_section(sections, title, extra)


_fact_embed_cache: dict | None = None
_fact_embed_lock = threading.Lock()


def _build_fact_embeddings() -> dict | None:
    try:
        all_facts = db.fact_all()
        if not all_facts:
            return None

        keys, values, texts = [], [], []
        for key, parsed in all_facts.items():
            value = parsed.get("value", "")
            keys.append(key)
            values.append(value)
            texts.append(f"{key}: {value[:300]}")

        if not texts:
            return None

        encoder = get_encoder()
        matrix = encoder.encode(
            texts, show_progress_bar=False, normalize_embeddings=True
        )
        return {"keys": keys, "values": values, "matrix": matrix}
    except Exception:
        return None


def _get_fact_embeddings() -> dict | None:
    global _fact_embed_cache
    with _fact_embed_lock:
        if _fact_embed_cache is None:
            _fact_embed_cache = _build_fact_embeddings()
        return _fact_embed_cache


def _invalidate_fact_embeddings():
    global _fact_embed_cache
    with _fact_embed_lock:
        _fact_embed_cache = None


def _search_facts_semantic(query_embedding: np.ndarray, n: int = 10) -> list[dict]:
    cache = _get_fact_embeddings()
    if cache is None:
        return []

    matrix = cache["matrix"]
    similarities = matrix @ query_embedding

    top_indices = np.argsort(similarities)[::-1][:n]

    # Lookups are BY KEY, never positional: a batched IN-query returns rows in
    # rowid order and omits absent keys, which would misalign the results
    # against top_indices after the first gap.
    vol_map = db.volume_map("fact")

    results = []
    now = datetime.now()
    for idx in top_indices:
        sim = float(similarities[idx])
        if sim < 0.20:
            continue
        fact_key = cache["keys"][idx]
        stored = vol_map.get(f"fact:{fact_key}")
        vol = stored if stored is not None else 50.0
        updated_at = None
        expired = False
        parsed = db.fact_get(fact_key)
        if parsed:
            updated_at = parsed.get("updated_at")
            expires_at = parsed.get("expires_at")
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    expired = now > exp_dt
                except (ValueError, TypeError):
                    pass
        results.append(
            {
                "key": fact_key,
                "value": cache["values"][idx][:200],
                "score": round(sim, 3),
                "volume": round(float(vol), 1),
                "updated_at": updated_at,
                "expired": expired,
            }
        )
    return results


def _search_semantic_memories(
    query_embedding: np.ndarray, n: int = 5, query_str: str = ""
) -> list[dict]:
    if _chroma_api_disabled():
        memories: list[dict] = []
        for doc, meta, eff_vol, _doc_id in _safe_substring_search(query_str, n):
            memories.append(
                {
                    "text": (doc or "")[:200],
                    "score": 0.0,
                    "volume": round(float(eff_vol), 1),
                    "tags": meta.get("tags", "") if meta else "",
                }
            )
        return memories
    try:
        collection = get_collection()
        results = collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )

        if not results["documents"] or not results["documents"][0]:
            return []

        ids = results["ids"][0]
        # Lookup BY KEY, never positional — see _search_facts_semantic.
        vol_map = db.volume_map("semantic")

        memories = []
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            stored = vol_map.get(f"semantic:{ids[i]}")
            vol = stored if stored is not None else 40.0
            sim = 1.0 - dist
            if sim < 0.20:
                continue
            memories.append(
                {
                    "text": doc[:200],
                    "score": round(sim, 3),
                    "volume": round(float(vol), 1),
                    "tags": meta.get("tags", "") if meta else "",
                }
            )
        return memories
    except Exception:
        return []


def _handle_hook_query(conn: _socket.socket):
    try:
        data = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        if not data:
            return

        request = json.loads(data.decode().strip())
        query = request.get("query", "")
        n_facts = request.get("n_facts", 10)
        n_semantic = request.get("n_semantic", 5)

        if not query:
            conn.sendall(
                json.dumps({"facts": [], "semantic": [], "time_ms": 0}).encode() + b"\n"
            )
            return

        t0 = time.time()

        encoder = get_encoder()
        query_embedding = encoder.encode(
            query, show_progress_bar=False, normalize_embeddings=True
        )

        facts = _search_facts_semantic(query_embedding, n_facts)
        semantic = _search_semantic_memories(query_embedding, n_semantic, query)

        elapsed_ms = int((time.time() - t0) * 1000)

        response = json.dumps(
            {"facts": facts, "semantic": semantic, "time_ms": elapsed_ms}
        )
        conn.sendall(response.encode() + b"\n")
    except Exception as e:
        try:
            conn.sendall(
                json.dumps({"error": str(e), "facts": [], "semantic": []}).encode()
                + b"\n"
            )
        except Exception:
            pass
    finally:
        conn.close()


def _start_query_socket():
    try:
        if QUERY_SOCKET.exists():
            # Probe BEFORE unlinking: never steal a socket that is alive.
            test_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            test_sock.settimeout(0.2)
            try:
                test_sock.connect(str(QUERY_SOCKET))
                return  # another process is actively serving
            except (OSError, TimeoutError):
                QUERY_SOCKET.unlink(missing_ok=True)
            finally:
                test_sock.close()

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.bind(str(QUERY_SOCKET))
        sock.listen(5)

        def serve():
            while True:
                try:
                    conn, _ = sock.accept()
                    threading.Thread(
                        target=_handle_hook_query, args=(conn,), daemon=True
                    ).start()
                except Exception:
                    continue

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
    except Exception:
        pass


# ===================================================================
# Entrypoint
# ===================================================================

if __name__ == "__main__":
    _start_query_socket()
    mcp.run(transport="stdio")
