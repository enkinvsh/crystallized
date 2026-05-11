#!/usr/bin/env python3
"""
OwnVoice hook — injects agent identity into every prompt.

Reads from ~/.config/opencode/memory/notes/self/:
- beliefs.md — agent's evolving beliefs
- focus.md — current focus / priorities
- observations.md — strengths and weaknesses watchlist

Also reads the latest journal entry for open questions.

Output format: [OwnVoice] block injected before the prompt.
"""

import os
from pathlib import Path

NOTES_DIR = Path(
    os.environ.get(
        "OPENCODE_MEMORY_NOTES_DIR",
        str(Path.home() / ".config" / "opencode" / "memory" / "notes"),
    )
)
SELF_DIR = NOTES_DIR / "self"
JOURNAL_DIR = NOTES_DIR / "journal"


def read_soul() -> str:
    beliefs_file = SELF_DIR / "beliefs.md"
    if not beliefs_file.exists():
        return ""
    return beliefs_file.read_text().strip()


def read_focus() -> str:
    focus_file = SELF_DIR / "focus.md"
    if not focus_file.exists():
        return ""
    return focus_file.read_text().strip()


def get_open_question() -> str | None:
    if not JOURNAL_DIR.exists():
        return None

    entries = sorted(JOURNAL_DIR.glob("*.md"), reverse=True)
    if not entries:
        return None

    content = entries[0].read_text()
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## open question"):
            for next_line in lines[i + 1 :]:
                stripped = next_line.strip()
                if stripped and not stripped.startswith("#"):
                    q_idx = stripped.find("?")
                    if q_idx > 0:
                        stripped = stripped[: q_idx + 1]
                    return stripped[:120]
            break

    for line in reversed(lines):
        if "?" in line and len(line.strip()) > 10 and not line.strip().startswith("#"):
            text = line.strip().lstrip("- ")
            q_idx = text.find("?")
            if q_idx > 0:
                text = text[: q_idx + 1]
            return text[:120]

    return None


def read_watchlist() -> list[str]:
    obs_file = SELF_DIR / "observations.md"
    if not obs_file.exists():
        return []

    lines = obs_file.read_text().strip().splitlines()
    weaknesses = []
    in_weaknesses = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## Weaknesses"):
            in_weaknesses = True
            continue
        if stripped.startswith("## ") and in_weaknesses:
            break
        if in_weaknesses and stripped.startswith("- "):
            label = stripped.lstrip("- ").split(":")[0].strip()
            if label:
                weaknesses.append(label)

    return weaknesses[:5]


def main():
    parts = []

    soul = read_soul()
    if soul:
        parts.append(soul)

    focus = read_focus()
    if focus:
        parts.append(f"Focus: {focus}")

    question = get_open_question()
    if question:
        parts.append(f"Open question: {question}")

    watchlist = read_watchlist()
    if watchlist:
        parts.append(f"Watchlist: {', '.join(watchlist)}")

    if parts:
        print("[OwnVoice]\n" + "\n".join(parts))


if __name__ == "__main__":
    main()
