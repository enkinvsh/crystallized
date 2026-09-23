#!/usr/bin/env python3
"""
Memory injection hook for opencode.

Runs before every prompt. Searches all memory layers for context
relevant to the current user message, and prepends it to the prompt.

Uses Unix socket to query the running MCP server's warm encoder
for fast semantic search (~50-100ms). Without it (server starting or
down) only the clock and a one-line recall() hint go in: the keyword
fallback this replaced dumped loosely matching facts into the prompt.
"""

import contextlib
import glob as _glob
import json
import math
import os
import os as _os
import re
import socket
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_VENV_SITE_PATTERN = _os.path.join(
    _os.path.dirname(__file__), ".venv", "lib", "python3.*", "site-packages"
)
_VENV_SITES = _glob.glob(_VENV_SITE_PATTERN)
_VENV_SITE = _VENV_SITES[0] if _VENV_SITES else ""
if _VENV_SITE and _os.path.isdir(_VENV_SITE) and _VENV_SITE not in sys.path:
    sys.path.insert(0, _VENV_SITE)

MEMORY_DB = Path(
    os.environ.get(
        "OPENCODE_MEMORY_DB",
        str(Path.home() / ".config" / "opencode" / "memory" / "memory.db"),
    )
)
QUERY_SOCKET = os.environ.get("OPENCODE_MEMORY_SOCKET", "/tmp/opencode-memory-query.sock")

sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import projects  # noqa: E402  (after the path tweak; stdlib-only module)

#: Calibrated 2026-09-23 on 817 short owner messages: the noise top semantic
#: score is p95 0.66 / p99 0.76, so a bare semantic hit needs >= 0.72.
INJECT_MIN_SCORE = 0.72
#: Latin terms inside the owner's Russian messages name entities (ranetka, NL,
#: w1, railway, code-execution); pure semantic search misses them
#: cross-lingually (live check: "ranetka сифон на NL" -> top hit meowzic 0.62,
#: "pena darkin настройка бота" -> tattoo 0.47). A rare term in a fact's key
#: lifts that fact by up to ANCHOR_BONUS_MAX.
ANCHOR_BONUS_MAX = 0.35
ANCHOR_IDF_FULL = 5.0
PROJECT_BONUS = 0.05
MAX_FACTS = 4
MAX_TERMS = 20
SEMANTIC_POOL = 400
#: Per-session record of injected keys; earlier hook blocks stay in context.
DEDUPE_TTL_S = 6 * 3600
INJECTED_DIR = Path(os.environ.get("OPENCODE_MEMORY_INJECTED_DIR", "/tmp/opencode-memory-inject"))
ANCHOR_MAX_TERMS = 8
#: Plugin notifications stored as user messages (DCP); they fire the hook too.
NOTIFICATION_PREFIXES = ("▣ DCP",)
#: Safe-mode substring hits carry score 0.0 and were pure noise in practice.
MEMORY_MIN_SCORE = 0.45


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

    A section that is already a lone header is set aside, not treated as the end
    of the pass. Ending there would let one long single-line section — the clock
    header is the longest — strand detail lines in every other section once it
    became the widest, which is how the floor invariant used to be violated.
    """
    sections = list(sections)
    if estimate_tokens("\n".join(sections)) <= max_tokens:
        return sections

    budget = max_tokens - estimate_tokens("\n" + TRIM_MARKER)
    trimmable = set(range(len(sections)))
    while trimmable and estimate_tokens("\n".join(sections)) > budget:
        # Longest first; lowest index breaks ties so the pass stays deterministic.
        widest = max(trimmable, key=lambda i: (len(sections[i]), -i))
        lines = sections[widest].split("\n")
        if len(lines) < 2:
            trimmable.discard(widest)
            continue
        sections[widest] = "\n".join(lines[:-1])
    sections.append(TRIM_MARKER)
    return sections


def clock_line(now: datetime | None = None) -> str:
    """Wall-clock header: local time first, UTC alongside it.

    The store keeps two timestamp conventions. ``facts``, ``docs`` and
    ``events`` are written with a naive ``datetime.now()`` — local time.
    ``causal_memories`` and ``belief_state`` are written UTC-aware. Printing
    only UTC here made every freshly written fact look five hours into the
    future and invited comparing the two layers against each other. Both
    readings are shown so neither has to be inferred.
    """
    local = (now or datetime.now()).astimezone()
    offset = local.strftime("%z")
    return (
        f"[Clock] {local:%Y-%m-%d %H:%M %a} "
        f"{offset[:3]}:{offset[3:]} (UTC {local.astimezone(UTC):%H:%M})"
    )


ENGLISH_STOPWORDS = frozenset(
    """
    a an the and or but if then so to of in on at by for from with without as
    is are was were be been do does did have has had it its this that these
    those there here what why how when where which who i me my we our you your
    he she they them their not no yes ok okay can could should would will may
    might must just also only very too more most less all any some each every
    again still now up out over about into than like please thanks via vs etc
    done fixed live new old fix check test verified applied deployed shipped
    status final plan report note notes update updated
    """.split()
)

PATH_URL_STOPWORDS = frozenset(
    """
    users mac documents projects downloads desktop library private tmp var usr
    opt home http https www github.com image png jpg jpeg webp true false null none
    """.split()
)

_TERM = re.compile(r"[a-z0-9][a-z0-9\-\.]*[a-z0-9]")
_CYRILLIC_WORD = re.compile(r"[а-яё]{2,}", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"[_\-]+", " ", s.lower())


def extract_terms(message: str) -> list[str]:
    """Latin entity names in the message, normalized like fact keys."""
    terms: dict[str, None] = {}
    for raw in _TERM.findall(message.lower()):
        token = raw.strip(".-")
        if (
            not token
            or (token.isdigit() and len(token) < 3)
            or token in ENGLISH_STOPWORDS
            or token in PATH_URL_STOPWORDS
        ):
            continue
        terms.setdefault(_norm(token), None)
    return list(terms)[:MAX_TERMS]


def anchor_terms(message: str) -> list[str]:
    """Terms allowed to anchor facts; [] for a mostly-Latin long message.

    The owner writes Russian, so Latin inside Russian text names entities. A
    long mostly-Latin message is a paste (prompt, config, log) whose Latin words
    are content, not names: there the semantic score alone decides.
    """
    terms = extract_terms(message)
    cyrillic_words = len(_CYRILLIC_WORD.findall(message))
    if len(terms) <= ANCHOR_MAX_TERMS or cyrillic_words >= len(terms):
        return terms
    return []


def load_live_facts(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(key, normalized key, normalized value) of every non-superseded, unexpired fact."""
    rows = conn.execute(
        "SELECT key, value FROM facts WHERE superseded_by IS NULL AND expires_at > ?",
        (datetime.now().isoformat(),),
    ).fetchall()
    return [(row[0], _norm(row[0]), _norm(row[1] or "")) for row in rows]


