#!/usr/bin/env python3
"""
Extract Anthropic OAuth tokens from Claude Desktop (macOS / Windows) and inject into opencode auth.json.

How it works:
- Claude Desktop stores OAuth tokens in its config.json under the key `oauth:tokenCache`,
  encrypted with Electron safeStorage.
- On macOS:
  safeStorage uses AES-128-CBC with PBKDF2(sha1, salt="saltysalt", iter=1003, dkLen=16).
  The master password lives in macOS Keychain under service "Claude Safe Storage".
  Format: "v10" + ciphertext (IV is 16 spaces).
- On Windows:
  safeStorage uses AES-256-GCM. The master key is stored in "Local State" under
  `os_crypt.encrypted_key` (DPAPI encrypted).
  Format: "v10" + 12-byte nonce + ciphertext + 16-byte auth tag.
- Decrypted tokens are written to opencode's auth.json (~/.local/share/opencode/auth.json),
  preserving all other configured providers.

Prerequisites:
- Make sure you are logged into the desired account in Claude Desktop.
- Fully quit Claude Desktop before running (Cmd+Q on macOS, close from tray / taskbar on Windows).
- Python package `cryptography` installed (pip install cryptography).

Usage:
    python3 extract_token.py            # interactive: lists available tokens, prompts to choose
    python3 extract_token.py --apply N  # non-interactive: apply token at index N (0-based)
    python3 extract_token.py --print    # decrypt and print tokens without modifying auth.json
"""

import argparse
import base64
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"
KEYCHAIN_SERVICE = "Claude Safe Storage"


