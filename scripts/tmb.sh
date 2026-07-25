#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
RELEASE_ROOT="https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
ARCHIVE_NAME="telegram-media-downloader-bot.tar.gz"
IMAGE_REPOSITORY="ghcr.io/hamedsanaei/telegram-media-downloader-bot"
cd "$ROOT_DIR"

release_url() {
  if [[ -n "${TMB_RELEASE_TAG:-}" ]]; then
    printf '%s/download/%s/%s' "$RELEASE_ROOT" "$TMB_RELEASE_TAG" "$1"
  else
    printf '%s/latest/download/%s' "$RELEASE_ROOT" "$1"
  fi
}

prepare_verified_release() {
  RELEASE_TEMPORARY_DIRECTORY="$(mktemp -d)" || return 1
  [[ -n "$RELEASE_TEMPORARY_DIRECTORY" ]] || return 1
  RELEASE_STAGING_DIRECTORY="$RELEASE_TEMPORARY_DIRECTORY/extracted"
  mkdir -p "$RELEASE_STAGING_DIRECTORY" || return 1
  curl -fsSL "$(release_url "$ARCHIVE_NAME")" \
    -o "$RELEASE_TEMPORARY_DIRECTORY/$ARCHIVE_NAME" || return 1
  curl -fsSL "$(release_url "$ARCHIVE_NAME.sha256")" \
    -o "$RELEASE_TEMPORARY_DIRECTORY/$ARCHIVE_NAME.sha256" || return 1
  if ! (
    cd "$RELEASE_TEMPORARY_DIRECTORY"
    sha256sum --check --status "$ARCHIVE_NAME.sha256"
  ); then
    echo "Release checksum verification failed." >&2
    return 1
  fi
  tar -xzf "$RELEASE_TEMPORARY_DIRECTORY/$ARCHIVE_NAME" \
    -C "$RELEASE_STAGING_DIRECTORY" --strip-components=1 || return 1
  RELEASE_VERSION="$(
    sed -n 's/^version = "\([^"]*\)"/\1/p' \
      "$RELEASE_STAGING_DIRECTORY/pyproject.toml" | head -n 1
  )"
  [[ -n "$RELEASE_VERSION" ]] || {
    echo "Unable to determine verified release version." >&2
    return 1
  }
  [[ -f "$RELEASE_STAGING_DIRECTORY/docker-compose.yml" ]] || {
    echo "Verified release is missing docker-compose.yml." >&2
    return 1
  }
}

install_prepared_release() {
  cp -a "$RELEASE_STAGING_DIRECTORY/." "$ROOT_DIR/"
}

cleanup_prepared_release() {
  if [[ -n "${RELEASE_TEMPORARY_DIRECTORY:-}" ]]; then
    rm -rf -- "$RELEASE_TEMPORARY_DIRECTORY" || true
  fi
}

set_configured_image() {
  local image="$1"
  if grep -q '^TMB_IMAGE=' .env; then
    sed -i "s|^TMB_IMAGE=.*|TMB_IMAGE=$image|" .env
  else
    echo "TMB_IMAGE=$image" >> .env
  fi
}

compose() {
  docker compose --project-directory "$ROOT_DIR" "$@"
}

configured_image() {
  local image
  image=""
  if [[ -f "$ROOT_DIR/.env" ]]; then
    image="$(sed -n 's/^TMB_IMAGE=//p' "$ROOT_DIR/.env" | head -n 1)"
  fi
  printf '%s' "${TMB_IMAGE:-${image:-ghcr.io/hamedsanaei/telegram-media-downloader-bot:latest}}"
}

backup() {
  mkdir -p backups
  local stamp item
  local -a backup_items=(config.yaml .env)
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  for item in data/state data/cookies data/telegram-bot-api; do
    if [[ -e "$item" ]]; then
      backup_items+=("$item")
    fi
  done
  tar -czf "backups/tmb-${stamp}.tar.gz" "${backup_items[@]}" || return 1
  echo "Backup created: backups/tmb-${stamp}.tar.gz"
}

load_running_application_services() {
  local service output
  output="$(compose --profile local-api ps --services --filter status=running)" || return 1
  PREVIOUS_SERVICES=()
  while IFS= read -r service; do
    case "$service" in
      bot|worker|local-api) PREVIOUS_SERVICES+=("$service") ;;
    esac
  done <<<"$output"
}

start_services() {
  local recreate="$1"
  shift
  if [[ $# -eq 0 ]]; then
    return
  fi
  if [[ "$recreate" == "true" ]]; then
    compose --profile local-api up -d --no-build --force-recreate "$@"
  else
    compose --profile local-api up -d --no-build "$@"
  fi
}

menu() {
  cat <<'EOF'
Telegram Media Bot
1) Start
2) Stop
3) Restart
4) Status
5) Logs
6) Doctor
7) Configure
8) Update
9) Backup
0) Exit
EOF
  read -r -p "Select: " choice
  case "$choice" in
    1) run start ;;
    2) run stop ;;
    3) run restart ;;
    4) run status ;;
    5) run logs ;;
    6) run doctor ;;
    7) run config ;;
    8) run update ;;
    9) run backup ;;
    0) exit 0 ;;
    *) echo "Invalid selection" >&2; exit 2 ;;
  esac
}

run() {
  case "${1:-}" in
    start) compose --profile local-api up -d --no-build ;;
    stop) compose --profile local-api down ;;
    restart) compose --profile local-api up -d --no-build --force-recreate ;;
    status) compose --profile local-api ps ;;
    logs)
      if [[ -n "${2:-}" ]]; then
        compose --profile local-api logs -f "$2"
      else
        compose --profile local-api logs -f
      fi
      ;;
    doctor)
      compose --profile local-api run --rm --no-deps worker \
        telegram-media-bot doctor --config /app/config.yaml
      ;;
    config)
      docker run --rm -it \
        -v "$ROOT_DIR:/workspace" -w /workspace \
        "$(configured_image)" \
        telegram-media-bot configure --config /workspace/config.yaml
      ;;
    update)
      local previous_image
      previous_image="$(configured_image)"
      PREVIOUS_SERVICES=()
      load_running_application_services
      if [[ ${#PREVIOUS_SERVICES[@]} -gt 0 ]]; then
        compose --profile local-api stop -t 45 "${PREVIOUS_SERVICES[@]}"
      fi
      RELEASE_TEMPORARY_DIRECTORY=""
      if backup \
        && prepare_verified_release \
        && set_configured_image "$IMAGE_REPOSITORY:$RELEASE_VERSION" \
        && compose --profile local-api pull \
        && install_prepared_release; then
        cleanup_prepared_release
        start_services true "${PREVIOUS_SERVICES[@]}"
      else
        cleanup_prepared_release
        set_configured_image "$previous_image"
        echo "Update failed; restoring the prior image and restarting the stack." >&2
        start_services false "${PREVIOUS_SERVICES[@]}"
        return 1
      fi
      ;;
    backup) backup ;;
    uninstall)
      compose --profile local-api down
      read -r -p "Delete config and data too? Type DELETE to confirm: " answer
      if [[ "$answer" == "DELETE" ]]; then
        rm -rf -- "$ROOT_DIR/data"
        rm -f -- "$ROOT_DIR/config.yaml"
      fi
      echo "Services stopped. Remove $ROOT_DIR manually when no longer needed."
      ;;
    *) echo "Usage: tmb start|stop|restart|status|logs [service]|doctor|config|update|backup|uninstall" >&2; exit 2 ;;
  esac
}

if [[ $# -eq 0 ]]; then
  menu
else
  run "$@"
fi
