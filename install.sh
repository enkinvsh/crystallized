#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# crystallized v2.0 — one-shot setup script
# opencode + oh-my-openagent + SQLite-backed MCP memory
# No Redis. No daemons other than the nightly consolidation job.
# ─────────────────────────────────────────────

# ── Colors ────────────────────────────────────
# ANSI-C quoted so the escapes are real bytes: `cat <<EOF` in usage() would
# otherwise print the literal \033 sequences.
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
RESET=$'\033[0m'

# ── Helpers ───────────────────────────────────
info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
success() { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo -e "\n${BOLD}──── $* ────${RESET}"; }
dry()     { echo -e "${YELLOW}[dry-run]${RESET} $*"; }

# ── Flags ─────────────────────────────────────
DRY_RUN=0
WITH_HOOKS=1
WITH_DAEMON=1
OFFLINE=0

usage() {
  cat <<EOF
${BOLD}crystallized install.sh${RESET} (v2.0)

One-shot setup for opencode + persistent AI memory.

${BOLD}USAGE${RESET}
  ./install.sh [OPTIONS]

${BOLD}OPTIONS${RESET}
  --help, -h    Show this message and exit
  --dry-run     Print every action without touching the filesystem
  --no-hooks    Skip Claude Code hook registration (~/.claude/settings.json)
  --no-daemon   Skip the nightly consolidation job (launchd / cron)
  --offline     Never reach the network; fail instead of downloading

${BOLD}WHAT THIS DOES${RESET}
  1. Checks prerequisites (git, python3 >= 3.11, curl)
  2. Installs uv (Python package manager) if missing
  3. Installs the opencode CLI if missing
  4. Deploys the memory MCP server to ~/.config/opencode/memory/
  5. Installs Python dependencies via uv sync --frozen
  6. Initializes the SQLite schema (memory.db, WAL mode)
  7. Copies identity templates (skips existing files)
  8. Merges ~/.config/opencode/opencode.json
  9. Merges Claude Code hooks into ~/.claude/settings.json
 10. Installs the nightly consolidation job (launchd on macOS, cron on Linux)
 11. Extracts Claude.app OAuth tokens (macOS only)

${BOLD}REQUIREMENTS${RESET}
  - macOS (primary) or Linux
  - Python 3.11+, git, curl
  - An Anthropic account for opencode authentication
  - Claude.app installed on macOS for first-party token extraction

EOF
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --help|-h)   usage ;;
    --dry-run)   DRY_RUN=1 ;;
    --no-hooks)  WITH_HOOKS=0 ;;
    --no-daemon) WITH_DAEMON=0 ;;
    --offline)   OFFLINE=1 ;;
    *)           die "Unknown option: $arg (try --help)" ;;
  esac
done

# ── Paths ─────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_DEST="$HOME/.config/opencode/memory"
CONFIG_DEST="$HOME/.config/opencode/opencode.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
LAUNCH_AGENT_LABEL="com.crystallized.dream"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
CRON_MARKER="# crystallized-dream"
MANIFEST_NAME=".crystallized-manifest"
PLIST_TEMPLATE="$REPO_DIR/config/${LAUNCH_AGENT_LABEL}.plist"

# Fallback schedule, used only when config/*.plist is absent. The template owns
# the real schedule on macOS.
DREAM_HOUR=4
DREAM_MINUTE=0

# Must stay on ONE line: it is interpolated into both a plist <string> and a
# crontab entry, and a newline silently corrupts both.
DREAM_FALLBACK_PY='import server; print(server.sleep())'

if [[ "$DRY_RUN" -eq 1 ]]; then
  warn "DRY RUN: no files will be created, modified, or deleted."
fi

# ── OS / arch detection ───────────────────────
detect_os() {
  case "$(uname -s)" in
    Darwin) OS="darwin" ;;
    Linux)  OS="linux"  ;;
    *)      die "Unsupported OS: $(uname -s). Only macOS and Linux are supported." ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    arm64|aarch64) ARCH="arm64"  ;;
    x86_64|amd64)  ARCH="x86_64" ;;
    *)             die "Unsupported architecture: $(uname -m)." ;;
  esac
}

