#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# crystallized — one-shot setup script
# Sets up opencode + oh-my-openagent + MCP memory server
# ─────────────────────────────────────────────

# ── Colors ────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Helpers ───────────────────────────────────
info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
success() { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}  $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo -e "\n${BOLD}──── $* ────${RESET}"; }

# ── Help ──────────────────────────────────────
usage() {
  cat <<EOF
${BOLD}crystallized install.sh${RESET}

One-shot setup for opencode + persistent AI memory.

${BOLD}USAGE${RESET}
  ./install.sh [OPTIONS]

${BOLD}OPTIONS${RESET}
  --help, -h    Show this message and exit

${BOLD}WHAT THIS DOES${RESET}
  1. Checks prerequisites (git, python3 >=3.11, curl)
  2. Installs Redis (if not present) and starts the service
  3. Installs uv (Python package manager, if not present)
  4. Installs opencode CLI (if not present)
  5. Deploys the memory MCP server to ~/.config/opencode/memory/
  6. Installs Python dependencies via uv sync
  7. Copies identity templates (skips existing files)
  8. Generates or merges ~/.config/opencode/opencode.json

${BOLD}REQUIREMENTS${RESET}
  - macOS (uses Homebrew) or Linux (Debian/Ubuntu or Fedora/RHEL)
  - An Anthropic account (for opencode authentication)
  - Homebrew must already be installed on macOS (brew.sh)

EOF
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --help|-h) usage ;;
  esac
done

# ── Repo root (where this script lives) ───────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMORY_DEST="$HOME/.config/opencode/memory"
CONFIG_DEST="$HOME/.config/opencode/opencode.json"

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