def anchor_strengths(
    terms: list[str], live: list[tuple[str, str, str]], max_terms: int = ANCHOR_MAX_TERMS
) -> dict[str, float]:
    """Sum of term IDF per fact: full for a key hit, half for a value-only hit.

    Only the max_terms rarest matching terms count, so many common words
    cannot add up to a bonus.
    """
    n = len(live)
    per_term: list[list[tuple[str, float]]] = []
    for term in terms:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])")
        hits: list[tuple[str, float]] = []
        for key, nkey, nvalue in live:
            # Substring test first: a bare regex scan cost 65-150 ms per term.
            if term in nkey and pattern.search(nkey):
                hits.append((key, 1.0))
            elif term in nvalue and pattern.search(nvalue):
                hits.append((key, 0.5))
        if hits:
            per_term.append(hits)
    if len(per_term) > max_terms:
        per_term = sorted(per_term, key=len)[:max_terms]
    strength: dict[str, float] = {}
    for hits in per_term:
        idf = math.log(n / len(hits))
        for key, weight in hits:
            strength[key] = strength.get(key, 0.0) + idf * weight
    return strength


def anchor_bonus(strength: float) -> float:
    return ANCHOR_BONUS_MAX * min(strength / ANCHOR_IDF_FULL, 1.0)


def _injected_path(session_id: str) -> Path:
    return INJECTED_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', session_id)}.json"