detect_os
detect_arch

info "OS: ${OS}, Arch: ${ARCH}"

# ── Linux/macOS package manager (only used for unzip) ──
pkg_install() {
  local pkg="$1"
  if [[ "$OFFLINE" -eq 1 ]]; then
    die "--offline is set but '$pkg' is missing. Install it manually and re-run."
  fi
  if [[ "$OS" == "darwin" ]]; then
    if ! command -v brew &>/dev/null; then
      die "Homebrew is not installed. Please install it from https://brew.sh first."
    fi
    brew install "$pkg"
  else
    if command -v apt &>/dev/null; then
      sudo apt install -y "$pkg"
    elif command -v apt-get &>/dev/null; then
      sudo apt-get install -y "$pkg"
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y "$pkg"
    elif command -v yum &>/dev/null; then
      sudo yum install -y "$pkg"
    else
      die "No supported package manager found (apt, dnf, yum). Please install '$pkg' manually."
    fi
  fi
}

# ═══════════════════════════════════════════════
step "1. Prerequisites"
# ═══════════════════════════════════════════════

check_git() {
  if ! command -v git &>/dev/null; then
    die "git is not installed. Please install git and re-run this script."
  fi
  success "git $(git --version | awk '{print $3}')"
}

check_python() {
  local py_bin=""
  for bin in python3 python; do
    if command -v "$bin" &>/dev/null; then
      py_bin="$bin"
      break
    fi
  done
  if [[ -z "$py_bin" ]]; then
    die "python3 is not installed. Please install Python 3.11+ and re-run."
  fi

  local version major minor
  version="$("$py_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  major="$(echo "$version" | cut -d. -f1)"
  minor="$(echo "$version" | cut -d. -f2)"

  if [[ "$major" -lt 3 ]] || { [[ "$major" -eq 3 ]] && [[ "$minor" -lt 11 ]]; }; then
    die "Python 3.11+ is required, but found $version. Please upgrade Python."
  fi
  PYTHON_BIN="$py_bin"
  success "python $version"
}

check_curl() {
  if ! command -v curl &>/dev/null; then
    die "curl is not installed. Please install curl and re-run this script."
  fi
  success "curl $(curl --version | head -1 | awk '{print $2}')"
}

check_git
check_python
check_curl

# ═══════════════════════════════════════════════
step "2. uv (Python package manager)"
# ═══════════════════════════════════════════════

install_uv() {
  if command -v uv &>/dev/null; then
    success "uv already installed: $(uv --version 2>/dev/null | awk '{print $2}')"
    return
  fi

  if [[ "$OFFLINE" -eq 1 ]]; then
    die "--offline is set and uv is not installed. Install it first: https://docs.astral.sh/uv/getting-started/installation/"
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "curl -LsSf https://astral.sh/uv/install.sh | sh"
    return
  fi

  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # The installer drops uv into ~/.cargo/bin or ~/.local/bin.
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

  if ! command -v uv &>/dev/null; then
    die "uv installation failed. Please install manually: https://docs.astral.sh/uv/getting-started/installation/"
  fi
  success "uv installed: $(uv --version | awk '{print $2}')"
}

install_uv

# ═══════════════════════════════════════════════
step "3. opencode CLI"
# ═══════════════════════════════════════════════

