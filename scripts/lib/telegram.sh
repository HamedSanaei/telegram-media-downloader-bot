# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Telegram-facing wizards. All configuration mutation and validation goes
# through the typed application CLI (config-edit); this file is only a
# frontend. Secrets are read through stdin and never echoed or logged.
# --------------------------------------------------------------------------- #

config_edit_run() {
  # Runs the typed config-edit CLI against the mounted workspace. -i keeps
  # stdin open so secrets can be piped without appearing in process args.
  docker run --rm -i --user "$(id -u):$(id -g)" \
    -v "$ROOT_DIR:/workspace" -w /workspace \
    "$(configured_image)" telegram-media-bot config-edit \
    --config /workspace/config.yaml "$@"
}

compose_run_app() {
  compose --profile local-api run --rm --no-deps worker "$@"
}

hidden_input() {
  local prompt="$1" value
  if is_tty; then
    read -r -s -p "$prompt" value || return 1
    printf '\n'
  else
    read -r value || return 1
  fi
  printf '%s' "$value"
}

# Telegram setup --------------------------------------------------------
telegram_setup_token() {
  local token
  token="$(hidden_input "Enter bot token (input hidden): ")" || return 1
  [[ -n "$token" ]] || {
    echo "Token cannot be empty." >&2
    return 1
  }
  printf '%s' "$token" | config_edit_run set telegram.bot_token - || return 1
  echo "Bot token updated (value never displayed)."
}

telegram_test_connection() {
  config_edit_run telegram-status --probe
}

telegram_show_status() {
  config_edit_run telegram-status
}

telegram_admin_list() {
  config_edit_run get telegram.admin_ids
}

telegram_admin_add() {
  local admin_id="${1:-}"
  if [[ -z "$admin_id" ]]; then
    read -r -p "Admin user ID: " admin_id || return 1
  fi
  [[ "$admin_id" =~ ^-?[0-9]+$ ]] || {
    echo "Admin ID must be an integer." >&2
    return 1
  }
  config_edit_run list-add telegram.admin_ids "$admin_id"
}

telegram_admin_remove() {
  local admin_id="${1:-}"
  if [[ -z "$admin_id" ]]; then
    read -r -p "Admin user ID to remove: " admin_id || return 1
  fi
  config_edit_run list-remove telegram.admin_ids "$admin_id"
}

telegram_set_support_username() {
  local username
  read -r -p "Support username (without @, empty to clear): " username || return 1
  config_edit_run set telegram.support_username "${username:-}"
}

telegram_set_polling_timeout() {
  local seconds
  read -r -p "Polling timeout seconds (5-60): " seconds || return 1
  config_edit_run set telegram.polling_timeout_seconds "$seconds"
}

# Required channels -----------------------------------------------------
channels_status() {
  config_edit_run channel-status "${1:+--probe}"
}

channels_enable() {
  config_edit_run set telegram.required_channels.enabled true
}

channels_disable() {
  config_edit_run set telegram.required_channels.enabled false
}

channels_add() {
  local chat_id title join_url
  read -r -p "Channel chat ID (numeric, e.g. -100...): " chat_id || return 1
  read -r -p "Channel title: " title || return 1
  read -r -p "Channel join URL (https://t.me/...): " join_url || return 1
  [[ "$chat_id" =~ ^-?[0-9]+$ ]] || {
    echo "Chat ID must be an integer." >&2
    return 1
  }
  config_edit_run channel-add --chat-id "$chat_id" --title "$title" --join-url "$join_url"
}

channels_remove() {
  local chat_id="${1:-}"
  if [[ -z "$chat_id" ]]; then
    read -r -p "Channel chat ID to remove: " chat_id || return 1
  fi
  config_edit_run channel-remove "$chat_id"
}

