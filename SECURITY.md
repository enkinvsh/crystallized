# Security Policy

## Trust Model

Crystallized assumes a single local user who owns the Mac, the Claude.app installation, the interactive shell that runs `install.sh`, and the `~/.config/opencode/` directory. Everything the installer does, every token it extracts, and every byte the memory layer stores is intended for that user. The installer is not designed for shared machines, multi-user systems, or hostile local environments. If another user can read your home directory or attach a debugger to your shell, they can read everything described below.

## What the installer reads

The installer reads from a small, predictable set of sources on the local machine:

- Claude.app safeStorage entries via the macOS Keychain. Decryption uses the standard Electron safeStorage flow and requires your login password the first time it runs. Choosing "Always Allow" in the Keychain prompt avoids future prompts.
- `~/Library/Application Support/Claude/config.json` for the encrypted OAuth blob and account metadata produced by Claude.app.
- The current shell environment to detect `PATH`, `HOME`, and the active package manager.

The installer does not read mail, browser data, SSH keys, or files outside the paths listed here.

## What the installer writes

The installer writes only to your own user-scoped paths:

- `~/.config/opencode/memory/` for the Python memory server, identity files, notepads, and the local virtualenv.
- `~/.config/opencode/opencode.json` for the opencode CLI config. The previous file is backed up with a timestamp suffix before any changes.
- `~/.local/share/opencode/auth.json` for the extracted OAuth tokens used by opencode.
- A local Redis instance bound to `localhost:6379` for the fast fact layer. Redis is started via Homebrew services on macOS and listens on loopback only.
- `~/.config/opencode/memory/chroma_db/` for the ChromaDB vector store used by semantic recall.

No system-wide files are modified. The installer does not edit shell rc files, login items, or launchd plists outside of Homebrew's own Redis service.

## Where tokens live and how to revoke

Extracted OAuth tokens live in a single file: `~/.local/share/opencode/auth.json`. The file is readable only by your user. To revoke local access, delete it:

```sh
rm ~/.local/share/opencode/auth.json
```

After deletion, opencode will fail to authenticate until you re-extract. To rotate tokens, log out of Claude.app, log back in, then run `python3 auth/extract_token.py` from this repository. The script re-reads Keychain safeStorage and writes a fresh `auth.json`. Revoking the underlying Anthropic session itself (server side) is done through your Anthropic account settings, not by this installer.

## Network surface

During install, the script downloads from three origins:

- Homebrew, for Redis and other native dependencies.
- `astral.sh`, for the `uv` Python installer.
- `github.com`, for opencode release artifacts.

After install, the memory layer is local only. Redis listens on `localhost:6379`. ChromaDB runs in-process inside the memory server. The notepad and document layers are plain files under `~/.config/opencode/memory/`. The installer and the memory server send no telemetry, no crash reports, and no usage metrics. The only outbound traffic from opencode itself is the normal Anthropic API traffic that opencode would make anyway.

## Reporting vulnerabilities

If you find a security issue, please do not file a public GitHub issue. The preferred channel is a private GitHub security advisory on this repository: use the "Report a vulnerability" button on the Security tab. If a contact email is published in the repository metadata, you may use that as a secondary channel. Please include reproduction steps, affected versions, and the smallest change that demonstrates the issue.