install_opencode() {
  if command -v opencode &>/dev/null; then
    success "opencode already installed: $(opencode --version 2>/dev/null || echo 'unknown version')"
    return
  fi

  if [[ "$OFFLINE" -eq 1 ]]; then
    warn "--offline is set and opencode is not installed. Skipping."
    warn "Install it later: https://opencode.ai/docs/getting-started"
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "download and install the opencode CLI for ${OS}/${ARCH}"
    return
  fi

  info "Installing opencode..."

  local install_dir
  if [[ "$OS" == "darwin" ]]; then
    install_dir="/usr/local/bin"
  elif [[ -w "/usr/local/bin" ]]; then
    install_dir="/usr/local/bin"
  else
    install_dir="$HOME/.local/bin"
    mkdir -p "$install_dir"
    if [[ ":$PATH:" != *":$install_dir:"* ]]; then
      export PATH="$install_dir:$PATH"
      warn "Added $install_dir to PATH for this session. Add it to your shell profile permanently."
    fi
  fi

  local api_url="https://api.github.com/repos/sst/opencode/releases/latest"
  local release_json
  release_json="$(curl -fsSL "$api_url" 2>/dev/null)" || {
    api_url="https://api.github.com/repos/anomalyco/opencode/releases/latest"
    release_json="$(curl -fsSL "$api_url" 2>/dev/null)" || die "Failed to fetch opencode release info. Check your internet connection."
  }

  local os_name arch_name
  os_name="$OS"
  arch_name="$ARCH"

  local download_url
  download_url="$(echo "$release_json" | $PYTHON_BIN -c "
import sys, json

data = json.load(sys.stdin)
assets = data.get('assets', [])
os_name = '$os_name'
arch = '$arch_name'

os_aliases  = [os_name, 'macos' if os_name == 'darwin' else os_name]
arch_aliases = [arch, 'amd64' if arch == 'x86_64' else arch, 'x64' if arch == 'x86_64' else arch]

def matches(name):
    name_lower = name.lower()
    has_os   = any(a in name_lower for a in os_aliases)
    has_arch = any(a in name_lower for a in arch_aliases)
    return has_os and has_arch

for ext in ['.zip', '.tar.gz', '.tgz']:
    for a in assets:
        n = a['name']
        if matches(n) and n.endswith(ext):
            print(a['browser_download_url'])
            sys.exit(0)

for a in assets:
    if matches(a['name']):
        print(a['browser_download_url'])
        sys.exit(0)

sys.exit(1)
" 2>/dev/null)" || {
    warn "Could not find a matching opencode binary for ${os_name}/${arch_name}."
    warn "Please install opencode manually: https://opencode.ai/docs/getting-started"
    return
  }

  info "Downloading: $download_url"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' RETURN

  local archive_name
  archive_name="$(basename "$download_url")"
  curl -fsSL "$download_url" -o "$tmp_dir/$archive_name"
  mkdir -p "$tmp_dir/extracted"

  if [[ "$archive_name" == *.zip ]]; then
    command -v unzip &>/dev/null || pkg_install unzip
    unzip -q "$tmp_dir/$archive_name" -d "$tmp_dir/extracted"
  elif [[ "$archive_name" == *.tar.gz ]] || [[ "$archive_name" == *.tgz ]]; then
    tar -xzf "$tmp_dir/$archive_name" -C "$tmp_dir/extracted"
  else
    cp "$tmp_dir/$archive_name" "$tmp_dir/extracted/opencode"
  fi

  local binary
  binary="$(find "$tmp_dir" -type f -name "opencode" -not -path "*/\.*" 2>/dev/null | head -1)"
  if [[ -z "$binary" ]]; then
    binary="$(find "$tmp_dir" -maxdepth 3 -type f -perm /111 2>/dev/null | grep -v '\.zip\|\.tar\|\.sh' | head -1)"
  fi

  if [[ -z "$binary" ]]; then
    warn "Could not locate opencode binary in downloaded archive."
    warn "Please install opencode manually: https://opencode.ai/docs/getting-started"
    return
  fi

  chmod +x "$binary"
  if [[ "$install_dir" == "/usr/local/bin" && ! -w "/usr/local/bin" ]]; then
    sudo cp "$binary" "$install_dir/opencode"
  else
    cp "$binary" "$install_dir/opencode"
  fi

  if command -v opencode &>/dev/null; then
    success "opencode installed to $install_dir/opencode"
  else
    warn "opencode copied to $install_dir but not found in PATH."
    warn "Add $install_dir to your PATH, then re-run or run opencode directly."
  fi
}

install_opencode

# ═══════════════════════════════════════════════
step "4. Memory MCP server"
# ═══════════════════════════════════════════════

REQUIRED_MEMORY_FILES=(server.py db.py pyproject.toml uv.lock)
OPTIONAL_MEMORY_FILES=(.python-version README.md)

