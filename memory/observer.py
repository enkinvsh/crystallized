#!/usr/bin/env python3
"""Observer — fast, hook-side capture of friction signals into causal_memories L0.

Two entry points, both wired through the Claude-Code / OMO hook layer:

    observer.py --post-tool      # PostToolUse: stdin carries tool name/input/output
    observer.py --session-end    # Stop:        stdin carries transcript_path

Hard rules (these are contract, not style):

* **Budget < 150 ms.** A deadline is armed on entry; every loop checks it and
  bails out with whatever it has collected so far. Partial capture beats a
  stalled tool call.
* **Always ``exit 0``.** A non-zero exit from a hook can abort the agent turn.
  Every failure path — bad JSON, missing file, locked database, unreadable
  transcript — is swallowed.
* **Never print on the PostToolUse path.** Only ``UserPromptSubmit`` stdout is
  injected as context; anything else is noise in the user's terminal.
* **Stage A only.** This process runs regexes from `patterns.py` and writes
  ``layer=0`` rows. LLM extraction (Stage B) is `dream.py`'s job — prompt
  latency stays at zero.
* **Idempotent by construction.** ``id = blake2s(source_ref + normalized text)``
  truncated to 16 hex chars, so re-scanning a transcript is a no-op. This matters
  because the first dream run backfills thousands of existing transcripts.
* **SQLite locks are not errors.** ``sqlite3.OperationalError`` drops the
  observation instead of stalling a tool call.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import blake2s
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db  # noqa: E402
import patterns  # noqa: E402
from db import TELEMETRY_TAGS  # noqa: E402 - re-exported below; owned by the schema layer

__all__ = [
    "BUDGET_MS",
    "TELEMETRY_TAGS",
    "Observation",
    "Deadline",
    "agent_side",
    "main",
    "parse_payload",
    "post_tool_observations",
    "record",
    "session_end_observations",
    "stable_id",
]

# ---------------------------------------------------------------------------
# Budget & limits
# ---------------------------------------------------------------------------

#: Wall-clock ceiling for the whole process, in milliseconds.
BUDGET_MS: float = float(os.environ.get("CRYSTALLIZED_OBSERVER_BUDGET_MS") or 150.0)

#: Newest N transcript lines considered at session end. Transcripts grow without
#: bound; friction that matters is recent.
MAX_TRANSCRIPT_LINES: int = 400

#: Hard cap on a single JSONL line we are willing to json.loads().
MAX_LINE_BYTES: int = 256 * 1024

#: Slice of a tool output kept as evidence text.
SNIPPET_CHARS: int = 280

#: Authoritative store for the agent's own turns.
#:
#: The Stop-hook transcript carries `user`, `tool_use` and `tool_result` entries
#: and nothing else — the agent's side of the exchange is never written to it.
#: That is why every session summary this hook has ever emitted read "0
#: assistant messages", 656 rows out of 656: not a parser bug, a source that
#: does not hold the data. The turns do exist, keyed by the very session id the
#: payload already carries, in opencode's own store.
AGENT_STORE: Path = Path(
    os.environ.get("CRYSTALLIZED_AGENT_STORE")
    or (Path.home() / ".local" / "share" / "opencode" / "opencode.db")
).expanduser()

#: Newest N agent turns pulled per session. Only the turn adjacent to a friction
#: signal is ever read, so this is a safety rail rather than a tuning knob.
MAX_AGENT_TURNS: int = 200

#: Slice of an agent turn kept as the cause of a friction signal.
AGENT_TURN_CHARS: int = 400

#: Stage-A damping. A regex hit is weak evidence; `dream.py` promotes it after
#: cross-session corroboration. 0.75 * 0.4 == 0.30, the documented L0 baseline.
STAGE_A_DAMPING: float = 0.4

L0_LAYER: int = 0

_ERROR_MARKERS: tuple[str, ...] = (
    "traceback (most recent call last)",
    "command not found",
    "permission denied",
    "no such file or directory",
    "segmentation fault",
    "fatal:",
    "error:",
    "exception:",
)


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


class Deadline:
    """Monotonic budget guard. Cheap enough to poll inside a hot loop."""

    __slots__ = ("_end", "_start")

    def __init__(self, budget_ms: float = BUDGET_MS) -> None:
        self._start = time.perf_counter()
        self._end = self._start + (max(budget_ms, 0.0) / 1000.0)

    def expired(self) -> bool:
        return time.perf_counter() >= self._end

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Observation:
    """One L0 causal memory candidate."""

    text: str
    source_ref: str
    session_id: str | None = None
    cause: str | None = None
    effect: str | None = None
    confidence: float = 0.3
    observed_at: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def id(self) -> str:
        return stable_id(self.source_ref, self.text)

    def tag_str(self) -> str:
        return ",".join(self.tags)


def stable_id(source_ref: str, text: str) -> str:
    """Deterministic 16-hex-char id: blake2s(source_ref || normalized text)."""
    norm = patterns.normalize(text).casefold()
    h = blake2s(f"{source_ref}\x00{norm}".encode(), digest_size=8)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def parse_payload(raw: str | bytes | None) -> dict[str, Any]:
    """Parse a hook payload. Never raises.

    Accepts the Claude-Code / OMO JSON object. Returns ``{}`` for anything
    unparseable so callers can proceed on defaults.
    """
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = raw.strip()
    if not raw or not raw.startswith("{"):
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    return None


def _as_text(value: Any, limit: int = SNIPPET_CHARS * 4) -> str:
    """Flatten an arbitrary tool payload into scannable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for k in ("text", "content", "output", "stdout", "stderr", "error", "message"):
            v = value.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, (list, dict)):
                parts.append(_as_text(v, limit))
        if not parts:
            try:
                return json.dumps(value, ensure_ascii=False)[:limit]
            except (TypeError, ValueError):
                return ""
        return "\n".join(parts)[:limit]
    if isinstance(value, list):
        return "\n".join(_as_text(v, limit) for v in value[:20])[:limit]
    return ""


