"""Keep the ``embeddings`` table in step with the store it indexes.

Until this module the table had exactly one writer, a one-shot backfill run on
2026-08-23, so every fact, lesson and note written after that day was
invisible to ``recall``'s meaning-based search. Here the index becomes a
living one: each key's rows carry the sha1 of the text they were embedded from
(migration 7), and a sync pass compares that with the record as it reads now.

    no rows            MISSING     never indexed; recall cannot see it at all
    hash differs       CHANGED     the vectors describe an older text
    hash unknown ("")  UNVERIFIED  a pre-migration row; still served meanwhile
    source gone        deleted     vectors that answer for nothing

Work runs in that priority order, so what recall cannot see at all comes
first and the one-off re-embedding of legacy rows comes last.

Imports ``db`` and nothing heavier: the encoder is passed in, because inside
the running server that module is ``__main__``, and importing ``server`` from
here would build a second copy of it, second model included.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import db

#: The prompt hook's one-vector-per-fact index. It lives in the same table
#: under its own kind so a restart re-encodes only the facts that changed.
HOOK_FACT_KIND = "fact_head"

MISSING, CHANGED, UNVERIFIED = 0, 1, 2

Sources = list[tuple[str, str]]
#: Texts in, per-text chunk vectors out (float32 LE bytes, normalized).
Embed = Callable[[list[str]], list[list[bytes]]]
#: Called between steps with (records done, records planned); True stops.
Pace = Callable[[int, int], bool]


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sources: the exact text each kind is embedded from
# ---------------------------------------------------------------------------


def fact_sources() -> Sources:
    return [(key, f"{key}: {parsed.get('value', '')}") for key, parsed in db.fact_all().items()]


def causal_sources() -> Sources:
    return [
        (row["id"], " ".join(p for p in (row["id"], row["text"], row["cause"], row["effect"]) if p))
        for row in db.causal_all(limit=1_000_000)
    ]


def doc_sources(notes_dir: Path) -> Sources | None:
    """``None`` when the notes directory is absent: an unmounted or moved
    directory must not read as "every note was deleted"."""
    if not notes_dir.is_dir():
        return None
    out: Sources = []
    for path in sorted(notes_dir.rglob("*.md")):
        rel = str(path.relative_to(notes_dir).with_suffix(""))
        out.append((rel, f"{rel}\n{path.read_text(encoding='utf-8')}"))
    return out


def semantic_sources(chroma: Iterable[tuple[str, str]]) -> Sources:
    """Chroma's documents and the fallback table, deduped by id; the fallback
    is the newer store, so it wins a tie."""
    merged = dict(chroma)
    merged.update((doc_id, text) for doc_id, text, _meta in db.semantic_iter())
    return sorted(merged.items())


def sources(
    kind: str, *, notes_dir: Path, chroma_documents: Callable[[], Sources | None]
) -> Sources | None:
    """One kind's ``(key, text)`` pairs, or ``None`` when its source could not
    be read this time -- a pass skips that kind rather than delete its vectors."""
    if kind == "fact":
        return fact_sources()
    if kind == "causal":
        return causal_sources()
    if kind == "doc":
        return doc_sources(notes_dir)
    if kind == "semantic":
        chroma = chroma_documents()
        return None if chroma is None else semantic_sources(chroma)
    raise ValueError(f"unknown kind: {kind}")


# ---------------------------------------------------------------------------
# Plan and sync
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Work:
    priority: int
    kind: str
    key: str
    text: str
    digest: str


@dataclass(frozen=True)
class Plan:
    kind: str
    gone: list[str]
    todo: list[Work]


@dataclass
class Result:
    embedded: int = 0
    chunks: int = 0
    deleted: int = 0
    pending: int = 0


def plan(kind: str, pairs: Sources, model: str, force: bool = False) -> Plan:
    """What one kind needs: keys whose vectors must go, records to embed.

    A blank text counts as absent -- it has nothing to embed, and planning it
    would re-embed it on every pass. ``force`` re-embeds even current keys
    (after a model change).
    """
    stored = db.embedding_hashes(kind, model)
    live: set[str] = set()
    todo: list[Work] = []
    for key, text in pairs:
        if not (text or "").strip():
            continue
        live.add(key)
        digest = text_hash(text)
        have = stored.get(key)
        if have is None:
            priority = MISSING
        elif have == digest and not force:
            continue
        elif have in ("", digest):
            priority = UNVERIFIED
        else:
            priority = CHANGED
        todo.append(Work(priority, kind, key, text, digest))
    gone = sorted(key for key in stored if key not in live)
    return Plan(kind, gone, todo)


def sync(
    plans: list[Plan], embed: Embed, model: str, *, step: int = 32, pace: Pace | None = None
) -> dict[str, Result]:
    """Apply plans: drop gone keys, then embed ``step`` records at a time.

    The encoder runs OUTSIDE any transaction; each step's rows are written in
    one short one, so other writers wait milliseconds, never a GPU batch.
    Records are taken by priority first and plan order second, so a stop
    between steps (``pace`` returning True) leaves only the least urgent work
    behind, counted as ``pending``. Anything that changes after planning is
    simply planned again next pass.
    """
    results = {p.kind: Result() for p in plans}
    doomed = [(p.kind, key) for p in plans for key in p.gone]
    if doomed:
        with db.write_txn() as conn:
            for kind, key in doomed:
                db.embedding_delete(kind, key, conn=conn)
                results[kind].deleted += 1

    order = {p.kind: i for i, p in enumerate(plans)}
    work = sorted((w for p in plans for w in p.todo), key=lambda w: (w.priority, order[w.kind]))
    done = 0
    while done < len(work):
        batch = work[done : done + step]
        vectors = embed([w.text for w in batch])
        with db.write_txn() as conn:
            for w, vecs in zip(batch, vectors, strict=True):
                if vecs:
                    db.embedding_upsert(
                        w.kind, w.key, vecs, model=model, dim=len(vecs[0]) // 4,
                        conn=conn, src_hash=w.digest,
                    )
                    results[w.kind].embedded += 1
                    results[w.kind].chunks += len(vecs)
                else:
                    db.embedding_delete(w.kind, w.key, conn=conn)
        done += len(batch)
        if done < len(work) and pace is not None and pace(done, len(work)):
            for w in work[done:]:
                results[w.kind].pending += 1
            break
    return results
