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


def test_decrypt_mac_safe_storage_synthetic():
    import base64
    import hashlib
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    from extract_token import decrypt_mac_safe_storage
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    password = b"mock-password-123"
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    raw_text = '{"test": "mac_payload"}'
    raw_bytes = raw_text.encode("utf-8")
    
    pad_len = 16 - (len(raw_bytes) % 16)
    padded = raw_bytes + bytes([pad_len] * pad_len)
    
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    
    blob_b64 = base64.b64encode(b"v10" + ciphertext).decode("ascii")
    decrypted = decrypt_mac_safe_storage(blob_b64, password)
    assert decrypted == raw_text


def test_decrypt_windows_safe_storage_synthetic():
    import base64
    import os
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    from extract_token import decrypt_windows_safe_storage
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    master_key = os.urandom(32)
    nonce = os.urandom(12)
    raw_text = '{"test": "win_payload"}'
    raw_bytes = raw_text.encode("utf-8")

    aesgcm = AESGCM(master_key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, raw_bytes, None)

    blob_b64 = base64.b64encode(b"v10" + nonce + ciphertext_and_tag).decode("ascii")
    decrypted = decrypt_windows_safe_storage(blob_b64, master_key)
    assert decrypted == raw_text