# Every top-level module ships; tests, fixtures, caches, and the virtualenv all
# live in subdirectories, so a non-recursive *.py glob is the whole allowlist.
collect_memory_modules() {
  local src="$1"
  DEPLOY_FILES=()
  local path
  for path in "$src"/*.py; do
    if [[ -f "$path" ]]; then
      DEPLOY_FILES+=("$(basename "$path")")
    fi
  done
  local fname
  for fname in pyproject.toml uv.lock "${OPTIONAL_MEMORY_FILES[@]}"; do
    if [[ -f "$src/$fname" ]]; then
      DEPLOY_FILES+=("$fname")
    fi
  done
  return 0
}

deploy_memory() {
  local src="$REPO_DIR/memory"
  local dest="$MEMORY_DEST"

  if [[ ! -d "$src" ]]; then
    die "memory/ directory not found in repo ($src)."
  fi

  local fname
  for fname in "${REQUIRED_MEMORY_FILES[@]}"; do
    if [[ ! -f "$src/$fname" ]]; then
      die "Required memory file missing in repo: memory/$fname"
    fi
  done

  collect_memory_modules "$src"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "mkdir -p $dest"
    for fname in "${DEPLOY_FILES[@]}"; do
      dry "cp memory/$fname -> $dest/$fname"
    done
    dry "write manifest $dest/$MANIFEST_NAME (${#DEPLOY_FILES[@]} entries)"
    return
  fi

  mkdir -p "$dest"
  info "Copying memory server files to $dest..."

  for fname in "${DEPLOY_FILES[@]}"; do
    cp "$src/$fname" "$dest/$fname"
    info "  copied: $fname"
  done

  # uninstall.sh removes exactly what this manifest lists. Without it, a
  # glob would also delete unrelated user scripts sharing the directory.
  printf '%s\n' "${DEPLOY_FILES[@]}" > "$dest/$MANIFEST_NAME"

  success "Memory server files deployed (${#DEPLOY_FILES[@]} files)"
}

deploy_memory

# ═══════════════════════════════════════════════
step "5. Python dependencies (uv sync)"
# ═══════════════════════════════════════════════

VENV_PYTHON="$MEMORY_DEST/.venv/bin/python"

run_uv_sync() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "cd $MEMORY_DEST && uv sync --frozen"
    return
  fi

  if [[ ! -f "$MEMORY_DEST/pyproject.toml" ]]; then
    die "No pyproject.toml found in $MEMORY_DEST. Memory deployment failed."
  fi

  local sync_args=(sync --frozen)
  if [[ "$OFFLINE" -eq 1 ]]; then
    sync_args+=(--offline)
    info "Running uv sync --frozen --offline in $MEMORY_DEST..."
  else
    info "Running uv sync --frozen in $MEMORY_DEST..."
  fi

  (cd "$MEMORY_DEST" && uv "${sync_args[@]}") \
    || die "uv sync failed. Check $MEMORY_DEST/pyproject.toml and uv.lock for errors."

  if [[ ! -x "$VENV_PYTHON" ]]; then
    die "uv sync finished but $VENV_PYTHON is missing."
  fi
  success "Python dependencies installed"
}

run_uv_sync

# ═══════════════════════════════════════════════
step "6. SQLite schema"
# ═══════════════════════════════════════════════

init_database() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "cd $MEMORY_DEST && .venv/bin/python -c \"import db; db.init_schema()\""
    return
  fi

  info "Initializing SQLite schema (WAL) at $MEMORY_DEST/memory.db..."
  (cd "$MEMORY_DEST" && "$VENV_PYTHON" -c "import db; db.init_schema()") \
    || die "Schema initialization failed. Run it manually: cd $MEMORY_DEST && .venv/bin/python -c 'import db; db.init_schema()'"

  local schema_version
  schema_version="$(cd "$MEMORY_DEST" && "$VENV_PYTHON" -c \
    "import db; print(db.get_db().execute('PRAGMA user_version').fetchone()[0])" 2>/dev/null || echo "?")"
  success "Schema ready (user_version=$schema_version)"
}

init_database

# ═══════════════════════════════════════════════
step "7. Identity templates"
# ═══════════════════════════════════════════════

copy_templates() {
  local src="$REPO_DIR/templates/notes"
  local dest="$MEMORY_DEST/notes"

  if [[ ! -d "$src" ]]; then
    warn "templates/notes/ not found in repo. Skipping template copy."
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "copy templates/notes/* -> $dest/ (skipping existing files)"
    return
  fi

  mkdir -p "$dest"
  info "Copying templates (skipping existing files)..."

  local copied=0 skipped=0
  while IFS= read -r -d '' src_file; do
    local rel_path="${src_file#"$src"/}"
    local dest_file="$dest/$rel_path"
    local dest_parent
    dest_parent="$(dirname "$dest_file")"

    mkdir -p "$dest_parent"
    if [[ -e "$dest_file" ]]; then
      info "  skip (exists): $rel_path"
      ((skipped++)) || true
    else
      cp "$src_file" "$dest_file"
      info "  copied: $rel_path"
      ((copied++)) || true
    fi
  done < <(find "$src" -type f -print0)

  success "Templates: $copied copied, $skipped skipped (already exist)"
}

copy_templates

# ═══════════════════════════════════════════════
step "8. opencode config (opencode.json)"
# ═══════════════════════════════════════════════

generate_config() {
  local template="$REPO_DIR/config/opencode.json"
  local dest="$CONFIG_DEST"

  if [[ ! -f "$template" ]]; then
    warn "config/opencode.json template not found in repo ($template). Skipping."
    return
  fi

  local uv_path
  uv_path="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "merge config/opencode.json into $dest (backup first if it exists)"
    return
  fi

  mkdir -p "$(dirname "$dest")"

  if [[ ! -f "$dest" ]]; then
    sed -e "s|MEMORY_PATH|$MEMORY_DEST|g" -e "s|UV_PATH|$uv_path|g" "$template" > "$dest"
    success "Config written to $dest"
    return
  fi

  local backup
  backup="$dest.bak.$(date +%Y%m%d-%H%M%S)"
  cp "$dest" "$backup"
  info "Backup of existing config written to: $backup"
  info "Merging MCP and plugin entries from template..."

  "$PYTHON_BIN" - "$template" "$dest" "$MEMORY_DEST" "$uv_path" <<'PY' || {
import json
import sys

template_path, dest_path, memory_path, uv_path = sys.argv[1:5]

with open(template_path) as f:
    raw = f.read().replace("MEMORY_PATH", memory_path).replace("UV_PATH", uv_path)
template_config = json.loads(raw)

with open(dest_path) as f:
    existing = json.load(f)

# MCP servers: never overwrite an entry the user already defined.
added, skipped = [], []
existing_mcps = existing.setdefault("mcp", {})
for name, cfg in template_config.get("mcp", {}).items():
    if name in existing_mcps:
        skipped.append(name)
    else:
        existing_mcps[name] = cfg
        added.append(name)

# Plugins: append missing entries, preserve order, no duplicates.
plugins_added = []
tmpl_plugins = template_config.get("plugin", [])
if tmpl_plugins:
    existing_plugins = existing.setdefault("plugin", [])
    existing_names = {p.split("@", 1)[0] for p in existing_plugins if isinstance(p, str)}
    for plugin in tmpl_plugins:
        if isinstance(plugin, str) and plugin.split("@", 1)[0] not in existing_names:
            existing_plugins.append(plugin)
            plugins_added.append(plugin)

with open(dest_path, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")

if added:
    print(f"[ok]    MCP entries added: {', '.join(added)}")
if skipped:
    print(f"[info]  MCP entries already present (unchanged): {', '.join(skipped)}")
if plugins_added:
    print(f"[ok]    Plugins added: {', '.join(plugins_added)}")
if not (added or skipped or plugins_added):
    print("[ok]    Config is already up to date")
PY
    warn "Could not merge config automatically. Your original is at $backup."
    warn "Merge the MCP section from $template into $dest manually."
    return
  }

  success "opencode.json merged"
}

generate_config

# ═══════════════════════════════════════════════
step "9. Claude Code hooks (~/.claude/settings.json)"
# ═══════════════════════════════════════════════

register_hooks() {
  if [[ "$WITH_HOOKS" -eq 0 ]]; then
    info "Skipping hook registration (--no-hooks)."
    return
  fi

  local template="$REPO_DIR/config/claude-settings.json"
  if [[ ! -f "$template" ]]; then
    warn "config/claude-settings.json not found in repo. Skipping hook registration."
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "merge config/claude-settings.json hooks into $CLAUDE_SETTINGS (backup first if it exists)"
    return
  fi

  mkdir -p "$(dirname "$CLAUDE_SETTINGS")"

  if [[ -f "$CLAUDE_SETTINGS" ]]; then
    local backup
    backup="$CLAUDE_SETTINGS.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$CLAUDE_SETTINGS" "$backup"
    info "Backup of existing settings written to: $backup"
  else
    info "No settings file yet, creating $CLAUDE_SETTINGS"
  fi

  "$PYTHON_BIN" - "$template" "$CLAUDE_SETTINGS" "$MEMORY_DEST" <<'PY' || {
import json
import os
import sys

template_path, dest_path, memory_path = sys.argv[1:4]

with open(template_path) as f:
    template = json.loads(f.read().replace("MEMORY_PATH", memory_path))

settings = {}
if os.path.exists(dest_path):
    with open(dest_path) as f:
        content = f.read().strip()
    if content:
        settings = json.loads(content)
    if not isinstance(settings, dict):
        raise SystemExit(f"{dest_path} does not contain a JSON object")

# Merge ONLY the hooks tree. Every other top-level key stays untouched.
hooks = settings.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"{dest_path}: 'hooks' is not an object, refusing to merge")

added = 0
for event, groups in template.get("hooks", {}).items():
    existing_groups = hooks.setdefault(event, [])
    if not isinstance(existing_groups, list):
        raise SystemExit(f"{dest_path}: hooks.{event} is not a list, refusing to merge")

    # Commands already registered for this event, at any nesting level.
    registered = {
        entry.get("command")
        for group in existing_groups
        if isinstance(group, dict)
        for entry in group.get("hooks", [])
        if isinstance(entry, dict)
    }

    for group in groups:
        new_entries = [
            entry for entry in group.get("hooks", [])
            if entry.get("command") not in registered
        ]
        if not new_entries:
            continue
        merged_group = {k: v for k, v in group.items() if k != "hooks"}
        merged_group["hooks"] = new_entries
        existing_groups.append(merged_group)
        added += len(new_entries)

with open(dest_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

if added:
    print(f"[ok]    Registered {added} hook command(s)")
else:
    print("[ok]    Hooks already registered (unchanged)")
PY
    warn "Could not merge Claude hooks automatically. Your settings file is unchanged or restored from backup."
    warn "Merge $template into $CLAUDE_SETTINGS manually (replace MEMORY_PATH with $MEMORY_DEST)."
    return
  }

  success "Claude Code hooks registered in $CLAUDE_SETTINGS"
}

register_hooks

# ═══════════════════════════════════════════════
step "10. Nightly consolidation job"
# ═══════════════════════════════════════════════

resolve_dream_command() {
  if [[ -f "$REPO_DIR/memory/dream.py" ]]; then
    DREAM_PLIST_ARGS="        <string>${VENV_PYTHON}</string>
        <string>${MEMORY_DEST}/dream.py</string>
        <string>--nightly</string>"
    DREAM_SHELL_CMD="${VENV_PYTHON} ${MEMORY_DEST}/dream.py --nightly"
  else
    DREAM_PLIST_ARGS="        <string>${VENV_PYTHON}</string>
        <string>-c</string>
        <string>${DREAM_FALLBACK_PY}</string>"
    DREAM_SHELL_CMD="${VENV_PYTHON} -c '${DREAM_FALLBACK_PY}'"
  fi
}

install_daemon_darwin() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "write $LAUNCH_AGENT_PLIST and load it with launchctl"
    dry "  command: $DREAM_SHELL_CMD"
    return
  fi

  mkdir -p "$(dirname "$LAUNCH_AGENT_PLIST")"

  # config/*.plist is the authoritative agent definition (schedule, ProcessType,
  # LowPriorityIO). Only fall back to a generated plist if it is missing.
  if [[ -f "$PLIST_TEMPLATE" ]]; then
    sed "s|MEMORY_PATH|$MEMORY_DEST|g" "$PLIST_TEMPLATE" > "$LAUNCH_AGENT_PLIST"
    info "Plist rendered from config/${LAUNCH_AGENT_LABEL}.plist"
  else
    write_fallback_plist
  fi

  load_launch_agent
}

write_fallback_plist() {
  cat > "$LAUNCH_AGENT_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
${DREAM_PLIST_ARGS}
    </array>
    <key>WorkingDirectory</key>
    <string>${MEMORY_DEST}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${DREAM_HOUR}</integer>
        <key>Minute</key>
        <integer>${DREAM_MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>${MEMORY_DEST}/dream.log</string>
    <key>StandardErrorPath</key>
    <string>${MEMORY_DEST}/dream.log</string>
</dict>
</plist>
PLIST
  info "Plist generated (no template found in config/)"
}

load_launch_agent() {
  # Replace any previously loaded instance. Both calls are best-effort:
  # bootout fails when nothing is loaded, which is not an error here.
  launchctl bootout "gui/$(id -u)/${LAUNCH_AGENT_LABEL}" &>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PLIST" &>/dev/null; then
    success "launchd agent loaded: $LAUNCH_AGENT_LABEL"
  elif launchctl load -w "$LAUNCH_AGENT_PLIST" &>/dev/null; then
    success "launchd agent loaded (legacy path): $LAUNCH_AGENT_LABEL"
  else
    warn "Plist written to $LAUNCH_AGENT_PLIST but launchctl could not load it."
    warn "Load it manually: launchctl bootstrap gui/$(id -u) $LAUNCH_AGENT_PLIST"
  fi
}

install_daemon_linux() {
  local cron_line="${DREAM_MINUTE} ${DREAM_HOUR} * * * cd ${MEMORY_DEST} && ${DREAM_SHELL_CMD} >> ${MEMORY_DEST}/dream.log 2>&1 ${CRON_MARKER}"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "install crontab entry: $cron_line"
    return
  fi

  if ! command -v crontab &>/dev/null; then
    warn "crontab is not available. Skipping the nightly consolidation job."
    warn "Run this yourself on a schedule:"
    warn "  cd ${MEMORY_DEST} && ${DREAM_SHELL_CMD}"
    return
  fi

  local current
  current="$(crontab -l 2>/dev/null || true)"

  # Drop any previous crystallized entry, then append the fresh one.
  local filtered
  filtered="$(printf '%s\n' "$current" | grep -v -F "$CRON_MARKER" || true)"

  {
    if [[ -n "${filtered//[$'\n't ]/}" ]]; then
      printf '%s\n' "$filtered"
    fi
    printf '%s\n' "$cron_line"
  } | crontab - || {
    warn "Could not update crontab. Add this line yourself:"
    warn "  $cron_line"
    return
  }

  success "cron job installed (daily at ${DREAM_HOUR}:$(printf '%02d' "$DREAM_MINUTE"))"
}

install_daemon() {
  if [[ "$WITH_DAEMON" -eq 0 ]]; then
    info "Skipping the nightly consolidation job (--no-daemon)."
    return
  fi

  resolve_dream_command

  if [[ "$OS" == "darwin" ]]; then
    install_daemon_darwin
  else
    install_daemon_linux
  fi
}

install_daemon

# ═══════════════════════════════════════════════
step "11. Authentication"
# ═══════════════════════════════════════════════

authenticate() {
  if [[ "$OS" != "darwin" ]]; then
    info "Linux detected, skipping automatic auth. Run manually:"
    info "  python3 $REPO_DIR/auth/extract_token.py"
    return
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "extract Claude.app OAuth tokens into ~/.local/share/opencode/auth.json"
    return
  fi

  if [[ ! -d "/Applications/Claude.app" ]]; then
    warn "Claude.app not found at /Applications/Claude.app."
    warn "Install Claude for macOS, then run: python3 $REPO_DIR/auth/extract_token.py"
    return
  fi

  local auth_file="$HOME/.local/share/opencode/auth.json"
  if [[ -f "$auth_file" ]]; then
    local has_token
    has_token="$("$PYTHON_BIN" - "$auth_file" <<'PY' 2>/dev/null || echo no
import json
import sys

try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    anthropic = data.get("providers", data).get("anthropic", {})
    keys = ("refresh", "refreshToken", "refresh_token")
    print("yes" if any(anthropic.get(k) for k in keys) else "no")
except Exception:
    print("no")
PY
)"
    if [[ "$has_token" == "yes" ]]; then
      success "Already authenticated"
      return
    fi
  fi

  # The deployed virtualenv already carries `cryptography` (via mcp[cli] ->
  # pyjwt[crypto]), so the extractor runs there instead of mutating the
  # system Python.
  local auth_python="$PYTHON_BIN"
  if [[ -x "$VENV_PYTHON" ]]; then
    auth_python="$VENV_PYTHON"
  fi

  # Keychain unlock needs an interactive prompt. Without a TTY the read fails,
  # and under `set -e` that would abort the installer after all work is done.
  if [[ ! -t 0 ]]; then
    warn "Not running interactively, skipping Keychain unlock."
    warn "Finish auth manually: $VENV_PYTHON $REPO_DIR/auth/extract_token.py"
    return
  fi

  local password=""
  if ! read -r -s -p "Enter your macOS login password (for Keychain access): " password; then
    echo ""
    warn "Could not read the password. Run auth manually:"
    warn "  $REPO_DIR/auth/extract_token.py"
    return
  fi
  echo ""
  security unlock-keychain -p "$password" ~/Library/Keychains/login.keychain-db 2>/dev/null || {
    warn "Keychain unlock failed. This usually means:"
    warn "  - the password is wrong, or"
    warn "  - the login keychain is in a different location."
    warn "The extractor will try anyway. If it fails, run it manually:"
    warn "  $auth_python $REPO_DIR/auth/extract_token.py"
  }

  info "Extracting Claude auth token..."
  local idx
  for idx in 0 1; do
    if "$auth_python" "$REPO_DIR/auth/extract_token.py" --skip-quit-check --apply "$idx" 2>/dev/null; then
      success "Authentication complete"
      return
    fi
    info "Token $idx did not apply, trying the next one..."
  done

  warn "Automatic auth failed. Run manually after setup:"
  warn "  $auth_python $REPO_DIR/auth/extract_token.py"
}

authenticate

# ═══════════════════════════════════════════════
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  success "Dry run complete. Nothing was changed."
  exit 0
fi

echo -e "\n${GREEN}${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║   crystallized v2.0 setup complete!      ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${RESET}\n"

echo -e "${BOLD}Next steps:${RESET}"
echo -e "  ${CYAN}1.${RESET} Run ${BOLD}opencode${RESET} to start the AI assistant"
echo -e "  ${CYAN}2.${RESET} The memory MCP server starts automatically via opencode"
echo ""
echo -e "${YELLOW}Tip:${RESET} If 'opencode' is not found, add ${BOLD}$HOME/.local/bin${RESET} to your PATH:"
echo -e "       ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
echo -e "     (add this to ~/.bashrc, ~/.zshrc, or ~/.profile)"
echo ""
echo -e "${YELLOW}If auth failed, run:${RESET} ${BOLD}python3 auth/extract_token.py${RESET}"
echo ""
echo -e "${CYAN}Memory database:${RESET} ${BOLD}$MEMORY_DEST/memory.db${RESET}"
echo -e "${CYAN}opencode config:${RESET} ${BOLD}$CONFIG_DEST${RESET}"
if [[ "$WITH_HOOKS" -eq 1 ]]; then
  echo -e "${CYAN}Claude hooks:${RESET}    ${BOLD}$CLAUDE_SETTINGS${RESET}"
fi
if [[ "$WITH_DAEMON" -eq 1 ]]; then
  if [[ "$OS" == "darwin" ]]; then
    echo -e "${CYAN}Nightly job:${RESET}     ${BOLD}$LAUNCH_AGENT_PLIST${RESET}"
  else
    echo -e "${CYAN}Nightly job:${RESET}     ${BOLD}crontab (${CRON_MARKER})${RESET}"
  fi
fi
echo ""
