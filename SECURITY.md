# Security Policy

## Supported Versions

Security fixes land on the latest release. v2.0.0 is the supported line; v1.x is unsupported and still carries the Redis fact layer, which listened on a local TCP port.

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
- `~/.config/opencode/memory/memory.db` (plus its `-wal` and `-shm` siblings) for facts, beliefs, volumes, and the event log. This is a plain local file. It is not encrypted at rest, so it inherits the protection of your home directory and disk encryption, nothing more.
- `~/.config/opencode/memory/chroma_db/` for the ChromaDB vector store used by semantic recall.
- `~/.config/opencode/opencode.json` for the opencode CLI config. The previous file is backed up with a timestamp suffix before any changes.
- `~/.claude/settings.json` for hook registration. The file is created if absent and backed up if present. Only the `hooks` tree is modified; foreign keys and foreign hook commands are preserved.
- `~/Library/LaunchAgents/com.crystallized.dream.plist` on macOS, or a marked crontab entry on Linux, for the nightly consolidation job.
- `~/.local/share/opencode/auth.json` for the extracted OAuth tokens used by opencode.

No system-wide files are modified. The installer does not edit shell rc files or login items, and it writes exactly one launchd plist, under your own `~/Library/LaunchAgents/`.

Run `./install.sh --dry-run` to see every path the installer would touch before it touches anything.

## Code the installer schedules and executes

Two mechanisms cause Crystallized code to run outside an explicit MCP call. Both are opt-out at install time.

- **Hooks.** `install.sh` registers commands in `~/.claude/settings.json` that run on `UserPromptSubmit`, `PostToolUse`, and `Stop`. Each is an absolute path to the interpreter inside `~/.config/opencode/memory/.venv/` and a script inside the same directory. Anyone who can write to that directory can therefore run code in your agent sessions, which is the same trust boundary as your home directory. Skip this with `--no-hooks`.
- **The nightly job.** A launchd agent (macOS) or crontab entry (Linux) runs the consolidation pass daily at 04:00, logging to `~/.config/opencode/memory/dream.log`. It reads and writes only the memory database and its sibling files. Skip this with `--no-daemon`.

The observer hook reads tool results and, at session end, the session transcript, in order to extract friction signals. Everything it extracts is written to the local database; none of it leaves the machine.

## Secrets

Crystallized has no secret store. v2.0 removed the encrypted vault that v1.x shipped.

Do not put secrets in facts or documents: every layer is stored in plaintext by design, because it is searched and injected into prompts.

## Where tokens live and how to revoke

Extracted OAuth tokens live in a single file: `~/.local/share/opencode/auth.json`. The file is readable only by your user. To revoke local access, delete it:

```sh
rm ~/.local/share/opencode/auth.json
```

After deletion, opencode will fail to authenticate until you re-extract. To rotate tokens, log out of Claude.app, log back in, then run `python3 auth/extract_token.py` from this repository. The script re-reads Keychain safeStorage and writes a fresh `auth.json`. Revoking the underlying Anthropic session itself (server side) is done through your Anthropic account settings, not by this installer.

## Removing everything

`./uninstall.sh --purge` unloads and deletes the launchd agent or cron entry, unregisters the hooks, removes the MCP entry from `opencode.json`, deletes the deployed modules and virtualenv, and removes `memory.db` together with its `-wal` and `-shm` siblings, `chroma_db/`, `notes/`, `identity.json`, and any leftover `vault/` from a pre-2.0 install.

It deliberately does not touch `auth.json`. Delete that separately, as shown above, if you are revoking access.

## Network surface

During install, the script downloads from these origins:

- `astral.sh`, for the `uv` Python installer.
- `github.com`, for opencode release artifacts.
- PyPI, via `uv sync --frozen`, resolved against the committed `memory/uv.lock` so the dependency set is fixed and reviewable.
- Hugging Face, on first semantic search, to fetch the sentence-transformer model.
- Homebrew, only as a fallback when an optional extraction tool such as `unzip` is missing.

Pass `--offline` to make the installer fail rather than reach the network.

After install, **nothing listens on a network port.** v2.0 removed Redis, which in v1.x bound to `localhost:6379`. The store is a local SQLite file, ChromaDB runs in-process inside the memory server, and the document layer is plain files under `~/.config/opencode/memory/`. Hooks talk to the running server over a Unix domain socket, not TCP.

The installer, the memory server, the hooks, and the nightly job send no telemetry, no crash reports, and no usage metrics. The only outbound traffic from opencode itself is the normal Anthropic API traffic that opencode would make anyway.

## Reporting vulnerabilities

If you find a security issue, please do not file a public GitHub issue. The preferred channel is a private GitHub security advisory on this repository: use the "Report a vulnerability" button on the Security tab. If a contact email is published in the repository metadata, you may use that as a secondary channel. Please include reproduction steps, affected versions, and the smallest change that demonstrates the issue.