def get_opencode_auth_path() -> Path:
    """Return path to opencode auth.json across platforms."""
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "opencode" / "auth.json"
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def get_claude_paths() -> tuple[Path, Path | None]:
    """
    Locate Claude Desktop config.json and Local State (Windows).
    Returns (config_path, local_state_path).
    """
    if IS_MACOS:
        config_path = Path.home() / "Library" / "Application Support" / "Claude" / "config.json"
        return config_path, None

    if IS_WINDOWS:
        local_appdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")

        candidates: list[Path] = []
        if local_appdata:
            la_path = Path(local_appdata)
            candidates.append(la_path / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude")
            packages_dir = la_path / "Packages"
            if packages_dir.exists():
                candidates.extend(packages_dir.glob("Claude_*/LocalCache/Roaming/Claude"))
        if appdata:
            candidates.append(Path(appdata) / "Claude")

        for candidate in candidates:
            cfg = candidate / "config.json"
            if cfg.exists():
                return cfg, candidate / "Local State"

        # Fallback to default expected path if none found
        if candidates:
            return candidates[0] / "config.json", candidates[0] / "Local State"
        fallback = Path.home() / "AppData" / "Roaming" / "Claude"
        return fallback / "config.json", fallback / "Local State"

    sys.exit(f"Unsupported operating system: {platform.system()}. Supported platforms: macOS, Windows.")


def get_mac_keychain_password() -> bytes:
    """Fetch Claude safeStorage password from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to read macOS Keychain: {result.stderr.strip()}")
    return result.stdout.strip().encode()


def decrypt_mac_safe_storage(blob_b64: str, password: bytes) -> str:
    """Decrypt Electron safeStorage v10 blob on macOS (AES-128-CBC)."""
    blob = base64.b64decode(blob_b64)
    if blob[:3] != b"v10":
        sys.exit(f"Unexpected blob prefix: {blob[:3]!r} (expected v10)")
    ciphertext = blob[3:]
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend())
    padded = cipher.decryptor().update(ciphertext) + cipher.decryptor().finalize()
    plaintext = padded[:-padded[-1]]  # PKCS7 unpad
    return plaintext.decode("utf-8")


def dpapi_unprotect_windows(blob: bytes) -> bytes:
    """Unprotect DPAPI encrypted bytes on Windows using CryptUnprotectData."""
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("ctypes.windll is not available on this platform.")

    in_blob = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    ok = windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        get_last_error = getattr(ctypes, "GetLastError", lambda: -1)
        raise OSError(f"CryptUnprotectData failed with error code: {get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        windll.kernel32.LocalFree(out_blob.pbData)


def get_windows_master_key(local_state_path: Path) -> bytes:
    """Extract and decrypt AES-256 master key from Windows Local State file."""
    if not local_state_path.exists():
        sys.exit(f"Local State file not found at: {local_state_path}")
    data = json.loads(local_state_path.read_text(encoding="utf-8-sig"))
    encrypted_key_b64 = data.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_key_b64:
        sys.exit("os_crypt.encrypted_key not found in Local State. Are you logged into Claude Desktop?")
    encrypted_key = base64.b64decode(encrypted_key_b64)
    if not encrypted_key.startswith(b"DPAPI"):
        sys.exit("Invalid Windows master key format: missing DPAPI prefix.")
    raw_dpapi_blob = encrypted_key[5:]
    return dpapi_unprotect_windows(raw_dpapi_blob)


def decrypt_windows_safe_storage(blob_b64: str, master_key: bytes) -> str:
    """Decrypt Electron safeStorage v10 blob on Windows (AES-256-GCM)."""
    blob = base64.b64decode(blob_b64)
    prefix = blob[:3]
    if prefix == b"v11":
        sys.exit("Unsupported v11 safeStorage format encountered.")
    if prefix != b"v10":
        sys.exit(f"Unexpected blob prefix: {prefix!r} (expected v10)")
    nonce = blob[3:15]
    ciphertext_and_tag = blob[15:]
    aesgcm = AESGCM(master_key)
    plaintext = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
    return plaintext.decode("utf-8")


def check_claude_quit() -> None:
    """Warn if Claude Desktop process appears to still be running."""
    if IS_MACOS:
        result = subprocess.run(["pgrep", "-fl", "MacOS/Claude"], capture_output=True, text=True)
        main = [ln for ln in result.stdout.splitlines() if "Helper" not in ln]
        if main:
            print("WARNING: Claude.app appears to still be running. Quit it (Cmd+Q) for fresh tokens.")
            print("\n".join(main))
            if input("Continue anyway? [y/N] ").strip().lower() != "y":
                sys.exit(1)
    elif IS_WINDOWS:
        result = subprocess.run(["tasklist", "/fi", "imagename eq Claude.exe"], capture_output=True, text=True)
        if "Claude.exe" in result.stdout:
            print("WARNING: Claude.exe appears to still be running. Close it completely for fresh tokens.")
            if input("Continue anyway? [y/N] ").strip().lower() != "y":
                sys.exit(1)


def load_token_cache() -> dict[str, Any]:
    """Load and decrypt Claude Desktop OAuth token cache."""
    config_path, local_state_path = get_claude_paths()
    if not config_path.exists():
        sys.exit(f"Claude config not found at {config_path}")

    cfg = json.loads(config_path.read_text(encoding="utf-8-sig"))
    blob = cfg.get("oauth:tokenCache")
    if not blob:
        sys.exit("No 'oauth:tokenCache' in Claude config. Are you logged into Claude Desktop?")

    if IS_MACOS:
        pwd = get_mac_keychain_password()
        decrypted = decrypt_mac_safe_storage(blob, pwd)
    elif IS_WINDOWS:
        if not local_state_path:
            sys.exit("Could not locate Local State path on Windows.")
        key = get_windows_master_key(local_state_path)
        decrypted = decrypt_windows_safe_storage(blob, key)
    else:
        sys.exit(f"Unsupported operating system: {platform.system()}")

    return json.loads(decrypted)


def write_opencode_auth(token: dict[str, Any]) -> None:
    """Update opencode auth.json with selected anthropic token, creating backup."""
    auth_file = get_opencode_auth_path()
    if not auth_file.exists():
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth = {}
    else:
        backup = auth_file.with_suffix(f".json.bak.{int(time.time())}")
        shutil.copy2(auth_file, backup)
        print(f"Backup created: {backup}")
        try:
            auth = json.loads(auth_file.read_text(encoding="utf-8"))
        except Exception:
            auth = {}

    auth["anthropic"] = {
        "type": "oauth",
        "refresh": token.get("refreshToken") or token.get("refresh", ""),
        "access": token.get("token") or token.get("access", ""),
        "expires": token.get("expiresAt") or token.get("expires", 0),
    }

    auth_file.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    if not IS_WINDOWS:
        try:
            os.chmod(auth_file, 0o600)
        except OSError:
            pass
    print(f"Updated auth config: {auth_file}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", type=int, metavar="N", help="Apply token at index N (0-based) without prompting")
    ap.add_argument("--print", action="store_true", help="Print decrypted cache and exit")
    ap.add_argument("--skip-quit-check", action="store_true", help="Skip running process check")
    args = ap.parse_args()

    if not args.skip_quit_check:
        check_claude_quit()

    cache = load_token_cache()
    entries = list(cache.items())

    if args.print:
        print(json.dumps(cache, indent=2))
        return

    if not entries:
        sys.exit("Token cache is empty.")

    print(f"Found {len(entries)} token(s):")
    for i, (key, tok) in enumerate(entries):
        parts = key.split(":")
        user_id = parts[0] if parts else "account"
        exp_ms = tok.get("expiresAt") or tok.get("expires") or 0
        if exp_ms:
            exp_human = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp_ms / 1000))
        else:
            exp_human = "unknown"
        print(f"  [{i}] user={user_id} expires={exp_human}")

    if args.apply is not None:
        idx = args.apply
    elif len(entries) == 1:
        idx = 0
    else:
        try:
            val = input("Pick index to apply (or Ctrl+C to abort): ").strip()
            idx = int(val)
        except (ValueError, EOFError, KeyboardInterrupt):
            sys.exit("\nAborted.")

    if idx < 0 or idx >= len(entries):
        sys.exit(f"Index {idx} out of range (0..{len(entries)-1}).")

    write_opencode_auth(entries[idx][1])
    print("\nDone. Test with: opencode run 'hi'")
    print("If you connected to the wrong workspace, re-run and pick a different index.")


if __name__ == "__main__":
    main()

