"""
MCP Memory Server for opencode.

Three-layer persistent memory:
1. Facts (Redis) — fast key-value store for project facts, preferences, decisions
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
import hashlib
import json
import math  # noqa: F401 — used in volume decay
import os
import re
import socket as _socket
import threading
import time
from datetime import datetime
from pathlib import Path

import chromadb
import numpy as np
import redis
from mcp.server.fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

NOTES_DIR = Path(
    os.environ.get(
        "OPENCODE_MEMORY_NOTES_DIR",
        str(Path.home() / ".config" / "opencode" / "memory" / "notes"),
    )
)
CHROMA_DIR = Path(
    os.environ.get(
        "OPENCODE_MEMORY_CHROMA_DIR",
        str(Path.home() / ".config" / "opencode" / "memory" / "chroma_db"),
    )
)
REDIS_PREFIX = "opencode:memory"

# ---------------------------------------------------------------------------
# Volume constants (power-law decay system)
# ---------------------------------------------------------------------------

MIN_VOLUME = 0.01
MAX_VOLUME = 100.0
DEFAULT_VOLUME = {
    "fact": 50.0,  # facts start mid-range, prove themselves via recall
    "semantic": 40.0,  # slightly lower — inferred, not stated
    "doc": 60.0,  # docs are intentionally saved, start louder
}

# Decay constants (power-law): V_eff = V_stored * (1 + t_hours / τ)^(-α)
DECAY_ALPHA = {"fact": 0.15, "semantic": 0.08, "doc": 0.03}
DECAY_TAU = {"fact": 72.0, "semantic": 168.0, "doc": 720.0}  # hours

VOLUME_INDEX_KEY = f"{REDIS_PREFIX}:volume_index"

mcp = FastMCP("opencode-memory")

# ---------------------------------------------------------------------------
# Lazy singletons — initialized on first use
# ---------------------------------------------------------------------------

_redis: redis.Redis | None = None
_chroma_collection: chromadb.Collection | None = None
_encoder: SentenceTransformer | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _redis = redis.Redis.from_url(url, decode_responses=True)
        else:
            host = os.environ.get("REDIS_HOST", "localhost")
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            _redis = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        _redis.ping()
    return _redis


def get_encoder() -> SentenceTransformer:
    global _encoder
    if _encoder is None:
        _encoder = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
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
# Volume helpers
# ---------------------------------------------------------------------------


def _zset_key(layer: str, entry_id: str) -> str:
    """Composite key for the volume ZSET: 'fact:user_name', 'semantic:abc123'."""
    return f"{layer}:{entry_id}"


def _get_volume(layer: str, entry_id: str) -> float:
    """Get raw stored volume from ZSET (source of truth)."""
    score = get_redis().zscore(VOLUME_INDEX_KEY, _zset_key(layer, entry_id))
    return score if score is not None else DEFAULT_VOLUME.get(layer, 50.0)  # type: ignore[return-value]


def _set_volume(layer: str, entry_id: str, volume: float) -> None:
    """Set volume in ZSET (source of truth)."""
    clamped = max(MIN_VOLUME, min(MAX_VOLUME, volume))
    get_redis().zadd(VOLUME_INDEX_KEY, {_zset_key(layer, entry_id): clamped})


def _decay_volume(stored: float, t_hours: float, layer: str) -> float:
    """Pure power-law decay: V_eff = V_stored * (1 + t_hours / τ)^(-α), floored at MIN_VOLUME.

    No Redis, no Chroma, no filesystem, no clock reads — testable in isolation.
    """
    if t_hours <= 0:
        return stored
    alpha = DECAY_ALPHA.get(layer, 0.1)
    tau = DECAY_TAU.get(layer, 168.0)
    decayed = stored * (1 + t_hours / tau) ** (-alpha)
    return max(MIN_VOLUME, decayed)


def _effective_volume(
    layer: str, entry_id: str, last_reinforced_at: str | None = None
) -> float:
    """Compute volume with power-law decay applied.

    Formula: V_eff = V_stored * (1 + t_hours / τ)^(-α)
    Decay is computed on read, never stored.
    """
    stored = _get_volume(layer, entry_id)

    if last_reinforced_at:
        try:
            last_dt = datetime.fromisoformat(last_reinforced_at)
            age_hours = (datetime.now() - last_dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            age_hours = 0.0
    else:
        age_hours = 0.0

    return _decay_volume(stored, age_hours, layer)


def _reinforce(layer: str, entry_id: str, quality: float = 1.0) -> float:
    """Reinforce volume on recall. Headroom-scaled diminishing returns.

    quality: 0.0 (appeared but unused) to 1.0 (directly used in response)
    Returns new volume.
    """
    current = _get_volume(layer, entry_id)
    headroom = MAX_VOLUME - current
    boost = 12.0 * quality * (headroom / MAX_VOLUME)
    new_vol = min(current + boost, MAX_VOLUME)
    _set_volume(layer, entry_id, new_vol)
    _log_memory_event(entry_id, "recall", new_vol, layer)
    return new_vol


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
    """
    words = query.strip().split()
    return len(words) == 1 and bool(_SINGLE_CYRILLIC_RE.match(words[0]))


