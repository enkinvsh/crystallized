# Changelog

All notable user-visible changes to Crystallized are documented in this file.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

Tags use `vMAJOR.MINOR.PATCH`. The maintainer creates tags and releases when changes are merged.

## [v2.0.0] - Unreleased

Crystallized becomes a self-contained, self-evolving memory engine. The Redis dependency is gone, the fact layer moved to SQLite, and the agent now observes its own work and consolidates it overnight.

**This is a breaking release.** Re-run `./install.sh` after pulling. The installer backs up `opencode.json` and `~/.claude/settings.json` before merging, so existing configuration is preserved.

### Removed

- **Removed Redis entirely.** The installer no longer installs Redis via Homebrew, `apt`, `dnf`, or `yum`, and no longer starts a service through `brew services` or `systemctl`. There is no broker to install, run, or keep alive.
- Removed the `REDIS_URL`, `REDIS_HOST`, `REDIS_PORT`, and `REDIS_DB` environment variables. Nothing reads them anymore.
- Removed the documented `jq` prerequisite, which the installer never actually checked for and never used.

### Added

- Added a SQLite storage layer (`memory/db.py`) running in WAL mode with a `PRAGMA user_version` migration runner, so future schema changes apply in order instead of being silently skipped.
- Added `causal_memories`, a layered episodic trace that promotes raw signal into episodes, patterns, and principles.
- Added `belief_state`, a bi-temporal belief table with `valid_from` / `valid_to` / `recorded_at` and a partial unique index that allows exactly one active belief per subject and predicate. Superseded beliefs are marked, never deleted.
- Added `memory/observer.py`, a hook-side capture path bound to `PostToolUse` and `Stop`. It runs under a millisecond budget, always exits `0`, never prints on the tool path, and is idempotent by content hash so re-scanning a transcript is a no-op.
- Added `memory/dream.py`, the nightly consolidation pass that promotes observations upward and resolves contradictions.
- Added `config/claude-settings.json`, the hook template the installer merges into `~/.claude/settings.json`.
- Added `config/com.crystallized.dream.plist`, the launchd agent definition the installer renders into place. It is the single source of truth for the nightly schedule and IO priority.
- Added installer flags `--dry-run`, `--no-hooks`, `--no-daemon`, and `--offline`, plus `--dry-run` for the uninstaller.
- Added `.crystallized-manifest`, written at install time to record exactly which files were deployed.
- Added TTL and expiry support to facts, along with `get_fact`, prefix-filtered `list_facts`, project-scoped `memory_context`, and chunked `read_doc` for large values.

### Changed

- **The installer now registers hooks.** Previously `memory-inject.py` and `own-voice.py` were copied into place but never wired to anything, so they never ran. `install.sh` now merges hook entries into `~/.claude/settings.json`, creating the file when absent and backing it up when present. Only the `hooks` tree is modified: foreign top-level keys and foreign hook commands are preserved, and re-running the installer does not create duplicates.
- The installer now initializes the SQLite schema explicitly and reports the resulting `user_version`.
- The installer now installs a nightly consolidation job: a launchd agent at `~/Library/LaunchAgents/com.crystallized.dream.plist` on macOS, or a marked crontab entry on Linux, running daily at 04:00.
- The installer now merges the `plugin` array in `opencode.json` in addition to the `mcp` section, without duplicating entries you already have.
- Token extraction now runs through the deployed virtualenv, which already carries `cryptography`. The installer no longer runs `pip3 install --break-system-packages` against your system Python.
- A missing Claude.app is now a warning with manual instructions instead of a hard installer failure.
- `--help` output renders its ANSI styling correctly instead of printing literal `\033[1m` escape sequences.
- The in-process ChromaDB API is disabled by default because it was observed terminating the Python process without raising. Reads fall back to a read-only SQLite path against ChromaDB's own file. Set `OPENCODE_MEMORY_DISABLE_CHROMA_API=0` to re-enable it.
- Documented the real prerequisites in both READMEs: Python 3.11+, git, and curl.

### Fixed

- **`uninstall.sh` now removes the SQLite database completely**, including the `memory.db-wal` and `memory.db-shm` siblings. Deleting `memory.db` alone left a write-ahead log that a later install would replay.
- `uninstall.sh` now unloads and deletes the launchd agent, or removes the marked cron entry, instead of leaving a scheduled job pointing at deleted files.
- `uninstall.sh` now unregisters Crystallized hooks from `~/.claude/settings.json`, identifying them by path and leaving every foreign hook and every foreign key untouched. Events left empty are pruned.
- `uninstall.sh` now removes only the files listed in the install manifest. A previous glob-based approach would have deleted unrelated user scripts kept in the same directory.
- Fixed a latent migration bug inherited from the runtime prototype, where the schema hardcoded `PRAGMA user_version = 1` and re-ran on every connection, leaving the version pinned and any future migration dead on arrival.

## [v1.1.0] - Unreleased

Stabilization release prepared for maintainer tagging after merge.

### Added

- Added `SECURITY.md` with supported versions, threat model, secret handling, network surface, and reporting guidance.
- Added `memory/uv.lock` so memory service installs can be reproduced from the committed dependency graph.
- Added `uninstall.sh` for removing installed Crystallized files without touching user memory data.
- Added this changelog to track user-visible changes by release.
- Added journal note templates under `templates/notes/journal/`.
- Added pytest coverage for memory facts, semantic memory, documents, decay math, hooks, and auth token extraction.
- Added GitHub Actions CI for linting and tests.
- Added and configured environment variables for memory and Redis paths: `REDIS_URL`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `OPENCODE_MEMORY_SOCKET`, `OPENCODE_MEMORY_NOTES_DIR`, and `OPENCODE_MEMORY_CHROMA_DIR`.

### Changed

- Documented installer caveats in `README.md` and `README.ru.md`, including Keychain prompts, Linux manual token extraction, ChromaDB first-run cost, and the single-user Mac assumption.
- Pinned the opencode plugin configuration so installs use the intended plugin source.
- Made the installer back up an existing `opencode.json` before writing Crystallized configuration.
- Clarified installer output when Redis is already running instead of treating an existing instance as a problem.
- Trimmed long fact values in `memory_context` to single-line previews so the snapshot stays compact.
- Summarized documents in `memory_context` one line per folder with a name teaser instead of listing every document.

### Fixed

- Improved Keychain error handling during Claude.app token extraction.
- Removed em dashes from project prose touched during the stabilization work.

## [v1.0.0] - Retrospective initial public release

### Added

- Released the three-layer memory MCP for facts, semantic memories, and markdown documents.
- Added the OwnVoice hook for injecting local self-notes into opencode sessions.
- Added Claude.app token extraction for local authentication setup.
- Added a one-command installer for the default local Crystallized setup.
