#!/usr/bin/env python3
"""
Memory injection hook for opencode.

Runs before every prompt. Searches all memory layers for context
relevant to the current user message, and prepends it to the prompt.

Uses Unix socket to query the running MCP server's warm encoder
for fast semantic search (~50-100ms). Falls back to keyword matching
if the socket is unavailable.
"""

import glob as _glob
import json
import os
import os as _os
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_VENV_SITE_PATTERN = _os.path.join(
    _os.path.dirname(__file__), ".venv", "lib", "python3.*", "site-packages"
)
_VENV_SITES = _glob.glob(_VENV_SITE_PATTERN)
_VENV_SITE = _VENV_SITES[0] if _VENV_SITES else ""
if _VENV_SITE and _os.path.isdir(_VENV_SITE) and _VENV_SITE not in sys.path:
    sys.path.insert(0, _VENV_SITE)

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
MEMORY_DB = Path(
    os.environ.get(
        "OPENCODE_MEMORY_DB",
        str(Path.home() / ".config" / "opencode" / "memory" / "memory.db"),
    )
)
QUERY_SOCKET = os.environ.get("OPENCODE_MEMORY_SOCKET", "/tmp/opencode-memory-query.sock")


def memory_ro_conn() -> sqlite3.Connection | None:
    """Open the memory store READ-ONLY. Hooks must never write or migrate."""
    if not MEMORY_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def query_semantic(message: str, n_facts: int = 10, n_semantic: int = 5) -> dict | None:
    if not os.path.exists(QUERY_SOCKET):
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(QUERY_SOCKET)

        request = (
            json.dumps({"query": message, "n_facts": n_facts, "n_semantic": n_semantic})
            + "\n"
        )
        sock.sendall(request.encode())

        data = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        sock.close()
        return json.loads(data.decode().strip())
    except Exception:
        return None


