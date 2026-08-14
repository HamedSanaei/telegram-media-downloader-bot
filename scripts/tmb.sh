#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT_DIR="${TMB_ROOT_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)}"
RELEASE_ROOT="${TMB_RELEASE_ROOT:-https://github.com/HamedSanaei/telegram-media-downloader-bot/releases}"
ARCHIVE_NAME="telegram-media-downloader-bot.tar.gz"
IMAGE_REPOSITORY="${TMB_IMAGE_REPOSITORY:-ghcr.io/hamedsanaei/telegram-media-downloader-bot}"
TMB_BIN_DIR="${TMB_BIN_DIR:-/usr/local/bin}"
UPDATE_HEALTH_TIMEOUT_SECONDS="${TMB_UPDATE_HEALTH_TIMEOUT_SECONDS:-180}"
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
  local script
  for script in \
    install.sh \
    manage.sh \
    scripts/tmb.sh \
    scripts/build_release_archives.sh \
    scripts/tests/test_tmb_update.sh \
    scripts/tests/test_tmb_upgrade_integration.sh; do
    [[ -f "$RELEASE_STAGING_DIRECTORY/$script" ]] || {
      echo "Verified release is missing $script." >&2
      return 1
    }
    bash -n "$RELEASE_STAGING_DIRECTORY/$script" || {
      echo "Verified release contains invalid Bash syntax in $script." >&2
      return 1
    }
  done
  chmod 755 \
    "$RELEASE_STAGING_DIRECTORY/install.sh" \
    "$RELEASE_STAGING_DIRECTORY/manage.sh" \
    "$RELEASE_STAGING_DIRECTORY/scripts/tmb.sh" \
    "$RELEASE_STAGING_DIRECTORY/scripts/build_release_archives.sh" \
    "$RELEASE_STAGING_DIRECTORY/scripts/tests/test_tmb_update.sh" \
    "$RELEASE_STAGING_DIRECTORY/scripts/tests/test_tmb_upgrade_integration.sh" || return 1
}

validate_prepared_release() {
  local prepared_image="$IMAGE_REPOSITORY:$RELEASE_VERSION" uid gid
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  docker compose \
    --project-directory "$ROOT_DIR" \
    --env-file "$ROOT_DIR/.env" \
    -f "$RELEASE_STAGING_DIRECTORY/docker-compose.yml" \
    --profile local-api config >/dev/null || {
    echo "Verified release contains an invalid Compose definition." >&2
    return 1
  }
  docker pull "$prepared_image" >/dev/null || {
    echo "Unable to pull the prepared release image for validation." >&2
    return 1
  }
  docker run --rm --read-only --user "$uid:$gid" \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777 \
    -v "$ROOT_DIR/config.yaml:/app/config.yaml:ro" \
    -v "$ROOT_DIR/data:/data:ro" \
    "$prepared_image" \
    telegram-media-bot config-check --config /app/config.yaml \
      --read-only-runtime >/dev/null || {
    echo "Existing config.yaml is not valid for the prepared release." >&2
    return 1
  }
}

prepare_application_transaction() {
  APPLICATION_TRANSACTION_DIRECTORY="$(mktemp -d "$ROOT_DIR/.tmb-update.XXXXXX")" || return 1
  APPLICATION_ROLLBACK_DIRECTORY="$APPLICATION_TRANSACTION_DIRECTORY/rollback-application"
  APPLICATION_NEXT_DIRECTORY="$APPLICATION_TRANSACTION_DIRECTORY/next-application"
  mkdir -p "$APPLICATION_ROLLBACK_DIRECTORY" "$APPLICATION_NEXT_DIRECTORY"
  cp -a "$RELEASE_STAGING_DIRECTORY/." "$APPLICATION_NEXT_DIRECTORY/"
  APPLICATION_ENTRIES=()
}

is_persistent_entry() {
  case "$1" in
    .env|config.yaml|data|backups) return 0 ;;
    *) return 1 ;;
  esac
}

