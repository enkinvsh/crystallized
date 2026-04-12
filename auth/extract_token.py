#!/usr/bin/env python3
"""
Extract Anthropic OAuth tokens from Claude.app (macOS) and inject into opencode auth.json.

How it works:
- Claude.app stores OAuth tokens in `~/Library/Application Support/Claude/config.json`
  under the key `oauth:tokenCache`, encrypted with Electron's safeStorage.
- On macOS safeStorage uses AES-128-CBC with PBKDF2(sha1, salt="saltysalt",
  iter=1003, dkLen=16). The password is stored in macOS Keychain under the
  generic-password service "Claude Safe Storage". IV is 16 spaces. Format: "v10" + ciphertext.
- We read the keychain entry (one-time TouchID/password prompt — pick "Always Allow"
  to skip future prompts), decrypt the cache, and rewrite opencode's auth.json
  with the chosen token.

Prereq:
- Quit Claude.app fully (Cmd+Q) BEFORE running. Electron only flushes config.json on quit.
- Make sure you're logged into the desired account in Claude.app first.
- `pip install cryptography` (or system has it).

Usage:
    python3 extract_token.py            # interactive: lists tokens, you pick one
    python3 extract_token.py --apply N  # non-interactive: apply token #N
    python3 extract_token.py --print    # just decrypt and print, don't touch auth.json
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

CLAUDE_CONFIG = Path.home() / "Library/Application Support/Claude/config.json"
OPENCODE_AUTH = Path.home() / ".local/share/opencode/auth.json"
KEYCHAIN_SERVICE = "Claude Safe Storage"


def get_keychain_password() -> bytes:
    """Fetch the Claude safeStorage password from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to read Keychain: {result.stderr.strip()}")
    return result.stdout.strip().encode()


def decrypt_safe_storage(blob_b64: str, password: bytes) -> str:
    """Decrypt an Electron safeStorage v10 blob (macOS variant)."""
    blob = base64.b64decode(blob_b64)
    if blob[:3] != b"v10":
        sys.exit(f"Unexpected blob prefix: {blob[:3]!r} (expected v10)")
    ciphertext = blob[3:]
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16), backend=default_backend())
    padded = cipher.decryptor().update(ciphertext) + cipher.decryptor().finalize()
    plaintext = padded[: -padded[-1]]  # PKCS7 unpad
    return plaintext.decode()


def check_claude_quit():
    """Warn if Claude.app main process is still running (config.json may be stale)."""
    result = subprocess.run(["pgrep", "-fl", "MacOS/Claude"], capture_output=True, text=True)
    main = [ln for ln in result.stdout.splitlines() if "Helper" not in ln]
    if main:
        print("WARNING: Claude.app appears to still be running. Quit it (Cmd+Q) for fresh tokens.")
        print("\n".join(main))
        if input("Continue anyway? [y/N] ").strip().lower() != "y":
            sys.exit(1)


def load_token_cache() -> dict:
    if not CLAUDE_CONFIG.exists():
        sys.exit(f"Claude config not found at {CLAUDE_CONFIG}")
    cfg = json.loads(CLAUDE_CONFIG.read_text())
    blob = cfg.get("oauth:tokenCache")
    if not blob:
        sys.exit("No 'oauth:tokenCache' in Claude config — are you logged in?")
    pwd = get_keychain_password()
    decrypted = decrypt_safe_storage(blob, pwd)
    return json.loads(decrypted)


def write_opencode_auth(token: dict):
    """Update opencode's anthropic entry, keeping a timestamped backup."""
    if not OPENCODE_AUTH.exists():
        sys.exit(f"opencode auth not found at {OPENCODE_AUTH}")
    backup = OPENCODE_AUTH.with_suffix(f".json.bak.{int(time.time())}")
    shutil.copy2(OPENCODE_AUTH, backup)
    auth = json.loads(OPENCODE_AUTH.read_text())
    auth["anthropic"] = {
        "type": "oauth",
        "refresh": token["refreshToken"],
        "access": token["token"],
        "expires": token["expiresAt"],
    }
    OPENCODE_AUTH.write_text(json.dumps(auth, indent=2))
    os.chmod(OPENCODE_AUTH, 0o600)
    print(f"Updated {OPENCODE_AUTH}")
    print(f"Backup:  {backup}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", type=int, metavar="N", help="Apply token at index N (0-based) without prompting")
    ap.add_argument("--print", action="store_true", help="Print decrypted cache and exit")
    ap.add_argument("--skip-quit-check", action="store_true")
    args = ap.parse_args()

    if not args.skip_quit_check:
        check_claude_quit()

    cache = load_token_cache()
    entries = list(cache.items())

    if args.print:
        print(json.dumps(cache, indent=2))
        return

    print(f"Found {len(entries)} token(s):")
    for i, (key, tok) in enumerate(entries):
        # key format: userId:installId:apiUrl:scopes
        parts = key.split(":")
        user_id = parts[0] if parts else "?"
        exp_human = time.strftime("%Y-%m-%d %H:%M", time.localtime(tok["expiresAt"] / 1000))
        print(f"  [{i}] user={user_id} expires={exp_human}")

    if args.apply is not None:
        idx = args.apply
    elif len(entries) == 1:
        idx = 0
    else:
        idx = int(input("Pick index to apply (or Ctrl+C to abort): "))

    write_opencode_auth(entries[idx][1])
    print("\nDone. Test with: opencode run 'hi'")
    print("If wrong workspace, re-run and pick a different index.")


if __name__ == "__main__":
    main()