def load_injected(session_id: str) -> dict[str, float]:
    """Keys already injected in this session, younger than DEDUPE_TTL_S."""
    if not session_id:
        return {}
    try:
        raw = json.loads(_injected_path(session_id).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cutoff = time.time() - DEDUPE_TTL_S
    return {
        str(k): float(v)
        for k, v in raw.items()
        if isinstance(v, (int, float)) and v >= cutoff
    }


def save_injected(session_id: str, injected: dict[str, float]) -> None:
    """Atomic best-effort write; dedupe is an optimization, never a failure."""
    if not session_id:
        return
    path = _injected_path(session_id)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(injected))
        os.replace(tmp, path)
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def rank_facts(
    semantic_facts: list[dict],
    strengths: dict[str, float],
    prefixes: list[str],
    seen: dict[str, float],
) -> list[dict]:
    """Semantic score + anchor bonus + project bonus, above INJECT_MIN_SCORE.

    Only facts in the semantic pool are candidates: an anchor without any
    semantic evidence is not enough to inject.
    """
    ranked = []
    for f in semantic_facts:
        key = f["key"]
        if str(f.get("value", "")).startswith("[superseded") or f.get("expired") or key in seen:
            continue
        final = (
            f.get("score", 0.0)
            + anchor_bonus(strengths.get(key, 0.0))
            + (PROJECT_BONUS if projects.matches(key, prefixes) else 0.0)
        )
        if final >= INJECT_MIN_SCORE:
            ranked.append({**f, "final": final})
    ranked.sort(key=lambda f: f["final"], reverse=True)
    return ranked[:MAX_FACTS]


def session_prefixes(cwd: str) -> list[str]:
    if not cwd:
        return []
    conn = memory_ro_conn()
    if conn is None:
        return []
    try:
        return projects.resolve_prefixes(cwd, conn)
    except (sqlite3.Error, OSError):
        return []
    finally:
        conn.close()


def main():
    user_message = ""
    cwd = ""
    session_id = ""
    try:
        if not sys.stdin.isatty():
            hook_data = json.load(sys.stdin)
            user_message = hook_data.get("prompt", "")
            cwd = hook_data.get("cwd", "") or ""
            session_id = hook_data.get("session_id", "") or ""
    except (json.JSONDecodeError, Exception):
        pass

    if not user_message and len(sys.argv) > 1:
        user_message = " ".join(sys.argv[1:])

    if user_message.lstrip().startswith(NOTIFICATION_PREFIXES):
        return

    prefixes = session_prefixes(cwd)
    sections = [clock_line()]

    # Silence is the default: a line is injected only when it clears a
    # relevance bar. The project index itself is loaded once per session by
    # the opencode plugin, not repeated here on every turn.
    semantic_results = (
        query_semantic(user_message, n_facts=SEMANTIC_POOL) if user_message.strip() else None
    )

    if semantic_results and "error" not in semantic_results:
        terms = anchor_terms(user_message)
        strengths: dict[str, float] = {}
        if terms:
            conn = memory_ro_conn()
            if conn is not None:
                try:
                    strengths = anchor_strengths(terms, load_live_facts(conn))
                except sqlite3.Error:
                    strengths = {}
                finally:
                    conn.close()
        seen = load_injected(session_id)
        facts = rank_facts(semantic_results.get("facts", []), strengths, prefixes, seen)
        if facts:
            fact_lines = []
            for f in facts:
                display_val = (
                    f["value"][:80] + "..." if len(f["value"]) > 80 else f["value"]
                )
                fact_lines.append(f"  [{f['final']:.2f}] {f['key']}: {display_val}")
            sections.append(
                f"[Memory] Relevant facts ({len(facts)}):\n" + "\n".join(fact_lines)
            )
            now = time.time()
            save_injected(session_id, seen | {f["key"]: now for f in facts})

        memories = [
            m for m in semantic_results.get("semantic", []) if m.get("score", 0.0) >= MEMORY_MIN_SCORE
        ]
        if memories:
            mem_lines = []
            for m in memories:
                text = m["text"][:120] + "..." if len(m["text"]) > 120 else m["text"]
                mem_lines.append(f"  [{m['score']:.2f}] {text}")
            sections.append("[Memory] Relevant memories:\n" + "\n".join(mem_lines))
    else:
        sections.append(
            "[Memory] Semantic search unavailable (memory server starting or down); "
            "use recall() if past context matters."
        )

    if prefixes:
        sections.append(f"[Memory] Project: {', '.join(p + '_*' for p in prefixes)}")

    print("\n".join(fit_to_budget(sections)))


if __name__ == "__main__":
    main()