install_prepared_release() {
  local source name
  while IFS= read -r -d '' source; do
    name="${source##*/}"
    is_persistent_entry "$name" && continue
    [[ "$name" != "." && "$name" != ".." && "$name" != */* ]] || return 1
    if [[ -e "$ROOT_DIR/$name" || -L "$ROOT_DIR/$name" ]]; then
      if ! mv "$ROOT_DIR/$name" "$APPLICATION_ROLLBACK_DIRECTORY/$name"; then
        cp -a "$ROOT_DIR/$name" "$APPLICATION_ROLLBACK_DIRECTORY/$name" || return 1
        rm -rf -- "${ROOT_DIR:?}/$name" || return 1
      fi
    fi
    if ! mv "$source" "$ROOT_DIR/$name"; then
      if [[ -e "$APPLICATION_ROLLBACK_DIRECTORY/$name" ]]; then
        mv "$APPLICATION_ROLLBACK_DIRECTORY/$name" "$ROOT_DIR/$name" || true
      fi
      return 1
    fi
    APPLICATION_ENTRIES+=("$name")
  done < <(find "$APPLICATION_NEXT_DIRECTORY" -mindepth 1 -maxdepth 1 -print0)
  chmod 755 \
    "$ROOT_DIR/install.sh" \
    "$ROOT_DIR/manage.sh" \
    "$ROOT_DIR/scripts/tmb.sh" \
    "$ROOT_DIR/scripts/build_release_archives.sh" \
    "$ROOT_DIR/scripts/tests/test_tmb_update.sh" \
    "$ROOT_DIR/scripts/tests/test_tmb_upgrade_integration.sh"
}

rollback_application_files() {
  local index name
  for ((index=${#APPLICATION_ENTRIES[@]} - 1; index >= 0; index--)); do
    name="${APPLICATION_ENTRIES[$index]}"
    [[ "$name" != "." && "$name" != ".." && "$name" != */* ]] || continue
    rm -rf -- "${ROOT_DIR:?}/$name"
    if [[ -e "$APPLICATION_ROLLBACK_DIRECTORY/$name" || \
      -L "$APPLICATION_ROLLBACK_DIRECTORY/$name" ]]; then
      mv "$APPLICATION_ROLLBACK_DIRECTORY/$name" "$ROOT_DIR/$name"
    fi
  done
  APPLICATION_ENTRIES=()
}

cleanup_prepared_release() {
  if [[ -n "${RELEASE_TEMPORARY_DIRECTORY:-}" ]]; then
    rm -rf -- "$RELEASE_TEMPORARY_DIRECTORY" || true
  fi
  case "${APPLICATION_TRANSACTION_DIRECTORY:-}" in
    "$ROOT_DIR"/.tmb-update.*)
      rm -rf -- "$APPLICATION_TRANSACTION_DIRECTORY" || true
      ;;
  esac
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

runtime_identity() {
  local name="$1" fallback="$2" value
  value="${!name:-}"
  if [[ -z "$value" && -f "$ROOT_DIR/.env" ]]; then
    value="$(sed -n "s/^${name}=//p" "$ROOT_DIR/.env" | head -n 1)"
  fi
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    printf '%s' "$value"
  else
    printf '%s' "$fallback"
  fi
}

normalize_runtime_permissions() {
  local image="$1" uid gid
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  if ! docker run --rm --user 0 --entrypoint sh \
    -e "APP_UID=$uid" -e "APP_GID=$gid" \
    -v "$ROOT_DIR:/workspace" "$image" -c '
      set -eu
      for path in \
        /workspace/data/state \
        /workspace/data/downloads \
        /workspace/data/temp \
        /workspace/data/cookies \
        /workspace/data/telegram-bot-api \
        /workspace/backups; do
        mkdir -p "$path"
      done
      chown -R "$APP_UID:$APP_GID" \
        /workspace/data /workspace/backups
      find /workspace/data /workspace/backups -type d -exec chmod 700 {} +
      find /workspace/data /workspace/backups -type f -exec chmod 600 {} +
    '; then
    echo "Update cannot safely continue: runtime ownership/permission repair failed." >&2
    return 1
  fi
}

probe_runtime_writes() {
  local image="$1" uid gid database
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  database="/data/state/jobs.sqlite3"
  docker run --rm --user "$uid:$gid" --entrypoint sh \
    -v "$ROOT_DIR/data:/data" "$image" -c '
      set -eu
      test -w /data/state
      touch /data/state/.permission-probe
      rm /data/state/.permission-probe
      python - "$1" <<'"'"'PY'"'"'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        raise SystemExit(f"unable to enable SQLite WAL mode: {mode!r}")
finally:
    connection.close()
PY
    ' sh "$database" || {
    echo "Runtime write/SQLite WAL permission probe failed." >&2
    return 1
  }
}