def _looks_like_error(response: Any, text: str) -> bool:
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError") or response.get("error"):
            return True
        if response.get("success") is False:
            return True
        code = response.get("exit_code", response.get("exitCode"))
        if isinstance(code, int) and code != 0:
            return True
    low = text[:SNIPPET_CHARS * 2].casefold()
    return any(marker in low for marker in _ERROR_MARKERS)


# ---------------------------------------------------------------------------
# --post-tool
# ---------------------------------------------------------------------------


def post_tool_observations(payload: dict[str, Any]) -> list[Observation]:
    """Turn one PostToolUse payload into zero or more L0 candidates."""
    tool = str(_first(payload, "tool_name", "toolName", "tool") or "unknown")
    session_id = _first(payload, "session_id", "sessionId", "sessionID")
    session_id = str(session_id) if session_id else None
    observed_at = _first(payload, "timestamp", "observed_at") or _now_iso()
    response = _first(payload, "tool_response", "tool_output", "tool_result", "toolResponse")
    out_text = _as_text(response)
    snippet = out_text[:SNIPPET_CHARS].strip()

    call_key = _first(payload, "tool_use_id", "toolUseId", "call_id") or stable_id(tool, out_text)
    base_ref = f"post-tool:{session_id or 'nosession'}:{call_key}"

    obs: list[Observation] = []

    if snippet and _looks_like_error(response, out_text):
        # No causal pair: this row fires on every failing call, so an episode
        # made of nothing else states one claim unanimously, and dream's
        # _coherent_pair inherits exactly the pairs every member agrees on.
        # That is how belief:b8991d5e79839166 ("tool_call_bash causes
        # tool_error", evidence l1:ec3384b5c665449b) came to be asserted from a
        # log line. A row that claims nothing cannot be agreed with.
        obs.append(
            Observation(
                text=f"tool `{tool}` reported an error: {snippet}",
                source_ref=f"{base_ref}#error",
                session_id=session_id,
                cause=None,
                effect=None,
                confidence=0.3,
                observed_at=str(observed_at),
                tags=("observer", "post-tool", "tool-error", f"tool:{tool}"),
            )
        )

    # Invariant: friction is never detected on this path. Tool output is not a
    # user utterance, so any match here is by construction a false positive
    # ("Stop" in TodoWrite output, "No" in a diff, "мусор" in a log line).
    # Friction lives on --session-end. Scanning tool *input* would be valid;
    # scanning the response is not.
    return obs