def _substring_search_semantic(query: str, n_results: int = 5) -> list[tuple]:
    """Fallback semantic search: substring match on stored document text.

    Used when embedding-based search produces noise (single Cyrillic words).
    Returns list of (doc, meta, eff_vol, doc_id) tuples sorted by volume.
    """
    collection = get_collection()
    count = collection.count()
    if count == 0:
        return []

    query_lower = query.strip().lower()

    all_data = collection.get(include=["documents", "metadatas"])

    matches = []
    for i, doc_id in enumerate(all_data["ids"]):
        doc = all_data["documents"][i] if all_data["documents"] else ""
        meta = all_data["metadatas"][i] if all_data["metadatas"] else {}

        if query_lower in doc.lower():
            eff_vol = _effective_volume(
                "semantic",
                doc_id,
                meta.get("last_reinforced_at") if meta else None,  # type: ignore[arg-type]
            )
            matches.append((doc, meta, eff_vol, doc_id))

    matches.sort(key=lambda x: x[2], reverse=True)
    return matches[:n_results]


# ---------------------------------------------------------------------------
# Event logging (CLS retrofit hook)
# ---------------------------------------------------------------------------


def _log_memory_event(
    entry_id: str, event_type: str, volume: float, layer: str
) -> None:
    """Log memory events to Redis stream for future CLS/analytics.

    Events: create, recall, reinforce, decay, update, delete_attempt
    """
    with contextlib.suppress(Exception):
        # non-critical, best-effort logging
        get_redis().xadd(
            f"{REDIS_PREFIX}:events",
            {
                "entry_id": entry_id,
                "event_type": event_type,
                "volume": str(volume),
                "layer": layer,
                "timestamp": datetime.now().isoformat(),
            },
            maxlen=100000,  # keep last 100K events, auto-trim
        )


# ===================================================================
# Layer 1 — Facts (Redis)
# ===================================================================


@mcp.tool()
def save_fact(key: str, value: str) -> str:
    """Save a quick fact (user name, project name, tech stack choice, key decision).
    Facts persist across sessions and are instantly retrievable by key.

    Args:
        key: Short descriptive key like "user_name", "db_choice", "project_lang"
        value: The fact value
    """
    data = json.dumps(
        {
            "value": value,
            "updated_at": datetime.now().isoformat(),
            "last_reinforced_at": datetime.now().isoformat(),
        }
    )
    get_redis().hset(f"{REDIS_PREFIX}:facts", key, data)
    existing_vol = get_redis().zscore(VOLUME_INDEX_KEY, _zset_key("fact", key))
    if existing_vol is None:
        _set_volume("fact", key, DEFAULT_VOLUME["fact"])
        _log_memory_event(key, "create", DEFAULT_VOLUME["fact"], "fact")
    else:
        _log_memory_event(key, "update", existing_vol, "fact")  # type: ignore[arg-type]
    _invalidate_fact_embeddings()
    return f"Saved fact: {key} = {value}"


@mcp.tool()
def list_facts() -> str:
    """List all saved facts. Useful to quickly see everything the memory knows."""
    raw: dict[str, str] = get_redis().hgetall(f"{REDIS_PREFIX}:facts")  # type: ignore[assignment]
    if not raw:
        return "No facts stored yet."
    lines = []
    for k, v in sorted(raw.items()):
        parsed = json.loads(v)
        eff_vol = _effective_volume("fact", k, parsed.get("last_reinforced_at"))
        lines.append(f"  {k}: {parsed['value']} (vol: {eff_vol:.1f})")
    return "Known facts:\n" + "\n".join(lines)