repair_tmb_command() {
  local target="$ROOT_DIR/scripts/tmb.sh" link="$TMB_BIN_DIR/tmb"
  chmod 755 "$target" || return 1
  mkdir -p "$TMB_BIN_DIR" 2>/dev/null || true
  if [[ -d "$TMB_BIN_DIR" && -w "$TMB_BIN_DIR" ]]; then
    ln -sfn "$target" "$link"
  elif command -v sudo >/dev/null 2>&1; then
    sudo mkdir -p "$TMB_BIN_DIR" && sudo ln -sfn "$target" "$link"
  else
    echo "Unable to repair the global tmb command at $link." >&2
    return 1
  fi
  [[ "$(readlink -f "$link")" == "$(readlink -f "$target")" ]] || return 1
  PATH="$TMB_BIN_DIR:$PATH" command -v tmb >/dev/null || return 1
  [[ -x "$(readlink -f "$(PATH="$TMB_BIN_DIR:$PATH" command -v tmb)")" ]] || return 1
  PATH="$TMB_BIN_DIR:$PATH" tmb status >/dev/null
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

service_is_ready() {
  local service="$1" container state health
  container="$(compose --profile local-api ps -q "$service")"
  [[ -n "$container" ]] || return 1
  state="$(docker inspect --format '{{.State.Status}}' "$container")" || return 1
  case "$state" in
    exited|dead|restarting) return 2 ;;
    running) ;;
    *) return 1 ;;
  esac
  health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$container"
  )" || return 1
  [[ "$health" == "healthy" || "$health" == "none" ]]
}