def _damped(confidence: float) -> float:
    return round(max(0.05, min(0.95, float(confidence) * STAGE_A_DAMPING)), 3)


# ---------------------------------------------------------------------------
# --session-end
# ---------------------------------------------------------------------------


def _entry_role(entry: dict[str, Any]) -> str:
    msg = entry.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        if isinstance(role, str) and role:
            return role.casefold()
    for key in ("role", "type"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v.casefold()
    return ""


def _entry_text(entry: dict[str, Any]) -> str:
    msg = entry.get("message")
    container: dict[str, Any] = msg if isinstance(msg, dict) else entry
    content = container.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict) and blk.get("type") in (None, "text"):
                t = blk.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    for key in ("text", "prompt"):
        v = container.get(key)
        if isinstance(v, str):
            return v
    return ""


def _has_tool_use(entry: dict[str, Any]) -> bool:
    if entry.get("tool_name") or entry.get("toolName"):
        return True
    msg = entry.get("message")
    container: dict[str, Any] = msg if isinstance(msg, dict) else entry
    content = container.get("content")
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
            for b in content
        )
    return False


def iter_transcript(path: Path, limit: int = MAX_TRANSCRIPT_LINES) -> Iterator[tuple[int, dict]]:
    """Yield ``(lineno, entry)`` for the newest ``limit`` JSONL entries.

    Malformed lines are skipped silently; a missing or unreadable file yields
    nothing. ``lineno`` is 1-based and refers to the real file offset so that
    ``source_ref`` stays stable across runs.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return
    total = len(raw_lines)
    start = max(0, total - limit)
    for idx in range(start, total):
        line = raw_lines[idx]
        if not line.strip() or len(line) > MAX_LINE_BYTES:
            continue
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(entry, dict):
            yield idx + 1, entry


def _resolve_transcript(payload: dict[str, Any], argv: list[str]) -> Path | None:
    if "--transcript" in argv:
        i = argv.index("--transcript")
        if i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
    raw = _first(payload, "transcript_path", "transcriptPath", "transcript")
    if isinstance(raw, str) and raw:
        return Path(raw).expanduser()
    return None


def _epoch_ms(value: Any) -> int | None:
    """Milliseconds since the epoch, or None when the stamp cannot be trusted.

    The transcript stamps ISO-8601 with a ``Z`` suffix while the agent store
    writes integer milliseconds. They are the same clock — measured 16 ms apart
    on the two halves of one exchange — so pairing across them is sound. Any
    stamp that will not parse returns None instead of a guess: a wrong pairing
    here would manufacture causality, which is precisely what this module is
    forbidden to do.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def agent_side(
    session_id: str | None,
    deadline: Deadline | None = None,
) -> tuple[int, list[tuple[int, str]]]:
    """The agent's half of a session: ``(message count, spoken turns)``.

    Turns are ``(epoch_ms, text)`` oldest first and carry only what the agent
    actually SAID. Reasoning blocks, tool calls and step markers are excluded on
    purpose: the question a friction signal needs answered is "what did the user
    just read", and the user reads none of those.

    Read-only, budget-aware, and fail-soft in every direction. A missing store,
    a schema that has moved, a lock, an exhausted deadline — each yields
    ``(0, [])`` so the summary degrades to its old behaviour rather than
    endangering a hook whose contract is to always exit 0.
    """
    if not session_id or (deadline is not None and deadline.expired()):
        return 0, []
    if not AGENT_STORE.exists():
        return 0, []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{AGENT_STORE}?mode=ro", uri=True, timeout=0.05)
        spoken = conn.execute(
            "SELECT m.time_created AS at, json_extract(p.data, '$.text') AS said "
            "FROM part p JOIN message m ON p.message_id = m.id "
            "WHERE p.session_id = ? "
            "AND json_extract(m.data, '$.role') = 'assistant' "
            "AND json_extract(p.data, '$.type') = 'text' "
            "ORDER BY m.time_created DESC LIMIT ?",
            (session_id, MAX_AGENT_TURNS),
        ).fetchall()
        counted = conn.execute(
            "SELECT COUNT(*) FROM message WHERE session_id = ? "
            "AND json_extract(data, '$.role') = 'assistant'",
            (session_id,),
        ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return 0, []
    finally:
        if conn is not None:
            with suppress(sqlite3.Error):
                conn.close()
    turns = [
        (int(at), said)
        for at, said in spoken
        if at is not None and isinstance(said, str) and said.strip()
    ]
    turns.reverse()
    total = int(counted[0]) if counted and counted[0] is not None else len(turns)
    return total, turns


def _turn_before(turns: list[tuple[int, str]], when_ms: int | None) -> str | None:
    """The last thing the agent said before ``when_ms``, flattened and clipped.

    None when the store gave us nothing, when the stamp did not parse, or when
    the moment precedes every turn — an unobserved cause stays unobserved.
    """
    if not turns or when_ms is None:
        return None
    said: str | None = None
    for at, text in turns:
        if at > when_ms:
            break
        said = text
    if said is None:
        return None
    return " ".join(said.split())[:AGENT_TURN_CHARS] or None


def session_end_observations(
    payload: dict[str, Any],
    transcript: Path | None,
    deadline: Deadline | None = None,
) -> list[Observation]:
    """Scan a session transcript for friction points and emit a summary row."""
    deadline = deadline or Deadline()
    session_id = _first(payload, "session_id", "sessionId", "sessionID")
    session_id = str(session_id) if session_id else None
    ref_base = transcript.name if transcript is not None else f"session:{session_id or 'unknown'}"

    obs: list[Observation] = []
    counts = {"user": 0, "assistant": 0, "tool": 0, "friction": 0, "lines": 0}
    truncated = False

    # Read the agent's own half BEFORE the scan, so every friction signal found
    # below can be paired with the turn that provoked it.
    agent_messages, agent_turns = agent_side(session_id, deadline)
    transcript_assistant = 0

    if transcript is not None:
        for lineno, entry in iter_transcript(transcript):
            if deadline.expired():
                truncated = True
                break
            counts["lines"] += 1
            role = _entry_role(entry)
            if _has_tool_use(entry):
                counts["tool"] += 1
            if role in ("assistant", "ai"):
                transcript_assistant += 1
                continue
            if role not in ("user", "human"):
                continue
            counts["user"] += 1
            text = _entry_text(entry)
            if not text.strip():
                continue
            hit = patterns.detect_friction(text)
            if hit is None:
                continue
            counts["friction"] += 1
            entry_ts = entry.get("timestamp") or entry.get("observed_at")
            # The effect is a real classification and stays. The cause used to
            # be empty: this line is only reached for an entry that IS a user
            # message, so `cause="user_message"` restated the detector's own
            # precondition, and _coherent_pair inherited it unanimously for any
            # session whose friction was homogeneous.
            #
            # What provoked the rejection was not merely unknown, it was
            # unwritten — the transcript records `user`, `tool_use` and
            # `tool_result` entries and no assistant entry at all. It is written
            # now, just not there: `agent_side` reads the same exchange from
            # opencode's own store, and the turn immediately preceding the
            # rejection is what the user was reacting to when they wrote it.
            # Still None when that store cannot be read or when the rejection
            # precedes every turn, because an unobserved cause must stay
            # unobserved rather than be filled with the nearest plausible thing.
            obs.append(
                Observation(
                    text=f"user {hit['type']}: {hit['match']}",
                    source_ref=f"{ref_base}:{lineno}",
                    session_id=session_id,
                    cause=_turn_before(agent_turns, _epoch_ms(entry_ts)),
                    effect=hit["type"],
                    confidence=_damped(hit["confidence"]),
                    observed_at=str(entry_ts) if entry_ts else _now_iso(),
                    tags=(
                        "observer",
                        "session-end",
                        "friction",
                        hit["type"],
                        hit["label"],
                        f"lang:{hit['language']}",
                    )
                    + (("frustration",) if hit["frustration"] else ()),
                )
            )

    # The store is authoritative; the transcript tally is a fallback for a host
    # that does keep assistant entries there. Preferring the store unconditionally
    # would report 0 for such a host the moment the store moved.
    counts["assistant"] = agent_messages or transcript_assistant

    summary = (
        f"session {session_id or 'unknown'} ended: {counts['user']} user messages, "
        f"{counts['assistant']} assistant messages, {counts['tool']} tool events, "
        f"{counts['friction']} friction signals"
        + (" (scan truncated by budget)" if truncated else "")
    )
    # No causal pair, for the same reason as the tool-error row above: one of
    # these is emitted per session unconditionally, so agreement is guaranteed
    # rather than observed. belief:950649f22e74f369 ("session_end causes
    # session_summary") is that tautology, promoted to ACTIVE belief state.
    obs.append(
        Observation(
            text=summary,
            source_ref=f"{ref_base}#summary",
            session_id=session_id,
            cause=None,
            effect=None,
            confidence=0.3,
            observed_at=_now_iso(),
            tags=("observer", "session-end", "session-summary"),
        )
    )
    return obs


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def record(observations: list[Observation], deadline: Deadline | None = None) -> int:
    """Write observations as L0 causal memories. Returns the number persisted.

    A locked or broken database costs the observations, never the agent turn:
    every sqlite error is swallowed and ``0`` is returned.
    """
    if not observations:
        return 0
    deadline = deadline or Deadline()
    written = 0
    try:
        with db.write_txn() as conn:
            for o in observations:
                if deadline.expired():
                    break
                db.causal_insert(
                    id=o.id,
                    text=o.text,
                    layer=L0_LAYER,
                    cause=o.cause,
                    effect=o.effect,
                    confidence=o.confidence,
                    source_ref=o.source_ref,
                    session_id=o.session_id,
                    observed_at=o.observed_at,
                    tags=o.tag_str(),
                    conn=conn,
                )
                written += 1
    except (sqlite3.Error, OSError, ValueError):
        return 0
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_stdin() -> str:
    try:
        if sys.stdin is None or sys.stdin.closed:
            return ""
        return sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return ""


def run_post_tool(raw: str, deadline: Deadline) -> int:
    payload = parse_payload(raw)
    if not payload:
        return 0
    return record(post_tool_observations(payload), deadline)


def run_session_end(raw: str, argv: list[str], deadline: Deadline) -> int:
    payload = parse_payload(raw)
    transcript = _resolve_transcript(payload, argv)
    return record(session_end_observations(payload, transcript, deadline), deadline)


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    """Hook entry point. Returns 0 unconditionally — by contract."""
    deadline = Deadline()
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if "--post-tool" in args:
            raw = _read_stdin() if stdin_text is None else stdin_text
            run_post_tool(raw, deadline)
        elif "--session-end" in args:
            raw = _read_stdin() if stdin_text is None else stdin_text
            run_session_end(raw, args, deadline)
    except BaseException:  # noqa: BLE001 - a hook must never break the turn
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
