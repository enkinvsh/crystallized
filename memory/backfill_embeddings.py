#!/usr/bin/env python3
"""Backfill the `embeddings` table for every layer of the store.

Idempotent and resumable: `db.embedding_upsert` replaces all chunks of a key,
so re-running never duplicates and an interrupted run simply resumes. Pass
`--force` to re-embed keys that already have vectors (needed after a model
change); by default they are skipped, which is what makes a resume cheap.

THIS SCRIPT IS THE ONE PLACE OUTSIDE THE SERVER PROCESS PERMITTED TO LOAD THE
MODEL. It imports `server` and uses `server.get_encoder()`, the same lazy
singleton the running server uses, so there is never a second
SentenceTransformer alive in a hook or a daemon. Run it while the server is
stopped, or accept that it holds its own copy for the duration.

The semantic layer is the UNION of two disjoint stores, keyed by document id:

    chroma embedding_id : 682     what was written BEFORE safe-mode
    semantic_fallback   : 615     everything written SINCE
    intersection        :   0
    union               : 1297    the count the system reports

Neither is a mirror of the other — reading only Chroma drops every memory
written since safe-mode, which is the half most likely to matter. They are
deduped by id anyway rather than relying on the disjointness holding.

Chroma is read from its OWN sqlite file, READ-ONLY, never through its API —
that API segfaults the interpreter on this install (chromadb 1.5.9 / Python
3.13, fatal inside `rust.py::_count`, EXIT=139), and a segfault cannot be
caught.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db  # noqa: E402
import server  # noqa: E402


def _existing_keys(kind: str) -> set[str]:
    rows, _ = db.embedding_load(kind=kind, model=server.EMBED_MODEL_NAME)
    return {r["key"] for r in rows}


def _chroma_documents() -> list[tuple[str, str]]:
    """`(id, document)` straight out of chroma.sqlite3, read-only."""
    conn = server._sqlite_ro_conn()
    if conn is None:
        return []
    try:
        cur = conn.execute(
            "SELECT em.id, em.string_value FROM embedding_metadata em "
            "WHERE em.key = 'chroma:document' AND em.string_value IS NOT NULL"
        )
        return [(str(row[0]), row[1]) for row in cur.fetchall()]
    except sqlite3.Error as exc:
        print(f"  ! chroma read failed: {exc}")
        return []
    finally:
        conn.close()


def _fallback_documents() -> list[tuple[str, str]]:
    """`(doc_id, text)` from the store's own semantic_fallback table."""
    return [(doc_id, text) for doc_id, text, _meta in db.semantic_iter()]


def _semantic_union() -> list[tuple[str, str]]:
    """Both semantic stores, deduped by id. Chroma first, fallback wins ties.

    The fallback is the newer of the two, so if an id ever appears in both its
    text is the one that reflects the latest write.
    """
    merged: dict[str, str] = dict(_chroma_documents())
    merged.update(dict(_fallback_documents()))
    return sorted(merged.items())


def _iter_sources(kind: str) -> list[tuple[str, str]]:
    if kind == "fact":
        return [
            (key, f"{key}: {parsed.get('value', '')}")
            for key, parsed in db.fact_all().items()
        ]
    if kind == "causal":
        return [
            (
                row["id"],
                " ".join(
                    p for p in (row["id"], row["text"], row["cause"], row["effect"]) if p
                ),
            )
            for row in db.causal_all(limit=1_000_000)
        ]
    if kind == "doc":
        out: list[tuple[str, str]] = []
        for path in sorted(server.NOTES_DIR.rglob("*.md")):
            rel = str(path.relative_to(server.NOTES_DIR).with_suffix(""))
            out.append((rel, f"{rel}\n{path.read_text(encoding='utf-8')}"))
        return out
    if kind == "semantic":
        return _semantic_union()
    raise ValueError(f"unknown kind: {kind}")


def backfill(kind: str, force: bool) -> tuple[int, int]:
    """Embed one layer. Returns ``(records_embedded, chunks_written)``."""
    sources = _iter_sources(kind)
    done = set() if force else _existing_keys(kind)
    todo = [(k, text) for k, text in sources if k not in done and (text or "").strip()]

    print(f"{kind}: {len(sources)} records, {len(sources) - len(todo)} already embedded")
    records = chunks = 0
    started = time.perf_counter()
    for i, (key, text) in enumerate(todo, 1):
        vectors = server.embed_chunks(text)
        if not vectors:
            continue
        db.embedding_upsert(
            kind, key, vectors,
            model=server.EMBED_MODEL_NAME,
            dim=len(vectors[0]) // 4,
        )
        records += 1
        chunks += len(vectors)
        if i % 50 == 0 or i == len(todo):
            rate = i / max(1e-9, time.perf_counter() - started)
            print(f"  {kind}: {i}/{len(todo)} records, {chunks} chunks, {rate:.0f} rec/s")
    return records, chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kinds", default=",".join(db.EMBEDDING_KINDS),
        help="comma-separated subset of: " + ", ".join(db.EMBEDDING_KINDS),
    )
    parser.add_argument(
        "--force", action="store_true",
        help="re-embed keys that already have vectors (use after a model change)",
    )
    args = parser.parse_args(argv)

    print(f"model: {server.EMBED_MODEL_NAME}")
    print(f"store: {db.DB_PATH}")
    chroma = _chroma_documents()
    fallback = _fallback_documents()
    union = _semantic_union()
    print(
        f"semantic corpus — chroma.sqlite3: {len(chroma)}, "
        f"semantic_fallback: {len(fallback)}, "
        f"overlap: {len(chroma) + len(fallback) - len(union)}, "
        f"union: {len(union)}"
    )

    tally: dict[str, tuple[int, int]] = {}
    for kind in [k.strip() for k in args.kinds.split(",") if k.strip()]:
        tally[kind] = backfill(kind, args.force)

    print("\n=== tally ===")
    total_chunks = 0
    for kind, (records, chunks) in tally.items():
        print(f"  {kind:<9} {records:>6} records -> {chunks:>7} chunks")
        total_chunks += chunks
    rows, matrix = db.embedding_load(model=server.EMBED_MODEL_NAME)
    size_mb = matrix.nbytes / (1024 * 1024) if len(rows) else 0.0
    print(f"  {'TOTAL':<9} {'':>6}            {total_chunks:>7} chunks written")
    print(f"  matrix now: {len(rows)} chunks, {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