verify_services_healthy() {
  local deadline service all_ready result
  [[ $# -gt 0 ]] || return 0
  deadline=$((SECONDS + UPDATE_HEALTH_TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    all_ready=true
    for service in "$@"; do
      if service_is_ready "$service"; then
        continue
      else
        result=$?
      fi
      if [[ "$result" -eq 2 ]]; then
        echo "Service $service entered a crash/restart state after update." >&2
        compose --profile local-api stop "$service" || true
        return 1
      fi
      all_ready=false
    done
    [[ "$all_ready" == "true" ]] && return 0
    sleep 5
  done
  echo "Updated services did not become healthy before the timeout." >&2
  compose --profile local-api stop "$@" || true
  return 1
}

verify_runtime_release() {
  local runtime_version
  runtime_version="$(
    compose --profile local-api run --rm --no-deps worker \
      python -c 'import telegram_media_bot; print(telegram_media_bot.__version__)'
  )" || return 1
  [[ "${runtime_version##*$'\n'}" == "$RELEASE_VERSION" ]] || {
    echo "Runtime version does not match release $RELEASE_VERSION." >&2
    return 1
  }
  compose --profile local-api run --rm --no-deps worker \
    telegram-media-bot doctor --config /app/config.yaml >/dev/null || return 1
  compose --profile local-api ps >/dev/null || return 1
}

project_image_cleanup_enabled() {
  local configured
  configured="$(
    docker run --rm \
      -v "$ROOT_DIR/config.yaml:/app/config.yaml:ro" \
      "$(configured_image)" \
      python -c '
from telegram_media_bot.bootstrap.config import load_settings
print(str(load_settings("/app/config.yaml").operations.update.prune_old_project_images_after_success).lower())
      ' 2>/dev/null
  )" || configured="true"
  [[ "${configured##*$'\n'}" != "false" ]]
}

cleanup_project_resources() {
  local dry_run="${1:-false}" current_image current_id container state image_id
  local repository image_size
  local -a project_containers=()
  local -a referenced_ids=()
  local -a project_ids=()
  local -a foreign_ids=()
  local -a candidate_ids=()
  local reclaimed=0

  current_image="$(configured_image)"
  current_id="$(docker image inspect --format '{{.Id}}' "$current_image")" || return 1

  while IFS= read -r container; do
    [[ -n "$container" ]] && project_containers+=("$container")
  done < <(compose --profile local-api ps -a -q)
  for container in "${project_containers[@]}"; do
    state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
    image_id="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
    if [[ "$state" != "running" && -n "$image_id" && "$image_id" != "$current_id" ]]; then
      if [[ "$dry_run" == "true" ]]; then
        echo "Would remove stopped project container: $container"
      else
        docker rm "$container" >/dev/null || return 1
      fi
    fi
  done

  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    image_id="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
    [[ -n "$image_id" ]] && referenced_ids+=("$image_id")
  done < <(docker ps -aq)

  while IFS='|' read -r repository image_id; do
    [[ -n "$image_id" ]] || continue
    if [[ "$repository" == "$IMAGE_REPOSITORY" ]]; then
      project_ids+=("$image_id")
    else
      foreign_ids+=("$image_id")
    fi
  done < <(docker image ls --no-trunc --format '{{.Repository}}|{{.ID}}')

  for image_id in "${project_ids[@]}"; do
    [[ "$image_id" == "$current_id" ]] && continue
    if printf '%s\n' "${referenced_ids[@]}" | grep -Fxq -- "$image_id"; then
      continue
    fi
    if printf '%s\n' "${foreign_ids[@]}" | grep -Fxq -- "$image_id"; then
      continue
    fi
    if ! printf '%s\n' "${candidate_ids[@]}" | grep -Fxq -- "$image_id"; then
      candidate_ids+=("$image_id")
    fi
  done

  for image_id in "${candidate_ids[@]}"; do
    image_size="$(docker image inspect --format '{{.Size}}' "$image_id" 2>/dev/null || printf '0')"
    [[ "$image_size" =~ ^[0-9]+$ ]] || image_size=0
    reclaimed=$((reclaimed + image_size))
    if [[ "$dry_run" == "true" ]]; then
      echo "Would remove old project image: $image_id"
    else
      docker image rm "$image_id" >/dev/null || return 1
    fi
  done
  echo "Project cleanup candidates: ${#candidate_ids[@]} image(s); approximate bytes: $reclaimed"
}

rollback_update() {
  local previous_image="$1"
  shift
  if [[ $# -gt 0 ]]; then
    compose --profile local-api stop "$@" >/dev/null 2>&1 || true
  fi
  set_configured_image "$previous_image"
  rollback_application_files || true
  normalize_runtime_permissions "$previous_image" || {
    echo "Rollback could not restore usable runtime permissions; services remain stopped." >&2
    return 1
  }
  repair_tmb_command || {
    echo "Rollback could not restore the tmb command; services remain stopped." >&2
    return 1
  }
  start_services false "$@" || return 1
  verify_services_healthy "$@" || return 1
}

perform_update() {
  local previous_image="$1"
  shift
  prepare_verified_release || return 1
  validate_prepared_release || return 1
  prepare_application_transaction || return 1
  backup || return 1
  if [[ $# -gt 0 ]]; then
    compose --profile local-api stop -t 45 "$@" || return 1
  fi
  UPDATE_STOPPED=true
  install_prepared_release || return 1
  normalize_runtime_permissions "$previous_image" || return 1
  probe_runtime_writes "$previous_image" || return 1
  set_configured_image "$IMAGE_REPOSITORY:$RELEASE_VERSION"
  compose --profile local-api pull || return 1
  start_services true "$@" || return 1
  verify_services_healthy "$@" || return 1
  repair_tmb_command || return 1
  verify_runtime_release || return 1
  if project_image_cleanup_enabled; then
    cleanup_project_resources false || {
      echo "Update succeeded, but old project image cleanup failed; retry with tmb cleanup." >&2
    }
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
10) Cleanup
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
    10) run cleanup ;;
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
      RELEASE_TEMPORARY_DIRECTORY=""
      APPLICATION_ENTRIES=()
      APPLICATION_TRANSACTION_DIRECTORY=""
      APPLICATION_ROLLBACK_DIRECTORY=""
      UPDATE_STOPPED=false
      load_running_application_services || return 1
      if perform_update "$previous_image" "${PREVIOUS_SERVICES[@]}"; then
        cleanup_prepared_release
        echo "Update to $RELEASE_VERSION completed successfully."
      else
        if [[ "$UPDATE_STOPPED" == "true" ]]; then
          echo "Update failed after service stop; rolling back application, image, and permissions." >&2
          rollback_update "$previous_image" "${PREVIOUS_SERVICES[@]}" || {
            cleanup_prepared_release
            return 1
          }
        else
          echo "Update validation failed before service stop; the installed release was unchanged." >&2
        fi
        cleanup_prepared_release
        return 1
      fi
      ;;
    cleanup)
      local dry_run=false
      local -a workspace_cleanup_arguments=(
        telegram-media-bot cleanup-workspaces --config /app/config.yaml
      )
      if [[ "${2:-}" == "--dry-run" ]]; then
        dry_run=true
        workspace_cleanup_arguments+=(--dry-run)
      elif [[ -n "${2:-}" ]]; then
        echo "Usage: tmb cleanup [--dry-run]" >&2
        return 2
      fi
      compose --profile local-api run --rm --no-deps worker \
        "${workspace_cleanup_arguments[@]}"
      cleanup_project_resources "$dry_run"
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
    *) echo "Usage: tmb start|stop|restart|status|logs [service]|doctor|config|update|backup|cleanup [--dry-run]|uninstall" >&2; exit 2 ;;
  esac
}

if [[ "${1:-}" == "update" && "${TMB_UPDATE_RUNNER:-0}" != "1" ]]; then
  UPDATE_RUNNER_PATH="$(mktemp)" || exit 1
  cp "$SCRIPT_PATH" "$UPDATE_RUNNER_PATH"
  chmod 700 "$UPDATE_RUNNER_PATH"
  if TMB_ROOT_DIR="$ROOT_DIR" \
    TMB_UPDATE_RUNNER=1 \
    TMB_UPDATE_RUNNER_PATH="$UPDATE_RUNNER_PATH" \
    bash "$UPDATE_RUNNER_PATH" update; then
    rm -f -- "$UPDATE_RUNNER_PATH"
  else
    result=$?
    rm -f -- "$UPDATE_RUNNER_PATH"
    exit "$result"
  fi
elif [[ $# -eq 0 ]]; then
  menu
else
  run "$@"
fi
