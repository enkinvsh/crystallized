# Changelog

All notable user-visible changes to Crystallized are documented in this file.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html).

Tags use `vMAJOR.MINOR.PATCH`. The maintainer creates tags and releases when changes are merged.

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

### Fixed

- Improved Keychain error handling during Claude.app token extraction.
- Removed em dashes from project prose touched during the stabilization work.

## [v1.0.0] - Retrospective initial public release

### Added

- Released the three-layer memory MCP for facts, semantic memories, and markdown documents.
- Added the OwnVoice hook for injecting local self-notes into opencode sessions.
- Added Claude.app token extraction for local authentication setup.
- Added a one-command installer for the default local Crystallized setup.
