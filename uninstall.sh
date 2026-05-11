#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# crystallized - uninstall / rollback
# Idempotent. Asks before deleting user data.
# ─────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
success() { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}---- $* ----${RESET}"; }

usage() {
  cat <<EOF
${BOLD}crystallized uninstall.sh${RESET}

Removes the memory MCP server and (optionally) user memory data.
Restores backed-up opencode.json if one is found.

${BOLD}USAGE${RESET}
  ./uninstall.sh [OPTIONS]

${BOLD}OPTIONS${RESET}
  --help, -h    Show this message and exit
  --keep-data   Do not prompt; preserve all user data
  --purge       Do not prompt; remove ALL data (memory, chroma_db, notes)

${BOLD}WHAT THIS REMOVES${RESET}
  - ~/.config/opencode/memory/server.py and friends (script files)
  - The MCP entry from ~/.config/opencode/opencode.json (if present)
  - Optionally: ~/.config/opencode/memory/{chroma_db,notes,vault,identity.json}

${BOLD}WHAT THIS DOES NOT TOUCH${RESET}
  - Redis (the service stays installed and running)
  - opencode CLI binary
  - ~/.local/share/opencode/auth.json
  - Claude.app

EOF
  exit 0
}

KEEP_DATA="ask"
for arg in "$@"; do
  case "$arg" in
    --help|-h)    usage ;;
    --keep-data)  KEEP_DATA="keep" ;;
    --purge)      KEEP_DATA="purge" ;;
    *)            warn "Unknown option: $arg" ;;
  esac
done

MEMORY_DIR="$HOME/.config/opencode/memory"
CONFIG_FILE="$HOME/.config/opencode/opencode.json"

step "1. Remove memory server scripts"

if [[ -d "$MEMORY_DIR" ]]; then
  for f in server.py memory-inject.py own-voice.py pyproject.toml uv.lock .python-version README.md; do
    if [[ -e "$MEMORY_DIR/$f" ]]; then
      rm -f "$MEMORY_DIR/$f"
      info "  removed: $f"
    fi
  done
  # The .venv is generated; remove it.
  if [[ -d "$MEMORY_DIR/.venv" ]]; then
    rm -rf "$MEMORY_DIR/.venv"
    info "  removed: .venv/"
  fi
  success "Server scripts removed"
else
  info "No memory directory at $MEMORY_DIR. Nothing to remove."
fi

step "2. User data (notes, chroma_db, vault, identity)"

DATA_PATHS=(
  "$MEMORY_DIR/notes"
  "$MEMORY_DIR/chroma_db"
  "$MEMORY_DIR/vault"
  "$MEMORY_DIR/identity.json"
)

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
      for p in "${DATA_PATHS[@]}"; do
        if [[ -e "$p" ]]; then
          rm -rf "$p"
          info "  removed: $p"
        fi
      done
      success "User data purged"
      ;;
    ask|*)
      warn "About to remove user memory data:"
      for p in "${DATA_PATHS[@]}"; do
        [[ -e "$p" ]] && echo "    $p"
      done
      read -r -p "Delete this data? [y/N] " ans
      ans="${ans:-N}"
      if [[ "$ans" =~ ^[Yy]$ ]]; then
        for p in "${DATA_PATHS[@]}"; do
          if [[ -e "$p" ]]; then
            rm -rf "$p"
            info "  removed: $p"
          fi
        done
        success "User data removed"
      else
        info "Keeping user data."
      fi
      ;;
  esac
fi

step "3. opencode.json: remove memory MCP entry"

if [[ -f "$CONFIG_FILE" ]]; then
  python3 - "$CONFIG_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
mcp = cfg.get("mcp", {})
if "memory" in mcp:
    del mcp["memory"]
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2); f.write("\n")
    print("[info]  memory MCP entry removed from opencode.json")
else:
    print("[info]  no memory MCP entry to remove")
PY
else
  info "No opencode.json at $CONFIG_FILE. Skipping."
fi

step "4. Restore most recent backup (if present)"

shopt -s nullglob
backups=("${CONFIG_FILE}".bak.*)
shopt -u nullglob

if [[ "${#backups[@]}" -gt 0 ]]; then
  latest=""
  for b in "${backups[@]}"; do
    if [[ -z "$latest" || "$b" > "$latest" ]]; then latest="$b"; fi
  done
  info "Found backup: $latest"
  read -r -p "Restore this backup over the current opencode.json? [y/N] " ans
  ans="${ans:-N}"
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    cp "$latest" "$CONFIG_FILE"
    success "Restored from $latest"
  else
    info "Backup left in place; current config unchanged."
  fi
else
  info "No backups found."
fi

echo ""
success "uninstall complete"
echo -e "${YELLOW}Note:${RESET} Redis service was NOT stopped (it may be used by other apps)."
echo -e "${YELLOW}Note:${RESET} opencode CLI and auth.json were NOT touched."
echo ""
