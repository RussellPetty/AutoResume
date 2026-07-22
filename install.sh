#!/usr/bin/env bash
set -euo pipefail

VERSION="0.1.0"
REPO_RAW="https://raw.githubusercontent.com/RussellPetty/AutoResume/main"
PREFIX="${HOME}/.local"
YES=0
ALIASES=1
UNINSTALL=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install.sh [--yes] [--no-aliases] [--prefix PATH] [--uninstall] [--dry-run]

  --yes          Install dependencies without prompting
  --no-aliases   Do not edit Bash or Zsh startup files
  --prefix PATH  Install under PATH (default: ~/.local)
  --uninstall    Remove AutoResume and restore shell/Claude settings
  --dry-run      Validate the platform and inputs without writing files
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --no-aliases) ALIASES=0 ;;
    --prefix)
      [ "$#" -ge 2 ] || { echo "install.sh: --prefix requires a path" >&2; exit 2; }
      PREFIX="$2"
      shift
      ;;
    --uninstall) UNINSTALL=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  *) echo "AutoResume supports macOS and Linux only." >&2; exit 1 ;;
esac

CONFIG_BASE="${XDG_CONFIG_HOME:-${HOME}/.config}/autoresume"
STATE_BASE="${XDG_STATE_HOME:-${HOME}/.local/state}/autoresume"
SHARE_BASE="${PREFIX}/share/autoresume"
INSTALL_DIR="${SHARE_BASE}/${VERSION}"
BIN_DIR="${PREFIX}/bin"
BIN_PATH="${BIN_DIR}/autoresume"
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
STATUS_COMMAND="${SHARE_BASE}/current/statusline.py"
MARKER_START="# >>> AutoResume >>>"
MARKER_END="# <<< AutoResume <<<"

startup_files() {
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) printf '%s\n' "${HOME}/.zshrc" ;;
    bash)
      if [ "$PLATFORM" = "macos" ]; then
        printf '%s\n' "${HOME}/.bash_profile"
      else
        printf '%s\n' "${HOME}/.bashrc"
      fi
      ;;
    *)
      [ -f "${HOME}/.zshrc" ] && printf '%s\n' "${HOME}/.zshrc"
      [ -f "${HOME}/.bashrc" ] && printf '%s\n' "${HOME}/.bashrc"
      ;;
  esac
}

remove_alias_block() {
  target="$1"
  [ -f "$target" ] || return 0
  temp="${target}.autoresume.$$"
  awk -v start="$MARKER_START" -v end="$MARKER_END" '
    $0 == start { skip=1; next }
    $0 == end { skip=0; next }
    !skip { print }
  ' "$target" > "$temp"
  mv "$temp" "$target"
}

restore_claude_settings() {
  SETTINGS_PATH="$CLAUDE_SETTINGS" META_PATH="${CONFIG_BASE}/claude-statusline.json" \
    python3 - <<'PY'
import json, os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
meta_path = Path(os.environ["META_PATH"])
try:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)
try:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    settings = None
installed = meta.get("installed_status_line")
if isinstance(settings, dict) and settings.get("statusLine") == installed:
    if meta.get("settings_was_malformed") and not [k for k in settings if k != "statusLine"]:
        backup = Path(meta.get("backup_path", ""))
        if backup.is_file():
            settings_path.write_bytes(backup.read_bytes())
    else:
        if meta.get("had_status_line"):
            settings["statusLine"] = meta.get("previous_status_line")
        else:
            settings.pop("statusLine", None)
        settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
meta_path.unlink(missing_ok=True)
PY
}

if [ "$UNINSTALL" -eq 1 ]; then
  if command -v python3 >/dev/null 2>&1; then
    restore_claude_settings
  fi
  while IFS= read -r file; do
    [ -n "$file" ] && remove_alias_block "$file"
  done <<EOF
$(startup_files)
EOF
  if [ -L "$BIN_PATH" ]; then
    link_target="$(readlink "$BIN_PATH")"
    case "$link_target" in
      "$SHARE_BASE"/*) rm -f "$BIN_PATH" ;;
    esac
  elif [ -f "$BIN_PATH" ] && grep -q "AutoResume" "$BIN_PATH" 2>/dev/null; then
    rm -f "$BIN_PATH"
  fi
  case "$SHARE_BASE" in
    */share/autoresume) rm -rf "$SHARE_BASE" ;;
    *) echo "Refusing to remove unexpected share path: $SHARE_BASE" >&2; exit 1 ;;
  esac
  case "$CONFIG_BASE" in */autoresume) rm -rf "$CONFIG_BASE" ;; esac
  case "$STATE_BASE" in */autoresume) rm -rf "$STATE_BASE" ;; esac
  echo "AutoResume has been uninstalled. Existing provider sessions were not touched."
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "Python 3.9 or newer is required." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Python 3.9 or newer is required." >&2
  exit 1
}

