# Anthropic Token Extraction Guide

This guide explains how to extract first-party Anthropic OAuth tokens from Claude Desktop and inject them directly into opencode.

## Why Use Token Extraction

When using opencode with an Anthropic subscription (such as Claude Pro or Max), standard third-party authentication flows can fail. The built-in PKCE login flow in community plugins frequently breaks when Anthropic adjusts OAuth parameters on their servers.

Anthropic also inspects client headers. Requests without valid first-party headers or tokens are routed to a separate 200 USD credit pool or rejected with HTTP 429 errors.

Extracting the token directly from Claude Desktop solves both issues:
1. Claude Desktop authenticates through official Single Sign-On (SSO) and writes valid OAuth tokens to disk.
2. The extracted token carries first-party credentials.
3. When paired with the `@ex-machina/opencode-anthropic-auth@1.8.1` plugin in opencode, outgoing requests include the required Claude Code identity headers.

```
+-------------------------------------------------------------+
| Claude Desktop (macOS / Windows)                            |
| Logs in via official SSO -> Writes encrypted token cache     |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
| auth/extract_token.py                                       |
| Decrypts safeStorage blob via Keychain (macOS) or DPAPI (Win) |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
| ~/.local/share/opencode/auth.json                           |
| Updates "anthropic" OAuth entry: access + refresh tokens    |
+------------------------------+------------------------------+
                               |
                               v
+-------------------------------------------------------------+
| opencode runtime + @ex-machina auth plugin                  |
| Sends API calls with first-party headers -> Subscription tier |
+-------------------------------------------------------------+
```

---

## Prerequisites