def get_top_volume_entries(top_n: int = 5) -> list[str]:
    conn = memory_ro_conn()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT entity_key, volume FROM volumes "
            "ORDER BY volume DESC, entity_key ASC LIMIT ?",
            (top_n,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    return [f"  {row['entity_key']} (vol:{float(row['volume']):.0f})" for row in rows]


def get_semantic_count() -> int:
    try:
        db_path = CHROMA_DIR / "chroma.sqlite3"
        if not db_path.exists():
            return 0
        conn = sqlite3.connect(str(db_path), timeout=1)
        cursor = conn.execute("SELECT COUNT(*) FROM embeddings")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception:
        return -1


def get_doc_stats() -> tuple[int, int]:
    """Count doc folders and docs without enumerating them.

    Enumerating the folders cost 967 of 2275 injected tokens (measured
    2026-08-22, cl100k) for a listing that is identical on every prompt and
    reachable on demand via ``list_docs()``.
    """
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        folders = 0
        total = 0
        for folder in NOTES_DIR.iterdir():
            if not folder.is_dir():
                continue
            count = sum(1 for _ in folder.glob("*.md"))
            if count:
                folders += 1
                total += count
        return folders, total
    except Exception:
        return 0, -1


#: Paid on every turn of every session, so it is a hard ceiling.
MAX_INJECT_TOKENS = 1300

#: Upper bound, not the folklore 0.25: this store's mixed RU/EN identifier-heavy
#: text measures ~0.40 tokens/char under cl100k. Keeps the hook tokenizer-free.
_TOKENS_PER_CHAR = 0.5


def estimate_tokens(text: str) -> int:
    return int(len(text) * _TOKENS_PER_CHAR) + 1


TRIM_MARKER = "[Memory] (trimmed to prompt budget)"


def fit_to_budget(sections: list[str], max_tokens: int = MAX_INJECT_TOKENS) -> list[str]:
    """Drop trailing detail lines from the widest section until the payload fits.

    Section headers are a floor: they are never dropped, so a budget too small
    to hold them yields a header-only payload rather than an empty one. The
    trim marker's own cost is reserved before trimming, otherwise appending it
    would push the result back over budget.
    """
    sections = list(sections)
    if estimate_tokens("\n".join(sections)) <= max_tokens:
        return sections

    budget = max_tokens - estimate_tokens("\n" + TRIM_MARKER)
    while estimate_tokens("\n".join(sections)) > budget:
        widest = max(range(len(sections)), key=lambda i: len(sections[i]))
        lines = sections[widest].split("\n")
        if len(lines) < 2:
            break
        sections[widest] = "\n".join(lines[:-1])
    sections.append(TRIM_MARKER)
    return sections


STOPWORDS = {
    "и",
    "в",
    "не",
    "на",
    "я",
    "с",
    "что",
    "а",
    "по",
    "это",
    "к",
    "но",
    "он",
    "из",
    "за",
    "то",
    "все",
    "как",
    "или",
    "мы",
    "ты",
    "от",
    "бы",
    "the",
    "a",
    "is",
    "it",
    "to",
    "in",
    "and",
    "of",
    "for",
    "on",
    "that",
    "this",
    "with",
    "i",
    "you",
    "we",
    "do",
    "can",
    "my",
    "me",
    "be",
    "so",
    "давай",
    "нужно",
    "хочу",
    "можно",
    "пожалуйста",
    "сделай",
    "покажи",
    "ладно",
    "ок",
    "да",
    "нет",
    "ну",
    "вот",
    "тут",
    "там",
    "еще",
    "уже",
}


def extract_keywords(message: str) -> list[str]:
    words = message.lower().split()
    return [
        w.strip(".,!?()[]{}:;\"'")
        for w in words
        if len(w) > 2 and w.lower().strip(".,!?()[]{}:;\"'") not in STOPWORDS
    ]


def get_relevant_facts_keyword(
    keywords: list[str], top_n: int = 20
) -> tuple[list[str], int]:
    conn = memory_ro_conn()
    if conn is None:
        return [], -1
    try:
        rows = conn.execute(
            "SELECT f.key AS key, f.value AS value, v.volume AS volume "
            "FROM facts f LEFT JOIN volumes v ON v.entity_key = 'fact:' || f.key"
        ).fetchall()
    except sqlite3.Error:
        return [], -1
    finally:
        conn.close()

    if not rows:
        return [], 0

    facts = []
    for row in rows:
        key = row["key"]
        value = row["value"] or ""
        volume = float(row["volume"]) if row["volume"] is not None else 50.0
        key_lower = key.lower()
        val_lower = value.lower()
        match_score = (
            sum(1 for kw in keywords if kw in key_lower or kw in val_lower)
            if keywords
            else 0
        )
        if match_score > 0 or volume >= 70.0:
            facts.append((key, value, volume, match_score))

    facts.sort(key=lambda x: (x[3], x[2]), reverse=True)

    output = []
    for key, value, vol, _score in facts[:top_n]:
        display_val = value[:80] + "..." if len(value) > 80 else value
        output.append(f"  {key}: {display_val} (vol:{vol:.0f})")
    return output, len(facts)


def main():
    user_message = ""
    try:
        if not sys.stdin.isatty():
            hook_data = json.load(sys.stdin)
            user_message = hook_data.get("prompt", "")
    except (json.JSONDecodeError, Exception):
        pass

    if not user_message and len(sys.argv) > 1:
        user_message = " ".join(sys.argv[1:])

    sections = []

    tz_offset = timezone(timedelta(hours=0))
    now = datetime.now(tz_offset)
    time_str = now.strftime("%Y-%m-%d %H:%M %a")
    sections.append(f"[Clock] {time_str} (UTC)")

    semantic_results = query_semantic(user_message) if user_message else None

    if semantic_results and "error" not in semantic_results:
        facts = semantic_results.get("facts", [])
        if facts:
            fact_lines = []
            for f in facts:
                display_val = (
                    f["value"][:80] + "..." if len(f["value"]) > 80 else f["value"]
                )
                fact_lines.append(
                    f"  {f['key']}: {display_val} (vol:{f['volume']:.0f})"
                )
            sections.append(
                f"[Memory] Relevant facts ({len(facts)}):\n" + "\n".join(fact_lines)
            )

        memories = semantic_results.get("semantic", [])
        if memories:
            mem_lines = []
            for m in memories:
                text = m["text"][:120] + "..." if len(m["text"]) > 120 else m["text"]
                tags = f" [{m['tags']}]" if m.get("tags") else ""
                mem_lines.append(
                    f"  [{m['score']:.2f}] {text}{tags} (vol:{m['volume']:.0f})"
                )
            sections.append("[Memory] Relevant memories:\n" + "\n".join(mem_lines))

        elapsed = semantic_results.get("time_ms", "?")
        sections.append(f"[Memory] Semantic search: {elapsed}ms")
    else:
        keywords = extract_keywords(user_message) if user_message else []
        fact_lines, fact_total = get_relevant_facts_keyword(keywords)
        if fact_total == -1:
            sections.append("[Memory] Facts: store unavailable")
        elif fact_total > 0:
            header = (
                f"[Memory] Relevant facts ({fact_total} matched)"
                if keywords
                else "[Memory] Top facts by volume"
            )
            sections.append(header + ":\n" + "\n".join(fact_lines))
        elif keywords:
            sections.append(
                "[Memory] No matching facts for: " + ", ".join(keywords[:5])
            )

        sem_count = get_semantic_count()
        if sem_count > 0:
            sections.append(
                f"[Memory] Semantic memories: {sem_count} (socket unavailable, use recall())"
            )

    top_entries = get_top_volume_entries(5)
    if top_entries:
        sections.append("[Memory] Loudest:\n" + "\n".join(top_entries))

    doc_folders, doc_total = get_doc_stats()
    if doc_total > 0:
        sections.append(
            f"[Memory] Docs: {doc_total} in {doc_folders} folders "
            "(list_docs() to browse, read_doc(folder, name) to open)"
        )

    sections.append(
        "[Memory] recall(query) for deep search. save_fact/remember/save_doc to store."
    )

    print("\n".join(fit_to_budget(sections)))


if __name__ == "__main__":
    main()
