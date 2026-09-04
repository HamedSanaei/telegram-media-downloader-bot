# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Common helpers and security utilities shared by every tmb library.
# All variables are expected to be set -euo pipefail safe at the call site.
# --------------------------------------------------------------------------- #

# Paths ----------------------------------------------------------------
# ROOT_DIR and SCRIPT_DIRECTORY are resolved by the tmb.sh entrypoint before
# sourcing this file. They are intentionally NOT re-resolved here so the
# isolated update runner (a copy of tmb.sh next to a copied lib/) resolves
# them from its own location.
[[ -n "${ROOT_DIR:-}" ]] || ROOT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
[[ -n "${SCRIPT_DIRECTORY:-}" ]] || SCRIPT_DIRECTORY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
[[ -n "${RELEASE_ROOT:-}" ]] || RELEASE_ROOT="https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
[[ -n "${ARCHIVE_NAME:-}" ]] || ARCHIVE_NAME="telegram-media-downloader-bot.tar.gz"
# Repository override contract: privileged/offline integration harnesses stage releases in a
# local registry and pass TMB_IMAGE_REPOSITORY; production never sets it and keeps the
# canonical GHCR default. The override keeps candidate pulls on the staging registry even
# before the release image is published.
[[ -n "${TMB_IMAGE_REPOSITORY:-}" ]] && IMAGE_REPOSITORY="$TMB_IMAGE_REPOSITORY"
[[ -n "${IMAGE_REPOSITORY:-}" ]] || IMAGE_REPOSITORY="ghcr.io/hamedsanaei/telegram-media-downloader-bot"
[[ -n "${TMB_BIN_DIR:-}" ]] || TMB_BIN_DIR="/usr/local/bin"
[[ -n "${UPDATE_HEALTH_TIMEOUT_SECONDS:-}" ]] || UPDATE_HEALTH_TIMEOUT_SECONDS=180
# Shared service lists and policy snapshots below are consumed by several
# libraries (services/status/backup/update/restore) after sourcing, so
# ShellCheck's per-file SC2034 analysis cannot see their readers.
# shellcheck disable=SC2034
PROJECT_SERVICES=(bot worker local-api redis)
# shellcheck disable=SC2034
FILESYSTEM_WRITER_SERVICES=(bot worker local-api)
# Standalone bootstrap snapshot; tests enforce parity with release-policy.json.
# shellcheck disable=SC2034
readonly -a BLOCKED_RELEASE_VERSIONS=("1.3.7")
cd "$ROOT_DIR" || exit 1

# TTY / confirmation ---------------------------------------------------
is_tty() {
  [[ -t 1 && -t 0 ]]
}

tmb_require_command() {
  local command="$1"
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command not found: $command" >&2
    return 1
  fi
}

# Require an exact typed phrase before a destructive action.
#   require_confirmation DELETE-DOWNLOADS
# Non-interactive callers must set TMB_ASSUME_YES=1 (the --yes flag) after
# reviewing; consent is never silently inferred from the absence of a TTY.
require_confirmation() {
  local phrase="$1"
  if [[ "${TMB_ASSUME_YES:-0}" == "1" ]]; then
    return 0
  fi
  if ! is_tty; then
    echo "Refusing: confirmation required but no interactive terminal is available." >&2
    echo "Re-run with --yes after reviewing what will happen." >&2
    return 1
  fi
  local answer
  read -r -p "Type $phrase to continue: " answer || return 1
  [[ "$answer" == "$phrase" ]]
}

# Management lock ------------------------------------------------------
# State-mutating operations (update, restore, backup create, cleanup,
# migration import, uninstall) take an exclusive lock so concurrent dangerous
# operations cannot race. Read-only commands never lock.
TMB_LOCK_OWNED=0
TMB_LOCK_MODE=""

acquire_management_lock() {
  if [[ "${TMB_NO_LOCK:-0}" == "1" ]]; then
    return 0
  fi
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$ROOT_DIR/.tmb.lock" || return 1
    if ! flock -n 9; then
      echo "Another tmb management operation is already running (lock: $ROOT_DIR/.tmb.lock)." >&2
      echo "Wait for it to finish; the lock is released automatically when the process exits." >&2
      return 1
    fi
    TMB_LOCK_OWNED=1
    TMB_LOCK_MODE="flock"
    return 0
  fi
  # Portable fallback for hosts without flock: atomic mkdir with stale-pid recovery.
  local lock_dir="$ROOT_DIR/.tmb-lock" pid
  if mkdir "$lock_dir" 2>/dev/null; then
    printf '%s\n' "$$" >"$lock_dir/pid"
    TMB_LOCK_OWNED=1
    TMB_LOCK_MODE="mkdir"
    trap release_management_lock EXIT INT TERM HUP
    return 0
  fi
  if [[ -f "$lock_dir/pid" ]]; then
    pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      rm -rf -- "$lock_dir" 2>/dev/null || true
      if mkdir "$lock_dir" 2>/dev/null; then
        printf '%s\n' "$$" >"$lock_dir/pid"
        TMB_LOCK_OWNED=1
        TMB_LOCK_MODE="mkdir"
        trap release_management_lock EXIT INT TERM HUP
        return 0
      fi
    fi
  fi
  echo "Another tmb management operation is already running (lock: $lock_dir)." >&2
  return 1
}

