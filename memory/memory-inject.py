#!/usr/bin/env python3
"""
Memory injection hook for opencode.

Runs before every prompt. Searches all memory layers for context
relevant to the current user message, and prepends it to the prompt.

Uses Unix socket to query the running MCP server's warm encoder
for fast semantic search (~50-100ms). Falls back to keyword matching
if the socket is unavailable.
"""

import json
import os
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os as _os
import glob as _glob

_VENV_SITE_PATTERN = _os.path.join(
    _os.path.dirname(__file__), ".venv", "lib", "python3.*", "site-packages"
)
_VENV_SITES = _glob.glob(_VENV_SITE_PATTERN)
_VENV_SITE = _VENV_SITES[0] if _VENV_SITES else ""
if _VENV_SITE and _os.path.isdir(_VENV_SITE) and _VENV_SITE not in sys.path:
    sys.path.insert(0, _VENV_SITE)

NOTES_DIR = Path.home() / ".config" / "opencode" / "memory" / "notes"
CHROMA_DIR = Path.home() / ".config" / "opencode" / "memory" / "chroma_db"
REDIS_PREFIX = "opencode:memory"
VOLUME_INDEX_KEY = f"{REDIS_PREFIX}:volume_index"
QUERY_SOCKET = "/tmp/opencode-memory-query.sock"

_redis_conn = None


def get_redis():
    global _redis_conn
    if _redis_conn is None:
        import redis

        _redis_conn = redis.Redis(host="localhost", port=6379, decode_responses=True)
    return _redis_conn


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
    try:
        r = get_redis()
        entries = r.zrevrange(VOLUME_INDEX_KEY, 0, top_n - 1, withscores=True)
    except Exception:
        return []

    result = []
    for entry_key, score in entries:
        result.append(f"  {entry_key} (vol:{score:.0f})")
    return result


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


def get_doc_listing() -> tuple[list[str], int]:
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        lines = []
        total = 0
        for folder in sorted(NOTES_DIR.iterdir()):
            if not folder.is_dir():
                continue
            docs = sorted(f.stem for f in folder.glob("*.md"))
            if docs:
                total += len(docs)
                lines.append(f"  {folder.name}/ ({len(docs)})")
        return lines, total
    except Exception:
        return [], -1


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
    try:
        r = get_redis()
        raw_facts = r.hgetall(f"{REDIS_PREFIX}:facts")
    except Exception:
        return [], -1

    if not raw_facts:
        return [], 0

    parsed_facts = []
    for key, raw in raw_facts.items():
        try:
            parsed = json.loads(raw)
            value = parsed.get("value", "")
            parsed_facts.append((key, value))
        except (json.JSONDecodeError, ValueError):
            continue

    if not parsed_facts:
        return [], 0

    try:
        r = get_redis()
        pipe = r.pipeline()
        for key, _ in parsed_facts:
            pipe.zscore(VOLUME_INDEX_KEY, f"fact:{key}")
        scores = pipe.execute()
    except Exception:
        scores = [50.0] * len(parsed_facts)

    facts = []
    for i, (key, value) in enumerate(parsed_facts):
        volume = scores[i] if scores[i] is not None else 50.0
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
    for key, value, vol, score in facts[:top_n]:
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
            sections.append(f"[Memory] Relevant memories:\n" + "\n".join(mem_lines))

        elapsed = semantic_results.get("time_ms", "?")
        sections.append(f"[Memory] Semantic search: {elapsed}ms")
    else:
        keywords = extract_keywords(user_message) if user_message else []
        fact_lines, fact_total = get_relevant_facts_keyword(keywords)
        if fact_total == -1:
            sections.append("[Memory] Facts: Redis unavailable")
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

    doc_lines, doc_total = get_doc_listing()
    if doc_total > 0:
        sections.append(f"[Memory] Docs ({doc_total}):\n" + "\n".join(doc_lines))

    sections.append(
        "[Memory] recall(query) for deep search. save_fact/remember/save_doc to store."
    )

    print("\n".join(sections))


if __name__ == "__main__":
    main()