### 1. Claude Desktop
Install Claude Desktop and log in to your Anthropic account:
- macOS: [Claude for macOS](https://claude.ai/download)
- Windows: [Claude for Windows](https://claude.ai/download)

Verify you can start a conversation inside Claude Desktop before proceeding.

### 2. Python Dependency
The extractor script requires the `cryptography` library for AES decryption:

```bash
pip install cryptography
```

If you installed Crystallized via `./install.sh`, `cryptography` is already installed inside `~/.config/opencode/memory/.venv`.

### 3. Opencode Auth Plugin
Ensure `@ex-machina/opencode-anthropic-auth` is declared in your `opencode.json` configuration file:

- **macOS / Linux**: `~/.config/opencode/opencode.json`
- **Windows**: `%USERPROFILE%\.config\opencode\opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [
    "@ex-machina/opencode-anthropic-auth@1.8.1"
  ]
}
```

The floating tag `@ex-machina/opencode-anthropic-auth@latest` is also supported if you prefer to
track the newest release instead of pinning to `1.8.1`.

#### Anchor-Based System Prompt Sanitization

Version `1.8.1` sanitizes the system prompt using **anchors** rather than truncation. The plugin
locates known marker strings in the outgoing prompt and removes only the client-identifying
preamble attached to them, leaving everything else byte-for-byte untouched.

The practical guarantee: **100% of user instructions are preserved.**

- Custom system prompts survive intact.
- Style guides survive intact.
- `AGENTS.md` content survives intact.

Earlier truncation-based approaches could silently drop the tail of a long prompt, so instructions
placed late in `AGENTS.md` never reached the model. Anchor-based sanitization removes that failure
mode: nothing outside the identified preamble is rewritten, reordered, or dropped.

---

## macOS Instructions

### How Decryption Works on macOS
1. Claude.app stores token data in `~/Library/Application Support/Claude/config.json` under `oauth:tokenCache`.
2. The data is encrypted using Electron `safeStorage` (AES-128-CBC, PBKDF2 with SHA-1, salt `saltysalt`, 1003 iterations).
3. The encryption password is stored in the macOS Keychain under the generic password service `Claude Safe Storage`.

### Step-by-Step Procedure

1. **Log in to Claude.app**:
   Make sure you are logged into your target account and workspace.

2. **Quit Claude.app Completely**:
   Press `Cmd+Q`. Closing the window is not enough because Electron flushes `config.json` only on complete application shutdown.

3. **Run the Extractor**:
   From the Crystallized repository root:

   ```bash
   python3 auth/extract_token.py
   ```

4. **Keychain Access Prompt**:
   macOS will prompt for Keychain access. Click **Always Allow** so the script can read the encryption key without repeated prompts.

5. **Select Your Workspace**:
   If your account has multiple workspaces, the script lists each one:
   ```text
   Found 2 token(s):
     [0] user=user_example123 expires=2027-08-20 14:30
     [1] user=user_example456 expires=2027-08-20 14:30
   Pick index to apply (or Ctrl+C to abort): 0
   ```
   Type the desired index number and press Enter.

6. **Verify the Setup**:
   ```bash
   opencode run "hello"
   ```

---

## Windows Instructions

### How Decryption Works on Windows
Windows uses a two-layer encryption scheme:
1. **Master Key**: The Electron master key is stored in `Local State` under `os_crypt.encrypted_key`. It is encrypted with Windows DPAPI (`CryptProtectData`).
2. **Payload**: The `oauth:tokenCache` string in `config.json` is encrypted with AES-256-GCM using the decrypted 32-byte master key.
3. **File Paths**: Claude Desktop on Windows is distributed as an MSIX package. Files reside in `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`.

### Step-by-Step Procedure

1. **Log in to Claude Desktop for Windows**:
   Confirm you can send a message in the desktop client.

2. **Exit Claude Desktop Completely**:
   Right-click the Claude icon in the system tray and select **Quit**, or terminate `Claude.exe` via Task Manager.

3. **Run the Extractor**:
   Open PowerShell or Command Prompt:

   ```powershell
   python auth\extract_token.py
   ```

4. **Select Workspace Index**:
   Choose the workspace index corresponding to your subscription.

5. **Test in Opencode**:
   ```powershell
   opencode run "hello"
   ```

---

## Command Line Flags

The `extract_token.py` script supports several flags for automated workflows:

| Flag | Description |
|---|---|
| `--print` | Decrypts and prints the JSON cache to stdout without modifying `auth.json`. |
| `--apply N` | Applies token index `N` directly without interactive prompts. |
| `--skip-quit-check` | Bypasses the active process check. |

Examples:

```bash
# Inspect decrypted tokens without applying
python3 auth/extract_token.py --print

# Apply token 0 non-interactively
python3 auth/extract_token.py --apply 0 --skip-quit-check
```

---

## What Happens to auth.json

When a token is applied:
1. A timestamped backup is created at `~/.local/share/opencode/auth.json.bak.<timestamp>`.
2. Only the `anthropic` key in `auth.json` is modified. Other providers (Google, OpenAI, custom endpoints) remain intact.
3. On Unix systems, file permissions are set to `0600` (`chmod 600`).

Sample structure written to `auth.json`:

```json
{
  "anthropic": {
    "type": "oauth",
    "refresh": "sk-ant-ort01-generic-refresh-token-placeholder",
    "access": "sk-ant-oat01-generic-access-token-placeholder",
    "expires": 1787328000000
  }
}
```

---

## Troubleshooting

### HTTP 429 Too Many Requests
- Cause: The `@ex-machina/opencode-anthropic-auth` plugin is missing or inactive.
- Fix: Add the plugin to your `opencode.json` configuration and launch opencode once so it installs the dependency.

### InvalidTag / Decryption Error
- Cause: The master key could not decrypt the ciphertext. This usually happens when Claude was running while files were being read.
- Fix: Fully quit Claude Desktop, verify no background processes remain, and re-run the extractor.

### Token Expiration
- Claude OAuth tokens have an expiration timestamp in `expiresAt` (milliseconds). Opencode automatically uses the `refresh` token to renew the session when needed.
- If tokens expire completely, simply open Claude Desktop, log in, quit the app, and run the extractor script again.

### Rolling Back
To restore a previous authentication state:

```bash
cd ~/.local/share/opencode
# Find the latest backup
ls -lt auth.json.bak.*
# Restore
cp auth.json.bak.<TIMESTAMP> auth.json
```