install_tmux() {
  if command -v tmux >/dev/null 2>&1; then return 0; fi
  manager=""
  command -v brew >/dev/null 2>&1 && manager="brew"
  command -v apt-get >/dev/null 2>&1 && manager="apt"
  command -v dnf >/dev/null 2>&1 && manager="dnf"
  command -v pacman >/dev/null 2>&1 && manager="pacman"
  [ -n "$manager" ] || { echo "tmux is required; install it with your system package manager." >&2; exit 1; }
  if [ "$YES" -ne 1 ]; then
    if [ ! -r /dev/tty ]; then
      echo "tmux is missing. Re-run with --yes to install it with $manager." >&2
      exit 1
    fi
    printf 'tmux is required. Install it with %s? [y/N] ' "$manager" >/dev/tty
    read -r answer </dev/tty
    case "$answer" in y|Y|yes|YES) ;; *) exit 1 ;; esac
  fi
  case "$manager" in
    brew) brew install tmux ;;
    apt) sudo apt-get update && sudo apt-get install -y tmux ;;
    dnf) sudo dnf install -y tmux ;;
    pacman) sudo pacman -S --needed --noconfirm tmux ;;
  esac
}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "AutoResume ${VERSION} installer checks passed for ${PLATFORM} (prefix ${PREFIX})."
  exit 0
fi

install_tmux
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$CONFIG_BASE" "$STATE_BASE"

copy_or_download() {
  source_relative="$1"
  destination="$2"
  if [ -n "${AUTORESUME_SOURCE_DIR:-}" ] && [ -f "${AUTORESUME_SOURCE_DIR}/${source_relative}" ]; then
    cp "${AUTORESUME_SOURCE_DIR}/${source_relative}" "$destination"
  elif [ -f "$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)/${source_relative}" ]; then
    cp "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/${source_relative}" "$destination"
  else
    command -v curl >/dev/null 2>&1 || { echo "curl is required to download AutoResume." >&2; exit 1; }
    curl -fsSL "${REPO_RAW}/${source_relative}" -o "$destination"
  fi
}

copy_or_download "src/autoresume.py" "${INSTALL_DIR}/autoresume.py"
copy_or_download "src/statusline.py" "${INSTALL_DIR}/statusline.py"
copy_or_download "install.sh" "${INSTALL_DIR}/install.sh"
chmod 755 "${INSTALL_DIR}/autoresume.py" "${INSTALL_DIR}/statusline.py" "${INSTALL_DIR}/install.sh"
[ ! -e "${SHARE_BASE}/current" ] || [ -L "${SHARE_BASE}/current" ] || {
  echo "Refusing to replace non-symlink path: ${SHARE_BASE}/current" >&2
  exit 1
}
if [ -e "$BIN_PATH" ] && [ ! -L "$BIN_PATH" ]; then
  grep -q "AutoResume" "$BIN_PATH" 2>/dev/null || {
    echo "Refusing to replace existing executable: $BIN_PATH" >&2
    exit 1
  }
  rm -f "$BIN_PATH"
fi
ln -sfn "$INSTALL_DIR" "${SHARE_BASE}/current"
ln -sfn "${SHARE_BASE}/current/autoresume.py" "$BIN_PATH"

CONFIG_PATH="${CONFIG_BASE}/config.json" python3 - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ["CONFIG_PATH"])
defaults = {"schema_version": 1, "enabled": True, "poll_seconds": 2,
            "grace_seconds": 30, "continuation_prompt": "continue"}
try:
    current = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    current = {}
if not isinstance(current, dict):
    current = {}
for key, value in defaults.items():
    current.setdefault(key, value)
path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
PY

mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
SETTINGS_PATH="$CLAUDE_SETTINGS" META_PATH="${CONFIG_BASE}/claude-statusline.json" \
STATUS_COMMAND="$STATUS_COMMAND" python3 - <<'PY'
import datetime, json, os
from pathlib import Path

settings_path = Path(os.environ["SETTINGS_PATH"])
meta_path = Path(os.environ["META_PATH"])
command = os.environ["STATUS_COMMAND"]
installed = {"type": "command", "command": command}
raw = b""
try:
    raw = settings_path.read_bytes()
except OSError:
    pass
malformed = False
try:
    settings = json.loads(raw) if raw else {}
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
    malformed = True
    settings = {}

try:
    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    old_meta = {}

current_status = settings.get("statusLine")
current_command = current_status.get("command") if isinstance(current_status, dict) else None
already_autoresume = isinstance(current_command, str) and current_command.endswith("/share/autoresume/current/statusline.py")
if (current_status == installed or already_autoresume) and isinstance(old_meta, dict) and old_meta:
    meta = old_meta
    meta["installed_status_line"] = installed
else:
    backup_path = None
    if raw:
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        backup = settings_path.with_name(f"settings.json.autoresume-backup-{stamp}")
        if not backup.exists():
            backup.write_bytes(raw)
        backup_path = str(backup)
    previous_command = current_status.get("command") if isinstance(current_status, dict) and current_status.get("type") == "command" else None
    meta = {
        "had_status_line": "statusLine" in settings,
        "previous_status_line": current_status,
        "previous_command": previous_command,
        "installed_status_line": installed,
        "settings_was_malformed": malformed,
        "backup_path": backup_path,
    }
settings["statusLine"] = installed
settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
meta_path.chmod(0o600)
PY

if [ "$ALIASES" -eq 1 ]; then
  while IFS= read -r file; do
    [ -n "$file" ] || continue
    mkdir -p "$(dirname "$file")"
    touch "$file"
    remove_alias_block "$file"
    cat >> "$file" <<'EOF'
# >>> AutoResume >>>
alias codex='autoresume run codex'
alias claude='autoresume run claude'
# <<< AutoResume <<<
EOF
  done <<EOF
$(startup_files)
EOF
fi

echo "Installed AutoResume ${VERSION} to ${INSTALL_DIR}"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "Add ${BIN_DIR} to PATH, then restart your shell." ;;
esac
if [ "$ALIASES" -eq 1 ]; then
  echo "Restart your shell (or source its startup file) to activate codex/claude supervision."
fi
