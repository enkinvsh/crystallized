# crystallized v1.1 stabilization design

**Date:** 2026-05-11
**Author:** Sisyphus-Junior (under Oen's direction)
**Status:** Approved (Approach B). Implementation pending.

## 1. Summary

v1.1 is a stabilization release for the existing public product. It is not a rewrite. It does not migrate the memory layer to the upstream `agent-memory-mcp` server. It hardens the things a serious user expects from a one-command installer that touches Keychain, Redis, MCP, and OAuth tokens: reproducible installs, documented security model, basic tests, CI, configurability, and a clean release.

The product thesis stays the same:

- Portable three-layer memory for opencode (Redis facts, ChromaDB semantic, filesystem docs).
- Crystallizing agent identity (`own-voice.py` plus templates).
- First-party Anthropic auth via Claude.app token extraction.

## 2. Goals

1. Make the installer reproducible across machines and Python patch versions.
2. Document the security and privacy posture so users know what they are accepting before running `./install.sh`.
3. Cover the critical paths with tests so refactors do not silently break the product.
4. Add CI on push/PR so regressions surface before users see them.
5. Finish operational gaps: configurable Redis/socket, seeded `journal/` directory, plugin version pinning, uninstall/rollback path.
6. Prepare the repo for tagged releases: `CHANGELOG.md`, version bump policy, release notes.

## 3. Non-goals

- No rewrite of `server.py`, `memory-inject.py`, or `own-voice.py` behaviour.
- No migration to the upstream `agent-memory-mcp` server. That is a v2 conversation, owned by Oen.
- No new memory features, no new identity features, no UI.
- No changes to the OAuth extraction approach beyond hardening and docs.
- No Windows support.
- No commits, pushes, releases, tags, or remote writes from this work unless Oen explicitly asks for them.
- No writes to any external third-party repository.

## 4. Current state (as discovered)

- No tests, no CI, no tagged releases.
- `oh-my-openagent@latest` pinned by floating tag in `config/opencode.json`.
- `uv.lock` is gitignored, so dependency resolution is not reproducible.
- No `SECURITY.md`, no threat model, no token handling notes.
- Hard-coded Redis host/port and ChromaDB socket assumptions inside `server.py` (to be confirmed during implementation).
- No `journal/` seed; `notes/` templates exist.
- No `uninstall.sh` and no rollback path for `~/.config/opencode/opencode.json` merges.
- README is good marketing prose, but lacks honest caveats (Keychain prompts, third-party-app detection edge cases, Linux limits, data locations, what the installer writes).
- Baseline `py_compile` on the worktree passes.

## 5. Approach

Approach B (approved): professional stabilization release. Touch the seams, not the core. Each change is small, reviewable, and reversible.

Implementation will be ordered into four phases. Each phase is independently shippable and leaves the repo in a working state.

### Phase 1. Safety and reproducibility

Goal: a user who clones today gets the same install as a user who clones in three months.

- Pin `oh-my-openagent` plugin to a specific version in `config/opencode.json`. Document upgrade procedure.
- Commit `uv.lock` (remove from `.gitignore`). Add a short note in README about how to regenerate.
- Add `SECURITY.md` covering:
  - What the installer reads (Claude.app safeStorage via Keychain).
  - What it writes (`~/.config/opencode/`, `~/.local/share/opencode/auth.json`, Redis on localhost).
  - Where tokens live and how to revoke them.
  - Reporting channel for vulnerabilities.
- Add README "What the installer does" and "What it does not do" sections. Honest caveats: Keychain prompts, third-party-app detection edge cases, Linux limits.
- Confirm `.gitignore` excludes all generated state. Add an explicit allowlist comment.

### Phase 2. Tests and CI

Goal: catch regressions before users do.

- Add `pytest` to dev dependencies.
- Unit tests for `memory/server.py` MCP tool surface (fact save/get, semantic remember/search, doc save/read, decay math). Use a fakeredis or in-memory shim where Redis is unavailable; mock ChromaDB if it cannot be brought up in CI cheaply.
- Smoke test for `memory-inject.py` and `own-voice.py`: given a stub MCP, the pre-prompt hook returns a deterministic injection.
- Unit tests for `auth/extract_token.py` that do not need real Keychain: parse fixtures, error paths.
- Static checks: `ruff` (lint + format) and `pyright` or `mypy` in lenient mode.
- GitHub Actions workflow: lint, type-check, pytest on Linux + macOS, Python 3.11, 3.12, 3.13. No secrets needed.

### Phase 3. Product finish

Goal: close the operational gaps users hit in week two.

- Configurable Redis: read host, port, socket, DB from env vars with documented defaults. No behaviour change for default users.
- Configurable ChromaDB path: env var override; default unchanged.
- Seed `templates/journal/` (or equivalent) so `journal/` is part of the identity scaffold, not a surprise directory the agent invents.
- `uninstall.sh`: removes deployed files in `~/.config/opencode/memory/`, optionally restores backed-up `opencode.json`, stops Redis service started by us, prints what it did. Idempotent. Asks before deleting user data.
- Installer: write a timestamped backup of any pre-existing `~/.config/opencode/opencode.json` before merging. Print the backup path.
- Installer: detect already-running Redis owned by another process and warn instead of starting a duplicate.

### Phase 4. Release readiness

Goal: turn the repo into a releasable artifact.

- `CHANGELOG.md` seeded with v1.0 (initial public release) and v1.1 entries.
- Version bump in `memory/pyproject.toml` to `1.1.0` once Phase 1 to 3 land.
- README: link to changelog, document upgrade path from v1.0.
- Decide tag scheme (`v1.1.0`). Document in `CHANGELOG.md` header.
- Do not create the tag or push it in this work. That is Oen's call.

## 6. Architecture impact

None at the layer boundaries. The MCP server still exposes the same tool surface. The pre-prompt hooks still produce `[Memory]` and `[OwnVoice]` blocks. `auth/extract_token.py` still writes the same `auth.json`. Changes are limited to:

- Config surface (new env vars, all with defaults that preserve current behaviour).
- Build surface (lockfile committed, plugin pinned).
- Repo surface (new docs, tests, CI, uninstall).

If implementation discovers a real architecture issue, it is raised to Oen, not silently refactored.

## 7. Security posture

- Tokens stay where they are today: Claude.app safeStorage, then `~/.local/share/opencode/auth.json`. No new storage.
- `SECURITY.md` makes the trust model explicit: the installer assumes the local user controls the Mac, Claude.app, and the shell session.
- No telemetry, no network calls beyond what the installer already does (Homebrew, uv, opencode release download).
- Memory data (Redis, ChromaDB, notes, journal) is local-only. Documented.
- Tests must not embed real tokens, real Keychain output, or real user memory. Fixtures only.

## 8. Reproducibility

- `uv.lock` committed. `uv sync` produces the same environment for the same Python minor version.
- Plugin pinned. `opencode` version is whatever the installer downloads at install time; documented as a known floating point with a recommended pin command.
- CI exercises the lockfile on a clean runner.

## 9. Tests and CI

- Local: `uv run pytest`, `uv run ruff check`, `uv run pyright` (or `mypy`).
- CI: same commands, matrix on OS and Python version.
- Coverage target: not enforced numerically in v1.1. Each tool in `server.py` and each error path in `extract_token.py` has at least one test.
- TDD on all new code introduced by this stabilization. Existing code gets characterization tests where touched.

## 10. Installer and runtime finishing

- Backup before merge (Phase 3).
- Detect existing Redis (Phase 3).
- `uninstall.sh` (Phase 3).
- Configurable Redis/ChromaDB (Phase 3).
- Better error messages on Keychain unlock failure (Phase 3).
- README updates so the install story matches reality (Phase 1).

## 11. Release readiness

- `CHANGELOG.md` describes user-visible changes only.
- Version bumps live in `memory/pyproject.toml` and (if added) a top-level `VERSION` file. Single source of truth.
- README documents upgrade from v1.0 (run `./install.sh` again; backups created automatically).

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| CI cannot run ChromaDB cheaply | Mock the persistent client; integration test gated by env var, off by default. |
| Pinning the plugin breaks users on `latest` | Document the upgrade command in README. Pinning is the safer default. |
| `uv.lock` commit causes churn from different Python patch versions | CI matrix covers 3.11 to 3.13. Document Python version policy in README. |
| Backup-before-merge logic mishandles an exotic config | Backup is timestamped and never deleted. User can restore manually. |
| Uninstall script confuses users sharing the machine | Script asks before deleting any non-empty data directory. Defaults to "no". |
| Scope creep toward v2 memory migration | This document is the contract. Anything outside Phases 1 to 4 is rejected and tracked in a separate v2 doc. |

## 13. Out of scope (deferred to v2)

- Migration to upstream `agent-memory-mcp`.
- Windows support.
- Network-attached Redis or ChromaDB.
- Multi-user installs.
- Plugin alternatives to `oh-my-openagent`.
- A first-class TUI for memory inspection.

## 14. Acceptance criteria for v1.1

- `uv.lock` committed; plugin pinned; `SECURITY.md` present; README has honest install/caveats sections.
- `pytest` runs locally and in CI on Linux and macOS across Python 3.11 to 3.13.
- `ruff` and type checks pass in CI.
- Installer creates a backup before merging `opencode.json`.
- `uninstall.sh` exists, is idempotent, and asks before touching user data.
- Redis and ChromaDB locations are configurable via documented env vars; defaults unchanged.
- `CHANGELOG.md` describes v1.1 changes.
- `memory/pyproject.toml` version is `1.1.0`.
- No commits to external repositories. No remote writes from this work.

## 15. Handoff

Next step: invoke the `writing-plans` skill to produce `docs/plans/2026-05-11-crystallized-v1.1-stabilization.md` with bite-sized TDD tasks ordered by the four phases above. Atlas reads this design first, verifies status, then proceeds.