release_management_lock() {
  [[ "${TMB_LOCK_OWNED:-0}" == "1" ]] || return 0
  TMB_LOCK_OWNED=0
  if [[ "$TMB_LOCK_MODE" == "flock" ]]; then
    flock -u 9 2>/dev/null || true
    exec 9>&- || true
  else
    rm -rf -- "$ROOT_DIR/.tmb-lock" 2>/dev/null || true
  fi
}

# Central secret redaction -------------------------------------------------
# Every diagnostic/bundle/log pipeline funnels through this filter so secret
# handling is implemented once. It redacts credential-looking lines and
# credentials embedded in URLs while preserving useful safe diagnostics.

# Redacts credential-bearing substrings from arbitrary strings: Telegram bot
# tokens (<6-10 digits>:<long alnum run>), api-hash-like standalone 32-hex
# runs (never inside longer hex runs such as sha256 digests), and URL userinfo.
# Display-only: it never modifies files or archives. Implemented with bash
# parameter expansion so it is portable (no awk backreference support needed).
redact_string() {
  local line match prefix
  while IFS= read -r line; do
    while [[ "$line" =~ [0-9]{6,10}:[A-Za-z0-9_-]{10,} ]]; do
      match="${BASH_REMATCH[0]}"
      line="${line//"$match"/[redacted-token]}"
    done
    while [[ "$line" =~ https?://[^/@:[:space:]]+:[^/@[:space:]]+@ ]]; do
      match="${BASH_REMATCH[0]}"
      line="${line//"$match"/${match%%://*}://[redacted]@}"
    done
    # Standalone 32-hex runs (api-hash-like) are redacted; runs adjacent to
    # other hex characters (e.g. sha256 digests) are preserved intact.
    while [[ "$line" =~ (^|[^0-9a-fA-F])([0-9a-fA-F]{32})([^0-9a-fA-F]|$) ]]; do
      prefix="${line%%"${BASH_REMATCH[0]}"*}"
      line="${prefix}${BASH_REMATCH[1]}[redacted-hash]${BASH_REMATCH[3]}${line:${#prefix}+${#BASH_REMATCH[0]}}"
    done
    printf '%s\n' "$line"
  done
}

sanitize_stream() {
  awk '
    {
      line = $0
      lower = tolower(line)
      if (lower ~ /(bot[_-]?token|api[_-]?hash|authorization|password|(^|[^[:alpha:]])secret([^[:alpha:]]|$)|cookie[^[:space:]]*(value|content)|tmb_[a-z0-9_]*=)/ ||
          lower ~ /^[^[:space:]]+[[:space:]]+(true|false)([[:space:]]+[^[:space:]]+){5}/) {
        print "[redacted sensitive line]"
        next
      }
      gsub(/https:\/\/[^\/@:[:space:]]+:[^\/@[:space:]]+@/, "https://[redacted]@", line)
      gsub(/http:\/\/[^\/@:[:space:]]+:[^\/@[:space:]]+@/, "http://[redacted]@", line)
      print substr(line, 1, 500)
    }
  ' | redact_string
}

# Sanitized single command output; $1 names the stage for the message.
run_redacted() {
  local stage="$1" output_file status
  shift
  output_file="$(mktemp "${TMPDIR:-/tmp}/tmb-stage.XXXXXX")" || return 1
  if "$@" >"$output_file" 2>&1; then
    rm -f -- "$output_file"
    return 0
  else
    status=$?
  fi
  echo "Stage failed: $stage" >&2
  if [[ -s "$output_file" ]]; then
    echo "Sanitized diagnostic output (last 40 lines):" >&2
    tail -n 40 "$output_file" | sanitize_stream >&2
  fi
  rm -f -- "$output_file"
  return "$status"
}

# Sizes ----------------------------------------------------------------
human_size() {
  local bytes="$1"
  if [[ ! "$bytes" =~ ^[0-9]+$ ]]; then
    printf 'n/a'
    return 0
  fi
  if ((bytes >= 1073741824)); then
    awk -v b="$bytes" 'BEGIN { printf "%.1fG", b / 1073741824 }'
  elif ((bytes >= 1048576)); then
    awk -v b="$bytes" 'BEGIN { printf "%.1fM", b / 1048576 }'
  elif ((bytes >= 1024)); then
    awk -v b="$bytes" 'BEGIN { printf "%.1fK", b / 1024 }'
  else
    printf '%sB' "$bytes"
  fi
}

dir_size_bytes() {
  local path="$1" result
  if [[ ! -e "$path" ]]; then
    printf '0'
    return 0
  fi
  result="$(du -sb -- "$path" 2>/dev/null | cut -f1 || true)"
  if [[ "$result" =~ ^[0-9]+$ ]]; then
    printf '%s' "$result"
  else
    printf 'unknown'
  fi
}

file_size_bytes() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf '0'
    return 0
  fi
  stat -c '%s' "$path" 2>/dev/null | grep -E '^[0-9]+$' || printf '0'
}

# Config display helpers (read-only booleans for the dashboard) --------
# These are display-only reads; configuration validation and mutation stay in
# the typed application CLI (telegram-media-bot config-edit).
config_flag() {
  # $1 = single-line grep pattern whose second field is true/false.
  local pattern="$1"
  grep -E "$pattern" "$ROOT_DIR/config.yaml" 2>/dev/null | head -n 1 | grep -Eq ': *true'
}

config_has_line() {
  local pattern="$1"
  grep -Eq "$pattern" "$ROOT_DIR/config.yaml" 2>/dev/null
}

config_bot_token_configured() {
  config_has_line '^  bot_token: *[^ C]' || config_has_line '^  bot_token: *"'
}

installed_version() {
  sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -n 1
}