@mcp.tool()
def delete_fact(key: str) -> str:
    """Delete a fact that is no longer relevant.

    Args:
        key: The fact key to delete
    """
    removed = get_redis().hdel(f"{REDIS_PREFIX}:facts", key)
    if removed:
        get_redis().zrem(VOLUME_INDEX_KEY, _zset_key("fact", key))
        _invalidate_fact_embeddings()
        return f"Deleted fact: {key}"
    return f"No fact found for key: {key}"


# ===================================================================
# Layer 2 — Semantic Memory (ChromaDB + sentence-transformers)
# ===================================================================


@mcp.tool()
def remember(text: str, tags: str = "") -> str:
    """Store a piece of information for later semantic search.
    Use this for conversation summaries, decisions with reasoning,
    architectural notes, debugging insights — anything worth remembering.

    Args:
        text: The text to remember (be descriptive — this is what gets searched)
        tags: Comma-separated tags for filtering, e.g. "architecture,database"
    """
    encoder = get_encoder()
    embedding = encoder.encode(text).tolist()
    doc_id = hashlib.md5(text.encode()).hexdigest()[:16]
    metadata = {
        "timestamp": time.time(),
        "date": datetime.now().isoformat(),
        "tags": tags,
        "last_reinforced_at": datetime.now().isoformat(),
    }
    get_collection().upsert(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata],
    )
    _set_volume("semantic", doc_id, DEFAULT_VOLUME["semantic"])
    _log_memory_event(doc_id, "create", DEFAULT_VOLUME["semantic"], "semantic")
    return f"Remembered (id={doc_id}): {text[:80]}..."


@mcp.tool()
def search_memory(query: str, n_results: int = 5) -> str:
    """Search past memories by meaning (semantic search).
    Use when the user references something from the past, or you need
    to recall a decision, discussion, or context from earlier sessions.

    Args:
        query: Natural language query describing what you're looking for
        n_results: How many results to return (default 5)
    """
    collection = get_collection()

    count = collection.count()
    if count == 0:
        return "No memories stored yet."

    if _is_single_cyrillic_word(query):
        matches = _substring_search_semantic(query, n_results)
        if not matches:
            return "No relevant memories found."
        lines = []
        for doc, meta, eff_vol, _doc_id in matches:
            date = meta.get("date", "unknown") if meta else "unknown"
            tags = meta.get("tags", "") if meta else ""
            tag_str = f" [{tags}]" if tags else ""
            lines.append(f"  [substr] ({date}){tag_str} (vol: {eff_vol:.1f}) {doc}")
        return f"Found {len(lines)} memories:\n" + "\n".join(lines)

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
            "semantic",
            doc_id,
            meta.get("last_reinforced_at") if meta else None,  # type: ignore[arg-type]
        )
        norm_volume = eff_vol / MAX_VOLUME

        age_hours = 0.0
        timestamp = meta.get("timestamp", 0) if meta else 0
        if timestamp:
            age_hours = (time.time() - timestamp) / 3600.0  # type: ignore[operator]
        recency = (1 + age_hours / 24.0) ** (-0.3)

        composite = 0.50 * semantic_sim + 0.30 * norm_volume + 0.20 * recency
        scored_results.append((doc, meta, composite, semantic_sim, eff_vol, doc_id))

    scored_results.sort(key=lambda x: x[2], reverse=True)

    lines = []
    for doc, meta, composite, _semantic_sim, eff_vol, _doc_id in scored_results:
        date = meta.get("date", "unknown") if meta else "unknown"
        tags = meta.get("tags", "") if meta else ""
        tag_str = f" [{tags}]" if tags else ""
        lines.append(
            f"  [{composite:.2f}] ({date}){tag_str} (vol: {eff_vol:.1f}) {doc}"
        )

    return f"Found {len(lines)} memories:\n" + "\n".join(lines)


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
    _set_volume("doc", f"{folder}/{name}", DEFAULT_VOLUME["doc"])
    get_redis().hset(
        f"{REDIS_PREFIX}:doc_reinforced",
        f"doc:{folder}/{name}",
        datetime.now().isoformat(),
    )
    _log_memory_event(f"{folder}/{name}", "create", DEFAULT_VOLUME["doc"], "doc")
    return f"Saved document: {folder}/{name}.md ({len(content)} chars)"


