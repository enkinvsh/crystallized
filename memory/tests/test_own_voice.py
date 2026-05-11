"""OwnVoice hook: identity injection.

Runs `own-voice.py` as a subprocess to verify two contracts:

1. Missing notes/self/ -> exit 0 with empty or minimal output (graceful no-op).
2. beliefs.md + focus.md present -> stdout carries an [OwnVoice] block.

`own-voice.py` honors `OPENCODE_MEMORY_NOTES_DIR` (Task 3.4) and falls back to
`Path.home() / ".config" / "opencode" / "memory" / "notes"` when unset. To keep
these tests deterministic AND avoid reading or mutating live notes under the
real $HOME, we set `OPENCODE_MEMORY_NOTES_DIR` to `tmp_path` and ALSO override
`HOME` to `tmp_path` as belt-and-suspenders subprocess isolation. The env var
alone is sufficient now; the HOME override is redundant but harmless.
"""

import os
import subprocess
import sys
from pathlib import Path

MEMORY_DIR = Path(__file__).parent.parent
HOOK = MEMORY_DIR / "own-voice.py"


def _hook_env(tmp_path: Path, notes_dir: Path) -> dict[str, str]:
    """Subprocess env that points at tmp_path for both code paths.

    - `OPENCODE_MEMORY_NOTES_DIR` is the active contract (Task 3.4) and
      alone suffices to redirect `NOTES_DIR`.
    - `HOME` override redirects `Path.home()` to `tmp_path` as redundant
      belt-and-suspenders isolation: should any code path ever fall back to
      `Path.home() / ".config" / "opencode" / "memory" / "notes"`, it still
      lands under `tmp_path` and never touches real notes.
    """
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "OPENCODE_MEMORY_NOTES_DIR": str(notes_dir),
    }


def test_own_voice_missing_notes_exits_clean(tmp_path):
    """No notes/self/ -> hook should exit 0 with empty or minimal output."""
    notes_dir = tmp_path / "notes"
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=_hook_env(tmp_path, notes_dir),
        timeout=5,
    )
    assert result.returncode == 0, (
        f"own-voice.py crashed with rc={result.returncode}; "
        f"stderr={result.stderr!r}"
    )


def test_own_voice_emits_block_when_beliefs_exist(tmp_path):
    """beliefs.md + focus.md present -> stdout includes the [OwnVoice] block."""
    # Lay out both possible NOTES_DIR locations so the test is robust whether
    # own-voice.py reads OPENCODE_MEMORY_NOTES_DIR (current behavior, Task 3.4)
    # or falls back to Path.home() via the HOME override. Both targets live
    # under tmp_path, so no real notes are read or written.
    home_self = tmp_path / ".config" / "opencode" / "memory" / "notes" / "self"
    env_self = tmp_path / "notes" / "self"
    for self_dir in (home_self, env_self):
        self_dir.mkdir(parents=True, exist_ok=True)
        (self_dir / "beliefs.md").write_text("I am Sisyphus.")
        (self_dir / "focus.md").write_text("Stabilize v1.1.")

    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=_hook_env(tmp_path, tmp_path / "notes"),
        timeout=5,
    )
    assert result.returncode == 0, (
        f"own-voice.py crashed with rc={result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert "OwnVoice" in result.stdout or "Sisyphus" in result.stdout, (
        f"expected [OwnVoice] block or 'Sisyphus' content in stdout; "
        f"got stdout={result.stdout!r}"
    )
