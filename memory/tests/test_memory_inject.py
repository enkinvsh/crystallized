"""Pre-prompt memory injection hook tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "memory-inject.py"


def test_hook_runs_without_socket(tmp_path):
    """No /tmp/opencode-memory-query.sock -> hook should still exit 0."""
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "memory.db"
    env = {
        **os.environ,
        "OPENCODE_MEMORY_DB": str(db_path),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": "what does Oen think of dark mode"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    # Output should contain a [Clock] / [Memory] block or be empty (graceful fallback).
    assert "[Clock]" in result.stdout or "[Memory]" in result.stdout or result.stdout.strip() == ""


def test_hook_handles_empty_prompt(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "memory.db"
    env = {
        **os.environ,
        "OPENCODE_MEMORY_DB": str(db_path),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
        "OPENCODE_MEMORY_SOCKET": "/tmp/nonexistent-socket-xyz.sock",
    }
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": ""}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