# ── Linux package manager ─────────────────────
pkg_install() {
  local pkg="$1"
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

  local version
  version="$("$py_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  local major minor
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
step "2. Redis"
# ═══════════════════════════════════════════════

install_redis() {
  if command -v redis-server &>/dev/null; then
    success "Redis already installed: $(redis-server --version | awk '{print $3}' | tr -d 'v=')"
    return
  fi

  info "Installing Redis..."
  if [[ "$OS" == "darwin" ]]; then
    if ! command -v brew &>/dev/null; then
      die "Homebrew is required to install Redis on macOS. Install it from https://brew.sh"
    fi
    brew install redis
  else
    if command -v apt &>/dev/null || command -v apt-get &>/dev/null; then
      # Prefer apt over apt-get
      if command -v apt &>/dev/null; then
        sudo apt update -qq
        sudo apt install -y redis-server
      else
        sudo apt-get update -qq
        sudo apt-get install -y redis-server
      fi
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y redis
    elif command -v yum &>/dev/null; then
      sudo yum install -y redis
    else
      die "Cannot install Redis automatically. Please install Redis manually: https://redis.io/docs/getting-started/"
    fi
  fi
  success "Redis installed"
}

start_redis() {
  # Check if already running
  if redis-cli ping &>/dev/null 2>&1; then
    success "Redis is running"
    return
  fi

  info "Starting Redis..."
  if [[ "$OS" == "darwin" ]]; then
    brew services start redis 2>/dev/null || redis-server --daemonize yes --loglevel warning
  else
    # Try systemd first
    if command -v systemctl &>/dev/null; then
      sudo systemctl enable redis-server 2>/dev/null || sudo systemctl enable redis 2>/dev/null || true
      sudo systemctl start redis-server 2>/dev/null || sudo systemctl start redis 2>/dev/null || true
    else
      redis-server --daemonize yes --loglevel warning
    fi
  fi

  # Verify
  sleep 1
  if redis-cli ping &>/dev/null 2>&1; then
    success "Redis started"
  else
    warn "Could not verify Redis is running. Memory MCP may not work correctly."
  fi
}

install_redis
start_redis

# ═══════════════════════════════════════════════
step "3. uv (Python package manager)"
# ═══════════════════════════════════════════════

install_uv() {
  if command -v uv &>/dev/null; then
    success "uv already installed: $(uv --version 2>/dev/null | awk '{print $2}')"
    return
  fi

  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh

  # Add uv to PATH for current session (installer puts it in ~/.cargo/bin or ~/.local/bin)
  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

  if ! command -v uv &>/dev/null; then
    die "uv installation failed. Please install manually: https://docs.astral.sh/uv/getting-started/installation/"
  fi
  success "uv installed: $(uv --version | awk '{print $2}')"
}

install_uv

# ═══════════════════════════════════════════════
step "4. opencode CLI"
# ═══════════════════════════════════════════════

install_opencode() {
  if command -v opencode &>/dev/null; then
    success "opencode already installed: $(opencode --version 2>/dev/null || echo 'unknown version')"
    return
  fi

  info "Installing opencode..."

  # Determine target install directory
  local install_dir
  if [[ "$OS" == "darwin" ]]; then
    install_dir="/usr/local/bin"
  elif [[ -w "/usr/local/bin" ]]; then
    install_dir="/usr/local/bin"
  else
    install_dir="$HOME/.local/bin"
    mkdir -p "$install_dir"
    # Ensure it's in PATH
    if [[ ":$PATH:" != *":$install_dir:"* ]]; then
      export PATH="$install_dir:$PATH"
      warn "Added $install_dir to PATH for this session. Add it to your shell profile permanently."
    fi
  fi

  # Fetch latest release download URL
  local api_url="https://api.github.com/repos/sst/opencode/releases/latest"
  local release_json
  release_json="$(curl -fsSL "$api_url" 2>/dev/null)" || {
    # Fallback to anomalyco fork
    api_url="https://api.github.com/repos/anomalyco/opencode/releases/latest"
    release_json="$(curl -fsSL "$api_url" 2>/dev/null)" || die "Failed to fetch opencode release info. Check your internet connection."
  }

  # Map to asset naming conventions
  local os_name arch_name
  os_name="$OS"            # darwin / linux
  arch_name="$ARCH"        # arm64 / x86_64

  # Try to extract download URL matching OS/arch
  # Common patterns: opencode_darwin_arm64.zip, opencode-linux-x86_64.tar.gz, etc.
  local download_url
  download_url="$(echo "$release_json" | $PYTHON_BIN -c "
import sys, json, re

data = json.load(sys.stdin)
assets = data.get('assets', [])
os_name = '$os_name'
arch = '$arch_name'

# Alias mappings
os_aliases  = [os_name, 'macos' if os_name == 'darwin' else os_name]
arch_aliases = [arch, 'amd64' if arch == 'x86_64' else arch, 'x64' if arch == 'x86_64' else arch]

def matches(name):
    name_lower = name.lower()
    has_os   = any(a in name_lower for a in os_aliases)
    has_arch = any(a in name_lower for a in arch_aliases)
    return has_os and has_arch

# Prefer zip, then tar.gz, then any archive
for ext in ['.zip', '.tar.gz', '.tgz']:
    for a in assets:
        n = a['name']
        if matches(n) and n.endswith(ext):
            print(a['browser_download_url'])
            sys.exit(0)

# Last resort: first matching asset
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

  # Extract
  if [[ "$archive_name" == *.zip ]]; then
    command -v unzip &>/dev/null || pkg_install unzip
    unzip -q "$tmp_dir/$archive_name" -d "$tmp_dir/extracted"
  elif [[ "$archive_name" == *.tar.gz ]] || [[ "$archive_name" == *.tgz ]]; then
    tar -xzf "$tmp_dir/$archive_name" -C "$tmp_dir/extracted" 2>/dev/null || {
      mkdir -p "$tmp_dir/extracted"
      tar -xzf "$tmp_dir/$archive_name" -C "$tmp_dir/extracted"
    }
  else
    # Might be a raw binary
    cp "$tmp_dir/$archive_name" "$tmp_dir/extracted/opencode"
  fi

  # Find the opencode binary in extracted content
  local binary
  binary="$(find "$tmp_dir" -type f -name "opencode" -not -path "*/\.*" 2>/dev/null | head -1)"
  if [[ -z "$binary" ]]; then
    # Maybe it's named differently or in root
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
step "5. Memory MCP server"
# ═══════════════════════════════════════════════

deploy_memory() {
  local src="$REPO_DIR/memory"
  local dest="$MEMORY_DEST"

  if [[ ! -d "$src" ]]; then
    warn "memory/ directory not found in repo ($src). Skipping memory server deployment."
    return
  fi

  mkdir -p "$dest"
  info "Copying memory server files to $dest..."

  # Copy all files from memory/ — overwrite scripts but keep user data
  for f in "$src"/*; do
    [[ -e "$f" ]] || continue
    local fname
    fname="$(basename "$f")"
    cp "$f" "$dest/$fname"
    info "  copied: $fname"
  done
  success "Memory server files deployed"
}

deploy_memory

# ═══════════════════════════════════════════════
step "6. Python dependencies (uv sync)"
# ═══════════════════════════════════════════════

run_uv_sync() {
  if [[ ! -f "$MEMORY_DEST/pyproject.toml" ]]; then
    warn "No pyproject.toml found in $MEMORY_DEST. Skipping uv sync."
    return
  fi

  info "Running uv sync in $MEMORY_DEST..."
  (cd "$MEMORY_DEST" && uv sync) || die "uv sync failed. Check $MEMORY_DEST/pyproject.toml for errors."
  success "Python dependencies installed"
}

run_uv_sync

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

  mkdir -p "$dest"
  info "Copying templates (skipping existing files)..."

  local copied=0 skipped=0
  while IFS= read -r -d '' src_file; do
    local rel_path="${src_file#$src/}"
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
  local dest_dir
  dest_dir="$(dirname "$dest")"

  mkdir -p "$dest_dir"

  # Expand actual memory path
  local memory_path="$MEMORY_DEST"

  if [[ ! -f "$template" ]]; then
    warn "config/opencode.json template not found in repo ($template)."
    warn "Skipping config generation. Create $dest manually."
    return
  fi

  if [[ ! -f "$dest" ]]; then
    # Fresh install — copy template with placeholder replaced
    sed "s|MEMORY_PATH|$memory_path|g" "$template" > "$dest"
    success "Config written to $dest"
    return
  fi

  # Config already exists — attempt to merge MCP section
  warn "Config already exists at $dest"
  info "Attempting to merge MCP section from template..."

  # Use Python for JSON merging (already a prerequisite)
  local merge_result
  merge_result="$($PYTHON_BIN -c "
import json, sys

template_path = '$template'
dest_path     = '$dest'
memory_path   = '$memory_path'

# Load template (replace placeholder)
with open(template_path) as f:
    raw = f.read().replace('MEMORY_PATH', memory_path)
template_config = json.loads(raw)

# Load existing config
with open(dest_path) as f:
    existing = json.load(f)

# Merge: template MCP entries into existing (don't overwrite existing MCPs with same name)
template_mcps = template_config.get('mcp', {})
existing_mcps = existing.setdefault('mcp', {})

added = []
skipped = []
for name, cfg in template_mcps.items():
    if name in existing_mcps:
        skipped.append(name)
    else:
        existing_mcps[name] = cfg
        added.append(name)

# Write merged config
with open(dest_path, 'w') as f:
    json.dump(existing, f, indent=2)
    f.write('\n')

# Report
print('added:'   + ','.join(added)   if added   else 'added:')
print('skipped:' + ','.join(skipped) if skipped else 'skipped:')
" 2>&1)" || {
    warn "Could not automatically merge config. Your existing config is unchanged."
    warn "Please manually merge the MCP section from $template into $dest"
    warn "(replace MEMORY_PATH with: $memory_path)"
    return
  }

  local added_line skipped_line
  added_line="$(echo "$merge_result" | grep '^added:')"
  skipped_line="$(echo "$merge_result" | grep '^skipped:')"
  local added="${added_line#added:}"
  local skipped="${skipped_line#skipped:}"

  if [[ -n "$added" ]]; then
    success "MCP entries added: $added"
  fi
  if [[ -n "$skipped" ]]; then
    info "MCP entries already present (unchanged): $skipped"
  fi
  if [[ -z "$added" && -z "$skipped" ]]; then
    success "Config is already up to date"
  fi
}

generate_config

# ═══════════════════════════════════════════════
echo -e "\n${GREEN}${BOLD}╔══════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║   🎉  crystallized setup complete!       ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════╝${RESET}\n"

echo -e "${BOLD}Next steps:${RESET}"
echo -e "  ${CYAN}1.${RESET} Run ${BOLD}opencode${RESET} to start the AI assistant"
echo -e "  ${CYAN}2.${RESET} On first run, authenticate with your Anthropic account"
echo -e "  ${CYAN}3.${RESET} The memory MCP server will start automatically via opencode"
echo ""
echo -e "${YELLOW}Tip:${RESET} If 'opencode' is not found, add ${BOLD}$HOME/.local/bin${RESET} to your PATH:"
echo -e "       ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
echo -e "     (add this to ~/.bashrc, ~/.zshrc, or ~/.profile)"
echo ""
echo -e "${CYAN}Memory data stored at:${RESET} ${BOLD}$MEMORY_DEST${RESET}"
echo -e "${CYAN}Config file:${RESET}           ${BOLD}$CONFIG_DEST${RESET}"
echo ""