channels_update() {
  local chat_id title join_url
  read -r -p "Channel chat ID to update: " chat_id || return 1
  read -r -p "New title (empty to keep): " title || return 1
  read -r -p "New join URL (empty to keep): " join_url || return 1
  local -a arguments=(channel-update "$chat_id")
  [[ -n "$title" ]] && arguments+=(--title "$title")
  [[ -n "$join_url" ]] && arguments+=(--join-url "$join_url")
  if [[ ${#arguments[@]} -eq 2 ]]; then
    echo "Nothing to update." >&2
    return 1
  fi
  config_edit_run "${arguments[@]}"
}

# Operator Logger -------------------------------------------------------
logger_status() {
  config_edit_run logger-status
}

logger_enable() {
  config_edit_run set telegram.logger.enabled true
  logger_status
}

logger_disable() {
  config_edit_run set telegram.logger.enabled false
  logger_status
}

logger_add_destination() {
  local chat_id="${1:-}"
  if [[ -z "$chat_id" ]]; then
    read -r -p "Logger destination channel ID (numeric -100...): " chat_id || return 1
  fi
  config_edit_run logger-add "$chat_id"
}

logger_remove_destination() {
  local chat_id="${1:-}"
  if [[ -z "$chat_id" ]]; then
    read -r -p "Logger destination channel ID to remove: " chat_id || return 1
  fi
  config_edit_run logger-remove "$chat_id"
}

logger_set_flag() {
  local key="$1" value="${2:-}"
  if [[ -z "$value" ]]; then
    read -r -p "$key (true/false): " value || return 1
  fi
  config_edit_run set "telegram.logger.$key" "$value"
}

logger_list_destinations() {
  config_edit_run get telegram.logger.channels
}

# Local Bot API ---------------------------------------------------------
local_api_status() {
  # Runtime reachability and migration state only exist inside the Compose
  # application context (service hostname, /data mounts); a bare container
  # would report cloud/unreachable for a healthy service.
  compose_run_app telegram-media-bot config-edit \
    --config /app/config.yaml local-api-status
}

local_api_configure() {
  local api_id api_hash mode host port
  echo "Configuring Local Bot API credentials and endpoint."
  read -r -p "API ID (numeric): " api_id || return 1
  [[ "$api_id" =~ ^[0-9]+$ ]] || {
    echo "API ID must be a positive integer." >&2
    return 1
  }
  api_hash="$(hidden_input "API hash (input hidden): ")" || return 1
  [[ -n "$api_hash" ]] || {
    echo "API hash cannot be empty." >&2
    return 1
  }
  read -r -p "Mode (managed/external, default managed): " mode || return 1
  mode="${mode:-managed}"
  case "$mode" in
    managed|external) ;;
    *)
      echo "Mode must be managed or external." >&2
      return 1
      ;;
  esac
  read -r -p "Listen host (default 0.0.0.0): " host || return 1
  host="${host:-0.0.0.0}"
  read -r -p "Port (default 8081): " port || return 1
  port="${port:-8081}"
  [[ "$port" =~ ^[0-9]+$ ]] || {
    echo "Port must be numeric." >&2
    return 1
  }
  config_edit_run set telegram.local_bot_api.api_id "$api_id" || return 1
  printf '%s' "$api_hash" | config_edit_run set telegram.local_bot_api.api_hash - || return 1
  config_edit_run set telegram.local_bot_api.mode "$mode" || return 1
  config_edit_run set telegram.local_bot_api.host "$host" || return 1
  config_edit_run set telegram.local_bot_api.port "$port" || return 1
  config_edit_run set telegram.local_bot_api.enabled true || return 1
  config_edit_run set telegram.local_api_base_url "http://local-api:${port}" || return 1
  config_edit_run set telegram.local_api_is_local true || return 1
  echo "Local Bot API configuration written. Start it from the service menu and test with tmb local-api test."
}

local_api_migrate_to_local() {
  echo "Migration to Local Bot API stops the bot and worker, migrates, then restores them."
  if [[ "${TMB_ASSUME_YES:-0}" != "1" ]]; then
    require_confirmation "MIGRATE-TO-LOCAL" || return 1
  fi
  compose --profile local-api stop -t 45 bot worker >/dev/null 2>&1 || true
  compose --profile local-api run --rm --no-deps bot \
    telegram-media-bot local-api --config /app/config.yaml migrate-to-local --yes || {
    compose --profile local-api up -d --no-build bot worker || true
    return 1
  }
  compose --profile local-api up -d --no-build bot worker
}

local_api_migrate_to_cloud() {
  echo "Migration to Cloud Bot API stops the bot and worker, migrates, then restores them."
  if [[ "${TMB_ASSUME_YES:-0}" != "1" ]]; then
    require_confirmation "MIGRATE-TO-CLOUD" || return 1
  fi
  compose --profile local-api stop -t 45 bot worker >/dev/null 2>&1 || true
  compose --profile local-api run --rm --no-deps bot \
    telegram-media-bot local-api --config /app/config.yaml migrate-to-cloud --yes || {
    compose --profile local-api up -d --no-build bot worker || true
    return 1
  }
  compose --profile local-api up -d --no-build bot worker
}

# Cookies ---------------------------------------------------------------
cookies_status() {
  config_edit_run cookie-status
}

cookies_replace_file() {
  local source="${1:-}"
  if [[ -z "$source" ]]; then
    read -r -p "Path to Netscape cookies file: " source || return 1
  fi
  [[ -f "$source" ]] || {
    echo "Cookie file not found: $source" >&2
    return 1
  }
  mkdir -p data/cookies
  local target="$ROOT_DIR/data/cookies/cookies.txt"
  if [[ -f "$target" ]]; then
    cp -p "$target" "$target.pre-replace-$(date -u +%Y%m%dT%H%M%SZ)" 2>/dev/null || true
  fi
  install -m 600 "$source" "$target"
  echo "Cookie file replaced at $target (mode 0600)."
  cookies_status
}

# Sessions / authentication status -------------------------------------
# The supported runtime is Bot API (cloud or Local Bot API). MTProto/Telethon
# user sessions were removed from the architecture; this reports the actual
# authentication surface honestly instead of faking a session command.
sessions_status() {
  cat <<'EOF'
Telegram authentication surface
-------------------------------
This installation authenticates through a Telegram BOT token only.
- Cloud Bot API: polling against api.telegram.org
- Local Bot API: the bot/worker use the managed local-api service

User-session (MTProto/Telethon/Premium) delivery is NOT part of the current
architecture: there is no phone login, no session file, and no staging
channel. No user credentials can be stored or restored.

Current state:
EOF
  config_edit_run telegram-status
  echo
  echo "Local Bot API:"
  config_edit_run local-api-status || true
}