"""Pre-prompt memory injection hook."""

import json
import os
import subprocess
import sys
from pathlib import Path

MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "memory-inject.py"


def test_hook_runs_without_socket(temp_memory_root, fake_redis):
    """No /tmp/opencode-memory-query.sock -> hook should still exit 0."""
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(Path(temp_memory_root) / "notes"),
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
    # Output should contain a [Memory] block or be empty (graceful fallback).
    assert "[Memory]" in result.stdout or result.stdout.strip() == ""


def test_hook_handles_empty_prompt(temp_memory_root):
    env = {
        **os.environ,
        "OPENCODE_MEMORY_NOTES_DIR": str(Path(temp_memory_root) / "notes"),
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
