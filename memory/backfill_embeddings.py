#!/usr/bin/env python3
"""Bring the `embeddings` table up to date now, in the foreground.

The running server does this by itself: the hook-socket owner syncs the index
every minute (`server._maintain_index`). This CLI runs the same pass on demand
-- after a model change with `--force`, or with no server running. Each key's
rows carry the hash of the text they came from, so a re-run embeds only what
is missing or changed and drops what is gone; it never duplicates.

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
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db  # noqa: E402
import server  # noqa: E402
import vector_index  # noqa: E402


def _fallback_documents() -> list[tuple[str, str]]:
    """`(doc_id, text)` from the store's own semantic_fallback table."""
    return [(doc_id, text) for doc_id, text, _meta in db.semantic_iter()]


def _semantic_union() -> list[tuple[str, str]]:
    """Both semantic stores, deduped by id. Chroma first, fallback wins ties.

    The fallback is the newer of the two, so if an id ever appears in both its
    text is the one that reflects the latest write.
    """
    return vector_index.semantic_sources(server._chroma_documents() or [])


def backfill(kind: str, force: bool) -> tuple[int, int]:
    """Sync one layer. Returns ``(records_embedded, chunks_written)``."""
    pairs = vector_index.sources(
        kind, notes_dir=server.NOTES_DIR, chroma_documents=server._chroma_documents
    )
    if pairs is None:
        print(f"{kind}: source unreadable, skipped")
        return 0, 0
    plan = vector_index.plan(kind, pairs, server.EMBED_MODEL_NAME, force=force)
    print(f"{kind}: {len(pairs)} records, {len(plan.todo)} to embed, {len(plan.gone)} gone")
    started = time.perf_counter()

    def progress(done: int, total: int) -> bool:
        rate = done / max(1e-9, time.perf_counter() - started)
        print(f"  {kind}: {done}/{total} records, {rate:.0f} rec/s")
        return False

    result = vector_index.sync(
        [plan], server.embed_chunks_many, server.EMBED_MODEL_NAME, pace=progress
    )[kind]
    return result.embedded, result.chunks


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
    chroma = server._chroma_documents() or []
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
