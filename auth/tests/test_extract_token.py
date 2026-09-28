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


CODE_CLIENT = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DESKTOP_CLIENT = "a473d7bb-17ac-43a7-abc0-a1343d7c2805"
HOST = "https://api.anthropic.com"
INFERENCE = "user:inference user:file_upload user:profile"


def _module():
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    import extract_token

    return extract_token


def test_usable_tokens_keeps_one_longest_token_per_account_and_org():
    et = _module()
    v2 = {
        f"acct:acc-1|{CODE_CLIENT}:org-1:{HOST}:{INFERENCE} user:sessions:claude_code": {
            "token": "a1s", "refreshToken": "r1s", "expiresAt": 1000, "subscriptionType": "max",
        },
        f"acct:acc-1|{CODE_CLIENT}:org-1:{HOST}:{INFERENCE}": {
            "token": "a1", "refreshToken": "r1", "expiresAt": 2000, "subscriptionType": "max",
        },
        f"acct:acc-1|{DESKTOP_CLIENT}:org-1:{HOST}:user:profile": {"token": "d", "refreshToken": "rd", "expiresAt": 1},
        f"acct:acc-1|{CODE_CLIENT}:org-2:{HOST}:{INFERENCE}": None,
        f"acct:acc-1|{CODE_CLIENT}:org-3:{HOST}:user:profile": {"token": "p", "refreshToken": "rp", "expiresAt": 1},
    }
    v1 = {
        f"{CODE_CLIENT}:org-1:{HOST}:{INFERENCE}": {"token": "a1", "refreshToken": "r1", "expiresAt": 1},
        f"{CODE_CLIENT}:org-4:{HOST}:{INFERENCE}": {"token": "a4", "refreshToken": "r4", "expiresAt": 1},
    }

    found = et.usable_tokens({"oauth:tokenCacheV2": v2, "oauth:tokenCache": v1})

    assert [tok["refreshToken"] for _, tok in found] == ["r1", "r4"]


def _write_mac_config(tmp_path, password: bytes, caches: dict) -> Path:
    import base64
    import hashlib
    import json

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)

    def encrypt(obj) -> str:
        raw = json.dumps(obj).encode()
        pad = 16 - len(raw) % 16
        enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
        return base64.b64encode(b"v10" + enc.update(raw + bytes([pad] * pad)) + enc.finalize()).decode()

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({name: encrypt(cache) for name, cache in caches.items()}))
    return cfg


def _run_main(et, monkeypatch, cfg: Path, password: bytes, data: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(et, "IS_MACOS", True)
    monkeypatch.setattr(et, "IS_WINDOWS", False)
    monkeypatch.setattr(et, "get_claude_paths", lambda: (cfg, None))
    monkeypatch.setattr(et, "get_mac_keychain_password", lambda: password)
    monkeypatch.setattr(sys, "argv", ["extract_token.py", "--skip-quit-check"])
    et.main()


def test_main_takes_token_from_v2_when_old_cache_is_empty(tmp_path, monkeypatch):
    import json

    et = _module()
    password = b"mock-password"
    cfg = _write_mac_config(tmp_path, password, {
        "oauth:tokenCache": {},
        "oauth:tokenCacheV2": {
            f"acct:acc-1|{CODE_CLIENT}:org-1:{HOST}:{INFERENCE}": {
                "token": "fake-access", "refreshToken": "fake-refresh", "expiresAt": 1700000000000,
            },
        },
    })
    data = tmp_path / "data"
    (data / "opencode").mkdir(parents=True)
    (data / "opencode" / "auth.json").write_text(json.dumps({"openai": {"type": "oauth"}}))

    _run_main(et, monkeypatch, cfg, password, data)

    auth = json.loads((data / "opencode" / "auth.json").read_text())
    assert auth["openai"] == {"type": "oauth"}
    assert auth["anthropic"] == {
        "type": "oauth", "refresh": "fake-refresh", "access": "fake-access", "expires": 1700000000000,
    }


def test_main_explains_missing_claude_code_token(tmp_path, monkeypatch):
    import pytest

    et = _module()
    password = b"mock-password"
    cfg = _write_mac_config(tmp_path, password, {
        "oauth:tokenCache": {},
        "oauth:tokenCacheV2": {f"acct:acc-1|{DESKTOP_CLIENT}:org-1:{HOST}:user:profile": {"token": "d"}},
    })

    with pytest.raises(SystemExit) as exc:
        _run_main(et, monkeypatch, cfg, password, tmp_path / "data")

    assert "Pro or Max" in str(exc.value.code)
    assert not (tmp_path / "data" / "opencode" / "auth.json").exists()
