#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# crystallized v2.0 — uninstall / rollback
# Idempotent. Asks before deleting user data.
# ─────────────────────────────────────────────

# ANSI-C quoted so the escapes are real bytes: `cat <<EOF` in usage() would
# otherwise print the literal \033 sequences.
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
success() { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo -e "\n${BOLD}──── $* ────${RESET}"; }
dry()     { echo -e "${YELLOW}[dry-run]${RESET} $*"; }

usage() {
  cat <<EOF
${BOLD}crystallized uninstall.sh${RESET} (v2.0)

Removes the memory MCP server, its hooks, and the nightly consolidation job.
User memory data is only removed when you say so.

${BOLD}USAGE${RESET}
  ./uninstall.sh [OPTIONS]

${BOLD}OPTIONS${RESET}
  --help, -h    Show this message and exit
  --keep-data   Do not prompt; preserve all user data
  --purge       Do not prompt; remove ALL data (memory.db, chroma_db, notes, legacy vault)
  --dry-run     Print every action without touching the filesystem

${BOLD}WHAT THIS REMOVES${RESET}
  - ~/.config/opencode/memory/*.py and the generated .venv/
  - The memory MCP entry from ~/.config/opencode/opencode.json
  - Crystallized hooks from ~/.claude/settings.json (foreign hooks untouched)
  - The nightly consolidation job (launchd agent on macOS, cron entry on Linux)
  - Optionally: memory.db (+ -wal/-shm), chroma_db/, notes/, legacy vault/, identity.json

${BOLD}WHAT THIS DOES NOT TOUCH${RESET}
  - opencode CLI binary
  - ~/.local/share/opencode/auth.json
  - Claude.app
  - Any non-Crystallized key in opencode.json or ~/.claude/settings.json

EOF
  exit 0
}

KEEP_DATA="ask"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --help|-h)    usage ;;
    --keep-data)  KEEP_DATA="keep" ;;
    --purge)      KEEP_DATA="purge" ;;
    --dry-run)    DRY_RUN=1 ;;
    *)            die "Unknown option: $arg (try --help)" ;;
  esac
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  warn "DRY RUN: no files will be created, modified, or deleted."
fi

MEMORY_DIR="$HOME/.config/opencode/memory"
CONFIG_FILE="$HOME/.config/opencode/opencode.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
LAUNCH_AGENT_LABEL="com.crystallized.dream"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CRON_MARKER="# crystallized-dream"
MANIFEST_NAME=".crystallized-manifest"

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  die "python3 is required to edit the JSON config files."
fi

# Reading from a closed stdin returns non-zero, which under `set -e` would kill
# the script mid-uninstall. Callers treat a failed prompt as "answer no".
prompt() {
  local message="$1" varname="$2"
  if [[ ! -t 0 ]]; then
    return 1
  fi
  read -r -p "$message" "${varname?}" || return 1
  return 0
}

remove_path() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "rm -rf $path"
    return 0
  fi
  rm -rf "$path"
  info "  removed: $path"
}

# ═══════════════════════════════════════════════
step "1. Nightly consolidation job"
# ═══════════════════════════════════════════════

remove_daemon_darwin() {
  if [[ ! -e "$LAUNCH_AGENT_PLIST" ]]; then
    info "No launchd agent at $LAUNCH_AGENT_PLIST."
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "launchctl bootout gui/$(id -u)/${LAUNCH_AGENT_LABEL}"
    dry "rm -f $LAUNCH_AGENT_PLIST"
    return
  fi

  # Both unload paths are best-effort: an agent that is not loaded is not an
  # error here, it is the desired end state.
  launchctl bootout "gui/$(id -u)/${LAUNCH_AGENT_LABEL}" &>/dev/null \
    || launchctl unload -w "$LAUNCH_AGENT_PLIST" &>/dev/null \
    || true

  rm -f "$LAUNCH_AGENT_PLIST"
  success "launchd agent unloaded and removed"
}

remove_daemon_linux() {
  if ! command -v crontab &>/dev/null; then
    info "crontab is not available. Nothing to remove."
    return
  fi

  local current
  current="$(crontab -l 2>/dev/null || true)"

  if ! printf '%s\n' "$current" | grep -q -F "$CRON_MARKER"; then
    info "No crystallized cron entry found."
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "remove crontab lines matching '$CRON_MARKER'"
    return
  fi

  local filtered
  filtered="$(printf '%s\n' "$current" | grep -v -F "$CRON_MARKER" || true)"

  if [[ -z "${filtered//[$'\n't ]/}" ]]; then
    crontab -r 2>/dev/null || true
  else
    printf '%s\n' "$filtered" | crontab - || {
      warn "Could not rewrite crontab. Remove the '$CRON_MARKER' line manually."
      return
    }
  fi
  success "cron entry removed"
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  remove_daemon_darwin
else
  remove_daemon_linux
fi

# ═══════════════════════════════════════════════
step "2. Claude Code hooks (~/.claude/settings.json)"
# ═══════════════════════════════════════════════

unregister_hooks() {
  if [[ ! -f "$CLAUDE_SETTINGS" ]]; then
    info "No settings file at $CLAUDE_SETTINGS. Skipping."
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "remove crystallized hook commands from $CLAUDE_SETTINGS"
    return
  fi

  local backup
  backup="$CLAUDE_SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$CLAUDE_SETTINGS" "$backup"
  info "Backup written to: $backup"

  "$PYTHON_BIN" - "$CLAUDE_SETTINGS" "$MEMORY_DIR" <<'PY' || {
import json
import sys

dest_path, memory_dir = sys.argv[1:3]

with open(dest_path) as f:
    content = f.read().strip()

settings = json.loads(content) if content else {}
hooks = settings.get("hooks")
if not isinstance(hooks, dict):
    print("[info]  no hooks section, nothing to remove")
    raise SystemExit(0)

# A command belongs to Crystallized when it points inside the deployed memory
# directory. Anything else in the same event is somebody else's hook.
def is_ours(entry):
    return isinstance(entry, dict) and memory_dir in str(entry.get("command", ""))

removed = 0
for event in list(hooks):
    groups = hooks[event]
    if not isinstance(groups, list):
        continue
    surviving_groups = []
    for group in groups:
        if not isinstance(group, dict):
            surviving_groups.append(group)
            continue
        entries = group.get("hooks", [])
        kept = [e for e in entries if not is_ours(e)]
        removed += len(entries) - len(kept)
        if kept:
            group["hooks"] = kept
            surviving_groups.append(group)
    if surviving_groups:
        hooks[event] = surviving_groups
    else:
        del hooks[event]

if not hooks:
    del settings["hooks"]

with open(dest_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"[ok]    Removed {removed} crystallized hook command(s)" if removed
      else "[info]  no crystallized hooks found")
PY
    warn "Could not edit $CLAUDE_SETTINGS. Your original is at $backup."
    return
  }

  success "Hooks unregistered"
}

unregister_hooks

# ═══════════════════════════════════════════════
step "3. Remove memory server scripts"
# ═══════════════════════════════════════════════

# Removal is manifest-driven on purpose. The memory directory is shared with
# whatever else the user keeps there, so a *.py glob would delete their files.
# The fallback list is used only when an older install left no manifest.
FALLBACK_MODULES=(
  server.py db.py vault.py volume.py patterns.py observer.py dream.py
  memory-inject.py own-voice.py vector_store.py migrate_redis_to_sqlite.py
)

collect_installed_files() {
  REMOVE_FILES=()
  if [[ -f "$MEMORY_DIR/$MANIFEST_NAME" ]]; then
    local line
    while IFS= read -r line; do
      [[ -n "$line" ]] && REMOVE_FILES+=("$line")
    done < "$MEMORY_DIR/$MANIFEST_NAME"
    REMOVE_FILES+=("$MANIFEST_NAME")
    info "Using install manifest (${#REMOVE_FILES[@]} entries)"
  else
    REMOVE_FILES=("${FALLBACK_MODULES[@]}" pyproject.toml uv.lock .python-version README.md)
    warn "No install manifest found, falling back to the known-module list."
  fi
  REMOVE_FILES+=(dream.log)
  return 0
}

if [[ -d "$MEMORY_DIR" ]]; then
  collect_installed_files

  for f in "${REMOVE_FILES[@]}"; do
    remove_path "$MEMORY_DIR/$f"
  done

  remove_path "$MEMORY_DIR/.venv"
  remove_path "$MEMORY_DIR/__pycache__"

  success "Server scripts removed"
else
  info "No memory directory at $MEMORY_DIR. Nothing to remove."
fi

# ═══════════════════════════════════════════════
step "4. User data (memory.db, notes, chroma_db, identity)"
# ═══════════════════════════════════════════════

# The -wal and -shm siblings are part of the SQLite database. Deleting
# memory.db alone leaves a write-ahead log that a later install would replay.
DATA_PATHS=(
  "$MEMORY_DIR/memory.db"
  "$MEMORY_DIR/memory.db-wal"
  "$MEMORY_DIR/memory.db-shm"
  "$MEMORY_DIR/notes"
  "$MEMORY_DIR/chroma_db"
  "$MEMORY_DIR/vault"
  "$MEMORY_DIR/identity.json"
)

purge_data() {
  local p
  for p in "${DATA_PATHS[@]}"; do
    remove_path "$p"
  done
}

has_any_data=0
for p in "${DATA_PATHS[@]}"; do
  if [[ -e "$p" ]]; then has_any_data=1; fi
done

if [[ "$has_any_data" -eq 0 ]]; then
  info "No user data found."
else
  case "$KEEP_DATA" in
    keep)
      info "Keeping all user data (--keep-data)"
      ;;
    purge)
      purge_data
      success "User data purged"
      ;;
    ask|*)
      warn "About to remove user memory data:"
      for p in "${DATA_PATHS[@]}"; do
        [[ -e "$p" ]] && echo "    $p"
      done
      local_ans=""
      if ! prompt "Delete this data? [y/N] " local_ans; then
        info "Not running interactively, keeping user data. Use --purge to force."
        local_ans="N"
      fi
      ans="${local_ans:-N}"
      if [[ "$ans" =~ ^[Yy]$ ]]; then
        purge_data
        success "User data removed"
      else
        info "Keeping user data."
      fi
      ;;
  esac
fi

# ═══════════════════════════════════════════════
step "5. opencode.json: remove memory MCP entry"
# ═══════════════════════════════════════════════

if [[ -f "$CONFIG_FILE" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "remove the 'memory' MCP entry from $CONFIG_FILE"
  else
    "$PYTHON_BIN" - "$CONFIG_FILE" <<'PY' || warn "Could not edit $CONFIG_FILE."
import json
import sys

path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

mcp = cfg.get("mcp", {})
if "memory" in mcp:
    del mcp["memory"]
    if not mcp:
        cfg.pop("mcp", None)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("[info]  memory MCP entry removed from opencode.json")
else:
    print("[info]  no memory MCP entry to remove")
PY
  fi
else
  info "No opencode.json at $CONFIG_FILE. Skipping."
fi

# ═══════════════════════════════════════════════
step "6. Restore most recent opencode.json backup (if present)"
# ═══════════════════════════════════════════════

shopt -s nullglob
backups=("${CONFIG_FILE}".bak.*)
shopt -u nullglob

if [[ "${#backups[@]}" -gt 0 ]]; then
  latest=""
  for b in "${backups[@]}"; do
    if [[ -z "$latest" || "$b" > "$latest" ]]; then latest="$b"; fi
  done
  info "Found backup: $latest"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "offer to restore $latest over $CONFIG_FILE"
  else
    ans=""
    if ! prompt "Restore this backup over the current opencode.json? [y/N] " ans; then
      info "Not running interactively, leaving the backup in place."
      ans="N"
    fi
    ans="${ans:-N}"
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      cp "$latest" "$CONFIG_FILE"
      success "Restored from $latest"
    else
      info "Backup left in place; current config unchanged."
    fi
  fi
else
  info "No backups found."
fi

echo ""
if [[ "$DRY_RUN" -eq 1 ]]; then
  success "Dry run complete. Nothing was changed."
else
  success "uninstall complete"
fi
echo -e "${YELLOW}Note:${RESET} opencode CLI, auth.json, and Claude.app were NOT touched."
echo -e "${YELLOW}Note:${RESET} Backups of edited JSON files are kept next to the originals."
echo ""
