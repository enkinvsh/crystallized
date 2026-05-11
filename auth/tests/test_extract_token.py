"""Error-path tests for ``auth/extract_token.py``.

Security constraints (per plan v1.1, task 2.9):
- Tests must NEVER touch the real macOS Keychain.
- Tests must NEVER read the real ``~/Library/Application Support/Claude/config.json``.
- Tests must NEVER print, store, or assert against real OAuth tokens.

Isolation strategy: invoke the script as a subprocess with ``HOME`` redirected to a
``tmp_path``-rooted fake home, so the module-level ``Path.home()`` computation
resolves to a directory that contains no Claude config. ``--skip-quit-check`` is
passed to avoid the interactive ``pgrep`` / stdin prompt branch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "extract_token.py"


def _isolated_env(home: Path) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C"}


def test_help_exits_zero():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"--help should exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "usage" in combined


def test_missing_claude_config_exits_nonzero(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--skip-quit-check"],
        capture_output=True,
        text=True,
        timeout=15,
        env=_isolated_env(fake_home),
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode != 0, (
        f"Missing Claude config must exit non-zero, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    combined = (result.stdout + result.stderr).lower()
    assert any(t in combined for t in ("claude", "config", "not found")), (
        f"Error must reference missing Claude config; got: "
        f"{result.stdout!r} / {result.stderr!r}"
    )

    assert "updated " not in combined
    assert "refreshtoken" not in combined
    assert "accesstoken" not in combined