@mcp.tool()
def read_doc(folder: str, name: str) -> str:
    """Read a previously saved document.

    Args:
        folder: Category folder
        name: Document name (without .md extension)
    """
    file_path = NOTES_DIR / folder / f"{name}.md"
    if file_path.exists():
        return file_path.read_text(encoding="utf-8")
    return f"Document not found: {folder}/{name}.md"


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
    return "All documents:\n" + "\n".join(lines)


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
        get_redis().zrem(VOLUME_INDEX_KEY, _zset_key("doc", f"{folder}/{name}"))
        return f"Deleted: {folder}/{name}.md"
    return f"Document not found: {folder}/{name}.md"


# ===================================================================
# Cross-layer — Unified Search & Context Scenting
# ===================================================================


@mcp.tool()
def recall(query: str, n_results: int = 5) -> str:
    """Search ALL memory layers at once — facts, semantic memories, and documents.
    This is the recommended first tool to call when you need to find anything
    from past sessions. It searches:
    1. Facts (Redis) — substring match on keys and values
    2. Semantic memories (ChromaDB) — meaning-based vector search
    3. Documents (filesystem) — filename and content substring match

    Args:
        query: What you're looking for (natural language or keyword)
        n_results: Max semantic results to return (default 5)
    """
    sections = []
    query_lower = query.lower()

    try:
        raw_facts: dict[str, str] = get_redis().hgetall(f"{REDIS_PREFIX}:facts")  # type: ignore[assignment]
        matched_facts = []
        for k, v in sorted(raw_facts.items()):
            parsed = json.loads(v)
            val = parsed["value"]
            if query_lower in k.lower() or query_lower in val.lower():
                new_vol = _reinforce("fact", k, quality=0.5)
                parsed["last_reinforced_at"] = datetime.now().isoformat()
                get_redis().hset(f"{REDIS_PREFIX}:facts", k, json.dumps(parsed))
                matched_facts.append(f"  {k}: {val} (vol: {new_vol:.1f})")
        if matched_facts:
            sections.append("Facts:\n" + "\n".join(matched_facts))
    except Exception:
        pass

    try:
        collection = get_collection()
        count = collection.count()
        if count > 0:
            if _is_single_cyrillic_word(query):
                matches = _substring_search_semantic(query, n_results)
                mem_lines = []
                for doc, meta, eff_vol, doc_id in matches:
                    _reinforce("semantic", doc_id, quality=0.5)
                    meta["last_reinforced_at"] = datetime.now().isoformat()
                    with contextlib.suppress(Exception):
                        collection.update(ids=[doc_id], metadatas=[meta])
                    date = meta.get("date", "unknown") if meta else "unknown"
                    tags = meta.get("tags", "") if meta else ""
                    tag_str = f" [{tags}]" if tags else ""
                    mem_lines.append(
                        f"  [substr] ({date}){tag_str} (vol: {eff_vol:.1f}) {doc}"
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
                        new_vol = _reinforce("semantic", doc_id, quality=0.5)
                        meta["last_reinforced_at"] = datetime.now().isoformat()  # type: ignore[index]
                        with contextlib.suppress(Exception):
                            collection.update(ids=[doc_id], metadatas=[meta])

                        eff_vol = _effective_volume(
                            "semantic",
                            doc_id,
                            meta.get("last_reinforced_at"),  # type: ignore[arg-type]
                        )
                        date = meta.get("date", "unknown") if meta else "unknown"
                        tags = meta.get("tags", "") if meta else ""
                        tag_str = f" [{tags}]" if tags else ""
                        mem_lines.append(
                            f"  [{score:.2f}] ({date}){tag_str} (vol: {eff_vol:.1f}) {doc}"
                        )
                    if mem_lines:
                        sections.append("Semantic memories:\n" + "\n".join(mem_lines))
    except Exception:
        pass

    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        doc_matches = []
        for folder in sorted(NOTES_DIR.iterdir()):
            if not folder.is_dir():
                continue
            for md_file in sorted(folder.glob("*.md")):
                name_match = query_lower in md_file.stem.lower()
                content = md_file.read_text(encoding="utf-8")
                content_match = query_lower in content.lower()
                if name_match or content_match:
                    preview = content[:200].replace("\n", " ").strip()
                    doc_matches.append(f"  {folder.name}/{md_file.stem}: {preview}...")
        if doc_matches:
            sections.append("Documents:\n" + "\n".join(doc_matches))
    except Exception:
        pass

    if not sections:
        return f"Nothing found across all memory layers for: {query}"

    return f'Recall results for "{query}":\n\n' + "\n\n".join(sections)


@mcp.tool()
def memory_context() -> str:
    """Get a compact snapshot of everything in memory — for orientation.
    Call this at the start of a session to understand what the agent
    already knows. Returns fact keys, memory count with recent tags,
    and document folder/file listing. Content is NOT included (use
    recall, search_memory, or read_doc to get details).

    No arguments needed.
    """
    sections = []

    try:
        raw_facts: dict[str, str] = get_redis().hgetall(f"{REDIS_PREFIX}:facts")  # type: ignore[assignment]
        if raw_facts:
            fact_lines = []
            for k, v in sorted(raw_facts.items()):
                parsed = json.loads(v)
                fact_lines.append(f"  {k}: {parsed['value']}")
            sections.append(f"Facts ({len(raw_facts)}):\n" + "\n".join(fact_lines))
        else:
            sections.append("Facts: (none)")
    except Exception:
        sections.append("Facts: (Redis unavailable)")

    try:
        collection = get_collection()
        mem_count = collection.count()
        if mem_count > 0:
            recent = collection.peek(min(20, mem_count))
            all_tags: set[str] = set()
            metadatas = recent.get("metadatas")
            if metadatas is not None:
                for meta in metadatas:
                    if meta is not None:
                        tags_val = meta.get("tags")
                        if isinstance(tags_val, str) and tags_val:
                            for t in tags_val.split(","):
                                t = t.strip()
                                if t:
                                    all_tags.add(t)
            tag_str = ", ".join(sorted(all_tags)) if all_tags else "none"
            sections.append(f"Semantic memories: {mem_count} entries (tags: {tag_str})")
        else:
            sections.append("Semantic memories: (none)")
    except Exception:
        sections.append("Semantic memories: (ChromaDB unavailable)")

    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        doc_lines = []
        total_docs = 0
        for folder in sorted(NOTES_DIR.iterdir()):
            if folder.is_dir():
                docs = sorted(f.stem for f in folder.glob("*.md"))
                if docs:
                    total_docs += len(docs)
                    doc_lines.append(f"  {folder.name}/")
                    for doc in docs:
                        doc_lines.append(f"    {doc}")
        if doc_lines:
            sections.append(f"Documents ({total_docs}):\n" + "\n".join(doc_lines))
        else:
            sections.append("Documents: (none)")
    except Exception:
        sections.append("Documents: (filesystem error)")

    return "Memory context:\n\n" + "\n\n".join(sections)


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
    new_vol = _reinforce(layer, key, quality=1.0)

    if layer == "fact":
        raw = get_redis().hget(f"{REDIS_PREFIX}:facts", key)
        if raw:
            parsed = json.loads(raw)  # type: ignore[arg-type]
            parsed["last_reinforced_at"] = datetime.now().isoformat()
            get_redis().hset(f"{REDIS_PREFIX}:facts", key, json.dumps(parsed))
        else:
            return f"Fact not found: {key}"
    elif layer == "semantic":
        collection = get_collection()
        try:
            result = collection.get(ids=[key], include=["metadatas"])
            if result["ids"]:
                meta = result["metadatas"][0]  # type: ignore[index]
                meta["last_reinforced_at"] = datetime.now().isoformat()  # type: ignore[index]
                collection.update(ids=[key], metadatas=[meta])
            else:
                return f"Memory not found: {key}"
        except Exception:
            pass
    elif layer == "doc":
        get_redis().hset(
            f"{REDIS_PREFIX}:doc_reinforced", f"doc:{key}", datetime.now().isoformat()
        )

    _log_memory_event(key, "reinforce", new_vol, layer)
    return f"Reinforced {layer}:{key} → volume {old_vol:.1f} → {new_vol:.1f}"


@mcp.tool()
def sleep() -> str:
    """Run memory decay cycle. Applies power-law decay based on time since last reinforcement.

    Unlike a fixed multiplier, this computes ACTUAL decay from elapsed time:
    V_eff = V_stored * (1 + t_hours / τ)^(-α)
    Then stores the decayed value and resets the clock.

    Call periodically (once per session or via oh-my-loop).
    Memories are NEVER deleted, only made quieter. Floor: 0.01.
    """
    stats = {"fact": 0, "semantic": 0, "doc": 0, "total_decayed": 0}
    now = datetime.now()

    # Decay facts
    try:
        raw_facts = get_redis().hgetall(f"{REDIS_PREFIX}:facts")
        pipe = get_redis().pipeline()
        for k, v in raw_facts.items():  # type: ignore[union-attr]
            parsed = json.loads(v)
            last_reinforced = parsed.get(
                "last_reinforced_at", parsed.get("updated_at", "")
            )
            stored_vol = _get_volume("fact", k)
            eff_vol = _effective_volume("fact", k, last_reinforced)

            if eff_vol < stored_vol - 0.01:
                pipe.zadd(
                    VOLUME_INDEX_KEY, {_zset_key("fact", k): max(MIN_VOLUME, eff_vol)}
                )
                parsed["last_reinforced_at"] = now.isoformat()
                pipe.hset(f"{REDIS_PREFIX}:facts", k, json.dumps(parsed))
                stats["fact"] += 1
        pipe.execute()
    except Exception:
        pass

    # Decay semantic memories
    try:
        collection = get_collection()
        all_data = collection.get(include=["metadatas"])
        pipe = get_redis().pipeline()
        chroma_updates_ids = []
        chroma_updates_metas = []

        for i, doc_id in enumerate(all_data["ids"]):
            meta = all_data["metadatas"][i] if all_data["metadatas"] else {}
            last_reinforced = meta.get("last_reinforced_at", meta.get("date", ""))
            stored_vol = _get_volume("semantic", doc_id)
            eff_vol = _effective_volume("semantic", doc_id, last_reinforced)  # type: ignore[arg-type]

            if eff_vol < stored_vol - 0.01:
                pipe.zadd(
                    VOLUME_INDEX_KEY,
                    {_zset_key("semantic", doc_id): max(MIN_VOLUME, eff_vol)},
                )
                meta["last_reinforced_at"] = now.isoformat()  # type: ignore[index]
                chroma_updates_ids.append(doc_id)
                chroma_updates_metas.append(meta)
                stats["semantic"] += 1

        pipe.execute()
        if chroma_updates_ids:
            with contextlib.suppress(Exception):
                collection.update(
                    ids=chroma_updates_ids, metadatas=chroma_updates_metas
                )
    except Exception:
        pass

    # Decay docs
    try:
        all_doc_entries = get_redis().zrangebyscore(
            VOLUME_INDEX_KEY, "-inf", "+inf", withscores=True
        )
        pipe = get_redis().pipeline()
        for entry_key, score in all_doc_entries:  # type: ignore[union-attr]
            if entry_key.startswith("doc:"):
                last_r = get_redis().hget(f"{REDIS_PREFIX}:doc_reinforced", entry_key)
                eff_vol = _effective_volume("doc", entry_key[4:], last_r)  # type: ignore[arg-type]
                if eff_vol < score - 0.01:
                    pipe.zadd(VOLUME_INDEX_KEY, {entry_key: max(MIN_VOLUME, eff_vol)})
                    pipe.hset(
                        f"{REDIS_PREFIX}:doc_reinforced", entry_key, now.isoformat()
                    )
                    stats["doc"] += 1
        pipe.execute()
    except Exception:
        pass

    stats["total_decayed"] = stats["fact"] + stats["semantic"] + stats["doc"]

    _log_memory_event(
        "sleep_cycle",
        "decay",
        float(stats["total_decayed"]),
        "all",
    )

    return (
        f"Sleep cycle complete. Decayed: "
        f"{stats['fact']} facts, {stats['semantic']} semantic, {stats['doc']} docs "
        f"({stats['total_decayed']} total). "
        f"Formula: V * (1 + t/τ)^(-α), floor={MIN_VOLUME}"
    )


@mcp.tool()
def export_identity() -> str:
    """Export the volume map — this IS the personality fingerprint.
    Same data + different volumes = different person.
    Save this to preserve identity across system rebuilds.

    Exports: all volumes from Redis ZSET + distribution statistics.
    """
    identity: dict = {
        "exported_at": datetime.now().isoformat(),
        "version": 2,
        "volumes": {},
        "stats": {},
    }

    try:
        all_entries = get_redis().zrangebyscore(
            VOLUME_INDEX_KEY, "-inf", "+inf", withscores=True
        )
        for entry_key, score in all_entries:  # type: ignore[union-attr]
            identity["volumes"][entry_key] = round(score, 4)

        scores = [s for _, s in all_entries]  # type: ignore[union-attr]
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

    export_path = Path.home() / ".config" / "opencode" / "memory" / "identity.json"
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
        path = str(Path.home() / ".config" / "opencode" / "memory" / "identity.json")

    import_path = Path(path)
    if not import_path.exists():
        return f"Identity file not found: {path}"

    identity = json.loads(import_path.read_text())

    if identity.get("version") != 2:
        return f"Unsupported identity version: {identity.get('version')}. Expected 2."

    volumes = identity.get("volumes", {})
    if not volumes:
        return "Identity file has no volumes."

    pipe = get_redis().pipeline()
    for entry_key, vol in volumes.items():
        pipe.zadd(VOLUME_INDEX_KEY, {entry_key: vol})
    pipe.execute()

    return (
        f"Identity imported from {path}. "
        f"Restored {len(volumes)} volume entries. "
        f"Stats: {json.dumps(identity.get('stats', {}), indent=2)}"
    )


# ===================================================================
# Internal Query Socket (for memory-inject.py hook)
# ===================================================================

QUERY_SOCKET = Path(os.environ.get("OPENCODE_MEMORY_SOCKET", "/tmp/opencode-memory-query.sock"))
_fact_embed_cache: dict | None = None
_fact_embed_lock = threading.Lock()


def _build_fact_embeddings() -> dict | None:
    try:
        r = get_redis()
        raw_facts = r.hgetall(f"{REDIS_PREFIX}:facts")
        if not raw_facts:
            return None

        keys, values, texts = [], [], []
        for key, raw in raw_facts.items():  # type: ignore[union-attr]
            try:
                parsed = json.loads(raw)
                value = parsed.get("value", "")
                keys.append(key)
                values.append(value)
                texts.append(f"{key}: {value[:300]}")
            except (json.JSONDecodeError, ValueError):
                continue

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

    r = get_redis()
    pipe = r.pipeline()
    for idx in top_indices:
        pipe.zscore(VOLUME_INDEX_KEY, f"fact:{cache['keys'][idx]}")
    volumes = pipe.execute()

    results = []
    for i, idx in enumerate(top_indices):
        sim = float(similarities[idx])
        if sim < 0.20:
            continue
        vol = volumes[i] if volumes[i] is not None else 50.0
        results.append(
            {
                "key": cache["keys"][idx],
                "value": cache["values"][idx][:200],
                "score": round(sim, 3),
                "volume": round(float(vol), 1),
            }
        )
    return results


def _search_semantic_memories(query_embedding: np.ndarray, n: int = 5) -> list[dict]:
    try:
        collection = get_collection()
        results = collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=n,
            include=["documents", "metadatas", "distances"],
        )

        if not results["documents"] or not results["documents"][0]:
            return []

        r = get_redis()
        pipe = r.pipeline()
        ids = results["ids"][0]
        for mem_id in ids:
            pipe.zscore(VOLUME_INDEX_KEY, f"semantic:{mem_id}")
        volumes = pipe.execute()

        memories = []
        for i, doc in enumerate(results["documents"][0]):
            dist = results["distances"][0][i]  # type: ignore[index]
            meta = results["metadatas"][0][i] if results["metadatas"] else {}
            vol = volumes[i] if volumes[i] is not None else 40.0
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
        semantic = _search_semantic_memories(query_embedding, n_semantic)

        elapsed_ms = int((time.time() - t0) * 1000)

        response = json.dumps(
            {"facts": facts, "semantic": semantic, "time_ms": elapsed_ms}
        )
        conn.sendall(response.encode() + b"\n")
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.sendall(
                json.dumps({"error": str(e), "facts": [], "semantic": []}).encode()
                + b"\n"
            )
    finally:
        conn.close()


def _start_query_socket():
    if QUERY_SOCKET.exists():
        QUERY_SOCKET.unlink()

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


# ===================================================================
# Entrypoint
# ===================================================================

if __name__ == "__main__":
    _start_query_socket()
    mcp.run(transport="stdio")
