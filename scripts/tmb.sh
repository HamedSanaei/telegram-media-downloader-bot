#!/usr/bin/env bash
# --------------------------------------------------------------------------- #
# tmb - the single authoritative operator control plane for the Telegram
# Media Downloader Bot. This entrypoint only resolves paths, sources the
# shared libraries, dispatches commands, and hosts the interactive menus;
# all business logic lives in scripts/lib/.
# --------------------------------------------------------------------------- #
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIRECTORY="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
ROOT_DIR="${TMB_ROOT_DIR:-$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)}"

# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIRECTORY/lib/common.sh"
# shellcheck source=scripts/lib/ui.sh
source "$SCRIPT_DIRECTORY/lib/ui.sh"
# shellcheck source=scripts/lib/services.sh
source "$SCRIPT_DIRECTORY/lib/services.sh"
# shellcheck source=scripts/lib/update.sh
source "$SCRIPT_DIRECTORY/lib/update.sh"
# shellcheck source=scripts/lib/backup.sh
source "$SCRIPT_DIRECTORY/lib/backup.sh"
# shellcheck source=scripts/lib/restore.sh
source "$SCRIPT_DIRECTORY/lib/restore.sh"
# shellcheck source=scripts/lib/status.sh
source "$SCRIPT_DIRECTORY/lib/status.sh"
# shellcheck source=scripts/lib/storage.sh
source "$SCRIPT_DIRECTORY/lib/storage.sh"
# shellcheck source=scripts/lib/docker.sh
source "$SCRIPT_DIRECTORY/lib/docker.sh"
# shellcheck source=scripts/lib/logs.sh
source "$SCRIPT_DIRECTORY/lib/logs.sh"
# shellcheck source=scripts/lib/telegram.sh
source "$SCRIPT_DIRECTORY/lib/telegram.sh"
# shellcheck source=scripts/lib/diagnostics.sh
source "$SCRIPT_DIRECTORY/lib/diagnostics.sh"
# shellcheck source=scripts/lib/config.sh
source "$SCRIPT_DIRECTORY/lib/config.sh"

# --------------------------------------------------------------------------- #
# Isolated update runner
# --------------------------------------------------------------------------- #
# `tmb update` executes from an isolated copy (tmb.sh + lib/) so the updater
# can replace the installed files without truncating its own executing inode.
if [[ "${1:-}" == "update" && "${TMB_UPDATE_RUNNER:-0}" != "1" ]]; then
  UPDATE_RUNNER_DIRECTORY="$(mktemp -d)" || exit 1
  cp "$SCRIPT_PATH" "$UPDATE_RUNNER_DIRECTORY/tmb-runner.sh"
  chmod 700 "$UPDATE_RUNNER_DIRECTORY/tmb-runner.sh"
  cp -r "$SCRIPT_DIRECTORY/lib" "$UPDATE_RUNNER_DIRECTORY/lib"
  if TMB_ROOT_DIR="$ROOT_DIR" \
    TMB_UPDATE_RUNNER=1 \
    TMB_UPDATE_RUNNER_PATH="$UPDATE_RUNNER_DIRECTORY/tmb-runner.sh" \
    bash "$UPDATE_RUNNER_DIRECTORY/tmb-runner.sh" update; then
    rm -rf -- "$UPDATE_RUNNER_DIRECTORY"
  else
    result=$?
    rm -rf -- "$UPDATE_RUNNER_DIRECTORY"
    exit "$result"
  fi
  exit 0
fi

# --------------------------------------------------------------------------- #
# Version / help
# --------------------------------------------------------------------------- #
run_version() {
  local version image digest
  version="$(installed_version)"
  image="$(configured_image)"
  digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}none{{end}}' "$image" 2>/dev/null || true)"
  echo "tmb - Telegram Media Downloader Bot manager"
  echo "Application version: ${version:-unknown}"
  echo "Configured image: $image"
  echo "Image digest: ${digest:-unknown}"
}

command_usage() {
  local command="${1:-}"
  case "$command" in
    start|stop|restart) echo "Usage: tmb $command" ;;
    status) echo "Usage: tmb status" ;;
    services) echo "Usage: tmb services start|stop|restart|recreate|ps|health|start-one SERVICE|stop-one SERVICE|restart-one SERVICE" ;;
    logs) echo "Usage: tmb logs [bot|worker|local-api|redis] [--tail N] [--since 2h] [-f|--follow] [errors]" ;;
    doctor) echo "Usage: tmb doctor [--offline] [--online-service bot|local-api]" ;;
    config) echo "Usage: tmb config check|show|wizard|set KEY VALUE|get KEY|list-add KEY VALUE|list-remove KEY VALUE|set-secret KEY" ;;
    update) echo "Usage: tmb update   (set TMB_RELEASE_TAG=vX.Y.Z for a pinned version)" ;;
    backup) echo "Usage: tmb backup [create|list|inspect FILE|verify FILE|secure FILE|delete FILE [--yes]]" ;;
    restore) echo "Usage: tmb restore [--dry-run] FILE" ;;
    migration) echo "Usage: tmb migration export [--include-downloads] | import FILE" ;;
    cleanup) echo "Usage: tmb cleanup [--dry-run]" ;;
    storage|disk) echo "Usage: tmb storage [overview|cleanup-downloads [--yes]|cleanup-temp [--yes]|orphan-workspaces [--yes]|old-backups [KEEP] [--yes]]" ;;
    docker) echo "Usage: tmb docker status|version|compose-version|containers|images|current-image|digest|pull|pull-latest|recreate|volumes|cleanup-preview|cleanup-old-images [--yes]|compose-config|build" ;;
    telegram) echo "Usage: tmb telegram status|setup|token|test|admin-list|admin-add [ID]|admin-remove [ID]|support|polling" ;;
    channels) echo "Usage: tmb channels status|enable|disable|list|add|remove [ID]|test|update" ;;
    logger) echo "Usage: tmb logger status|enable|disable|list|add [ID]|remove [ID]|alerts [true|false]|mirror [true|false]|payment-events [true|false]|attestation [true|false]|health" ;;
    local-api) echo "Usage: tmb local-api status|configure|start|stop|restart|test|migrate-to-local|migrate-to-cloud" ;;
    cookies) echo "Usage: tmb cookies status|replace [FILE]" ;;
    sessions) echo "Usage: tmb sessions" ;;
    bundle|diagnostics) echo "Usage: tmb bundle" ;;
    uninstall) echo "Usage: tmb uninstall [--yes|full]" ;;
    version) echo "Usage: tmb version" ;;
    help) echo "Usage: tmb help [COMMAND]" ;;
    *)
      cat <<'EOF'
tmb - Telegram Media Downloader Bot manager

Usage: tmb COMMAND [ARGS]

Lifecycle:
  start | stop | restart | status
  services ACTION            service menu actions (start/stop/restart/recreate/ps/health/start-one/stop-one/restart-one)
  health                     per-service health, restart count, uptime

Telegram:
  telegram status|setup|token|test|admin-*|support|polling
  channels status|enable|disable|list|add|remove|test|update
  logger status|enable|disable|list|add|remove|alerts|mirror|payment-events|attestation|health
  local-api status|configure|start|stop|restart|test|migrate-to-local|migrate-to-cloud
  cookies status|replace [FILE]
  sessions                   authentication surface status

Logs & diagnostics:
  logs [service] [--tail N] [--since 2h] [-f] [errors]
  doctor [--offline] [--online-service bot|local-api]
  bundle                     create a sanitized diagnostic support bundle

Storage:
  storage [overview|cleanup-downloads|cleanup-temp|orphan-workspaces|old-backups]
  cleanup [--dry-run]

Backup / restore / migration:
  backup [create|list|inspect FILE|verify FILE|secure FILE|delete FILE]
  restore [--dry-run] FILE
  migration export [--include-downloads] | import FILE

Docker & updates:
  docker status|containers|images|current-image|digest|pull|pull-latest|recreate|volumes|cleanup-preview|cleanup-old-images|compose-config|build
  update                     transactional verified updater (TMB_RELEASE_TAG pins a version)
  version

Configuration:
  config check|show|wizard|set KEY VALUE|get KEY|list-add KEY VALUE|list-remove KEY VALUE|set-secret KEY

Other:
  uninstall [--yes|full]     safe staged uninstall
  help [COMMAND]             this help
  tmb                        interactive menu

Run `tmb` without arguments for the interactive menu. Destructive operations
require an exact typed confirmation, or --yes for automation.
EOF
      ;;
  esac
}

run_help() {
  command_usage "${1:-}"
}

# --------------------------------------------------------------------------- #
# Sub-command frontends (thin wrappers over the shared handlers)
# --------------------------------------------------------------------------- #
run_telegram() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status) telegram_show_status ;;
    setup|wizard) menu_telegram_setup ;;
    token) telegram_setup_token ;;
    test) telegram_test_connection ;;
    admin-list) telegram_admin_list ;;
    admin-add) telegram_admin_add "${1:-}" ;;
    admin-remove) telegram_admin_remove "${1:-}" ;;
    support) telegram_set_support_username ;;
    polling) telegram_set_polling_timeout ;;
    *)
      command_usage telegram
      return 2
      ;;
  esac
}

run_channels() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status) channels_status ;;
    enable) channels_enable ;;
    disable) channels_disable ;;
    list) config_edit_run channel-status ;;
    add) channels_add ;;
    remove) channels_remove "${1:-}" ;;
    test) channels_status probe ;;
    update) channels_update ;;
    *)
      command_usage channels
      return 2
      ;;
  esac
}

run_logger() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status|health) logger_status ;;
    enable) logger_enable ;;
    disable) logger_disable ;;
    list) logger_list_destinations ;;
    add) logger_add_destination "${1:-}" ;;
    remove) logger_remove_destination "${1:-}" ;;
    alerts) logger_set_flag alerts_enabled "${1:-}" ;;
    mirror) logger_set_flag submission_mirror_enabled "${1:-}" ;;
    payment-events) logger_set_flag payment_events_enabled "${1:-}" ;;
    attestation) logger_set_flag operator_privacy_attested "${1:-}" ;;
    test)
      echo "Destination probing is performed by the bot admin panel (🧾 کانال‌های لاگر);" >&2
      echo "the manager exposes aggregate outbox health through tmb logger health." >&2
      return 1
      ;;
    *)
      command_usage logger
      return 2
      ;;
  esac
}

run_local_api() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status) local_api_status ;;
    configure|setup) local_api_configure ;;
    start) run_services start-one local-api ;;
    stop) run_services stop-one local-api ;;
    restart) run_services restart-one local-api ;;
    test) local_api_status ;;
    migrate-to-local) local_api_migrate_to_local ;;
    migrate-to-cloud) local_api_migrate_to_cloud ;;
    *)
      command_usage local-api
      return 2
      ;;
  esac
}

run_cookies() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    status) cookies_status ;;
    replace) cookies_replace_file "${1:-}" ;;
    *)
      command_usage cookies
      return 2
      ;;
  esac
}

run_uninstall() {
  local choice
  if is_tty && [[ $# -eq 0 ]]; then
    ui_heading "Uninstall"
    choice="$(menu_select "Uninstall:" \
      "Stop application only" \
      "Stop and remove containers (keep data)" \
      "Remove application but KEEP backup/data" \
      "Full uninstall (removes data and volumes)")" || return 0
    [[ -n "$choice" ]] || return 0
  else
    case "${1:-}" in
      full|--full) choice=4 ;;
      *) choice=2 ;;
    esac
  fi
  case "$choice" in
    1)
      compose --profile local-api stop
      echo "Application stopped. Containers, data, and volumes are preserved."
      ;;
    2)
      compose --profile local-api down --remove-orphans
      echo "Containers removed. Data, volumes, and configuration are preserved."
      ;;
    3)
      acquire_management_lock || return 1
      if [[ "${TMB_ASSUME_YES:-0}" != "1" ]]; then
        require_confirmation "DELETE-APPLICATION" || return 1
      fi
      compose --profile local-api down --remove-orphans
      rm -f -- "$ROOT_DIR/docker-compose.yml" "$ROOT_DIR/docker-compose.override.yml"
      rm -f -- "$ROOT_DIR/install.sh" "$ROOT_DIR/manage.sh"
      rm -rf -- "$ROOT_DIR/scripts" "$ROOT_DIR/src" "$ROOT_DIR/plugins"
      rm -f -- "$ROOT_DIR/pyproject.toml" "$ROOT_DIR/uv.lock" "$ROOT_DIR/Dockerfile"
      echo "Application files removed. data/, backups/, config.yaml, and .env are preserved."
      ;;
    4)
      acquire_management_lock || return 1
      if [[ "${TMB_ASSUME_YES:-0}" != "1" ]]; then
        read -r -p "Create a migration backup before removing durable state? [y/N]: " answer || answer=n
        if [[ "$answer" =~ ^[yY] ]]; then
          run_migration_export || return 1
        fi
      fi
      if [[ "${TMB_ASSUME_YES:-0}" != "1" ]]; then
        require_confirmation "DELETE-FULL-UNINSTALL" || return 1
      fi
      compose --profile local-api down --volumes --remove-orphans
      [[ "$ROOT_DIR" != "/" && -n "$ROOT_DIR" ]] || {
        echo "Refusing to remove the installation root: $ROOT_DIR" >&2
        return 1
      }
      rm -rf -- "$ROOT_DIR"
      if [[ -L "$TMB_BIN_DIR/tmb" ]]; then
        rm -f -- "$TMB_BIN_DIR/tmb" 2>/dev/null || sudo rm -f -- "$TMB_BIN_DIR/tmb" 2>/dev/null || true
      fi
      echo "Full uninstall completed."
      ;;
  esac
}

# --------------------------------------------------------------------------- #
# Interactive menus
# --------------------------------------------------------------------------- #
menu_dashboard() {
  ui_heading "Dashboard & Status"
  run_status
}

menu_services() {
  local selection
  while true; do
    ui_heading "Service Management"
    selection="$(menu_select "Service:" \
      "Start all" "Stop all" "Restart all" \
      "Start bot" "Stop bot" "Restart bot" \
      "Start worker" "Stop worker" "Restart worker" \
      "Start Redis" "Stop Redis" "Restart Redis" \
      "Start Local API" "Stop Local API" "Restart Local API" \
      "Show services" "Show container health")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_start ;;
      2) run_stop ;;
      3) run_restart ;;
      4) run_services start-one bot ;;
      5) run_services stop-one bot ;;
      6) run_services restart-one bot ;;
      7) run_services start-one worker ;;
      8) run_services stop-one worker ;;
      9) run_services restart-one worker ;;
      10) run_services start-one redis ;;
      11) run_services stop-one redis ;;
      12) run_services restart-one redis ;;
      13) run_services start-one local-api ;;
      14) run_services stop-one local-api ;;
      15) run_services restart-one local-api ;;
      16) run_services ps ;;
      17) run_services_health ;;
    esac
  done
}

menu_telegram_setup() {
  local selection
  while true; do
    ui_heading "Telegram Setup"
    selection="$(menu_select "Telegram:" \
      "Status" \
      "Set/change bot token (hidden input)" \
      "Test token (getMe) and show bot username" \
      "List admin IDs" \
      "Add admin ID" \
      "Remove admin ID" \
      "Set support username" \
      "Set polling timeout")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) telegram_show_status ;;
      2) telegram_setup_token ;;
      3) telegram_test_connection ;;
      4) telegram_admin_list ;;
      5) telegram_admin_add ;;
      6) telegram_admin_remove ;;
      7) telegram_set_support_username ;;
      8) telegram_set_polling_timeout ;;
    esac
  done
}

menu_channels() {
  local selection
  while true; do
    ui_heading "Required Channels"
    selection="$(menu_select "Channels:" \
      "Status" "Enable policy" "Disable policy" "List channels" \
      "Add channel" "Remove channel" "Test channel access" \
      "Change title / join URL")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) channels_status ;;
      2) channels_enable ;;
      3) channels_disable ;;
      4) config_edit_run channel-status ;;
      5) channels_add ;;
      6) channels_remove ;;
      7) channels_status probe ;;
      8) channels_update ;;
    esac
  done
}

menu_logger() {
  local selection
  while true; do
    ui_heading "Operator Logger"
    selection="$(menu_select "Logger:" \
      "Status / outbox health" \
      "Enable logger" \
      "Disable logger" \
      "List destinations" \
      "Add destination" \
      "Remove destination" \
      "Alerts on/off" \
      "Submission mirror on/off" \
      "Payment events on/off" \
      "Privacy attestation on/off")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) logger_status ;;
      2) logger_enable ;;
      3) logger_disable ;;
      4) logger_list_destinations ;;
      5) logger_add_destination ;;
      6) logger_remove_destination ;;
      7) logger_set_flag alerts_enabled ;;
      8) logger_set_flag submission_mirror_enabled ;;
      9) logger_set_flag payment_events_enabled ;;
      10) logger_set_flag operator_privacy_attested ;;
    esac
  done
}

menu_sessions() {
  ui_heading "Telegram / Session Management"
  sessions_status
}

menu_logs() {
  local selection
  while true; do
    ui_heading "Logs & Diagnostics"
    selection="$(menu_select "Logs:" \
      "All services (last 100 lines)" \
      "Bot" "Worker" "Redis" "Local Bot API" \
      "Errors only (last 24h)" \
      "Last 500 lines" \
      "Since 1 hour" "Since 24 hours" \
      "Follow live" \
      "Create sanitized diagnostic bundle" \
      "Show log disk usage")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_logs ;;
      2) run_logs bot ;;
      3) run_logs worker ;;
      4) run_logs redis ;;
      5) run_logs local-api ;;
      6) run_logs errors ;;
      7) run_logs --tail 500 ;;
      8) run_logs --since 1h ;;
      9) run_logs --since 24h ;;
      10) run_logs -f ;;
      11) create_support_bundle ;;
      12) run_log_disk_usage ;;
    esac
  done
}

menu_storage() {
  local selection
  while true; do
    ui_heading "Storage & Media"
    selection="$(menu_select "Storage:" \
      "Overview (sizes)" \
      "Cleanup completed media (downloads)" \
      "Cleanup temp files" \
      "Cleanup orphan workspaces (dry-run)" \
      "Cleanup old backups" \
      "Docker project cleanup preview" \
      "Docker project cleanup (old images)")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) storage_overview ;;
      2) cleanup_downloads ;;
      3) cleanup_temp ;;
      4) cleanup_orphan_workspaces ;;
      5) cleanup_old_backups ;;
      6) run_docker_cleanup_preview ;;
      7) run_docker_cleanup_old_images ;;
    esac
  done
}

menu_backup() {
  local selection archive
  while true; do
    ui_heading "Backup / Restore / Migration"
    selection="$(menu_select "Backup:" \
      "Create operational backup (consistent)" \
      "List backups" \
      "Inspect backup" \
      "Verify backup" \
      "Delete backup" \
      "Export migration bundle" \
      "Restore from backup" \
      "Migration import (new server)")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_backup create ;;
      2) backup_list ;;
      3)
        backup_list
        read -r -p "Archive path: " archive || return 0
        [[ -n "$archive" ]] && backup_inspect "$archive"
        ;;
      4)
        backup_list
        read -r -p "Archive path: " archive || return 0
        [[ -n "$archive" ]] && backup_verify "$archive"
        ;;
      5)
        backup_list
        read -r -p "Archive path: " archive || return 0
        [[ -n "$archive" ]] && backup_delete "$archive"
        ;;
      6) run_migration_export ;;
      7) menu_restore_interactive ;;
      8)
        read -r -p "Migration archive path: " archive || return 0
        [[ -n "$archive" ]] && run_migration import "$archive"
        ;;
    esac
  done
}

menu_restore_interactive() {
  local selection archive
  backup_list
  echo
  read -r -p "Archive path (or absolute path on this server): " archive || return 1
  [[ -n "$archive" ]] || return 1
  read -r -p "Dry run first? [y/N]: " selection || selection=n
  if [[ "$selection" =~ ^[yY] ]]; then
    run_restore --dry-run "$archive"
  else
    run_restore "$archive"
  fi
}

menu_docker() {
  local selection
  while true; do
    ui_heading "Docker & Images"
    selection="$(menu_select "Docker:" \
      "Docker status" \
      "Docker version" \
      "Compose version" \
      "Containers" \
      "Images" \
      "Current project image + digest" \
      "Pull current image" \
      "Pull latest release image" \
      "Recreate containers" \
      "Show volumes" \
      "Project cleanup preview" \
      "Remove unused OLD project images" \
      "Compose config validation" \
      "Build local development image")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_docker_status ;;
      2) run_docker_version ;;
      3) docker compose version ;;
      4) run_docker_containers ;;
      5) run_docker_images ;;
      6) run_docker_current_image ;;
      7) run_docker_pull_current ;;
      8) run_docker_pull_latest_release ;;
      9) run_docker_recreate ;;
      10) run_docker_volumes ;;
      11) run_docker_cleanup_preview ;;
      12) run_docker_cleanup_old_images ;;
      13) run_docker_compose_config ;;
      14) run_docker_build_local ;;
    esac
  done
}

menu_update() {
  local selection
  while true; do
    ui_heading "Update / Rollback"
    selection="$(menu_select "Update:" \
      "Current version" \
      "Check latest release" \
      "Update (transactional verified updater)" \
      "Update to a specific version" \
      "Cleanup old project images" \
      "Previous backup / recovery information")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_version ;;
      2)
        echo "Latest release:"
        curl -fsSL "$(release_url "$ARCHIVE_NAME.sha256")" 2>/dev/null |
          awk '{print "  release asset sha256: " $1}' || echo "  (unable to reach the release server)"
        ;;
      3) run_update ;;
      4)
        local tag
        read -r -p "Release tag (e.g. v1.4.0): " tag || return 0
        [[ -n "$tag" ]] || return 0
        TMB_RELEASE_TAG="$tag" run_update
        ;;
      5) run_docker_cleanup_old_images ;;
      6) backup_list ;;
    esac
  done
}

menu_config() {
  local selection key
  while true; do
    ui_heading "Configuration"
    selection="$(menu_select "Configuration:" \
      "Telegram settings" \
      "Required channels" \
      "Operator Logger" \
      "Local Bot API" \
      "Downloads / media limits" \
      "Queue settings" \
      "Validate configuration" \
      "Sanitized configuration summary" \
      "Full interactive wizard")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) menu_telegram_setup ;;
      2) menu_channels ;;
      3) menu_logger ;;
      4) menu_local_api ;;
      5)
        read -r -p "telegram.max_upload_size_mb: " key || return 0
        [[ -n "$key" ]] && config_edit_run set telegram.max_upload_size_mb "$key"
        read -r -p "media.max_file_size_mb: " key || return 0
        [[ -n "$key" ]] && config_edit_run set media.max_file_size_mb "$key"
        ;;
      6)
        read -r -p "queue.max_jobs: " key || return 0
        [[ -n "$key" ]] && config_edit_run set queue.max_jobs "$key"
        ;;
      7) run_config_check ;;
      8) config_summary ;;
      9) config_wizard ;;
    esac
  done
}

menu_local_api() {
  local selection
  while true; do
    ui_heading "Local Bot API"
    selection="$(menu_select "Local API:" \
      "Status" \
      "Configure (API ID / hash / mode / port)" \
      "Start" "Stop" "Restart" \
      "Test endpoint" \
      "Migrate Cloud -> Local" \
      "Migrate Local -> Cloud")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) local_api_status ;;
      2) local_api_configure ;;
      3) run_services start-one local-api ;;
      4) run_services stop-one local-api ;;
      5) run_services restart-one local-api ;;
      6) local_api_status ;;
      7) local_api_migrate_to_local ;;
      8) local_api_migrate_to_cloud ;;
    esac
  done
}

menu_doctor() {
  local selection
  while true; do
    ui_heading "Security / Doctor"
    selection="$(menu_select "Doctor:" \
      "Full doctor" \
      "Offline doctor" \
      "Config validation (config-check)" \
      "Database / queue / Redis health" \
      "Cookie file status" \
      "Sanitized diagnostic bundle")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1) run_doctor ;;
      2) run_doctor --offline ;;
      3) run_config_check ;;
      4) run_db_health ;;
      5) cookies_status ;;
      6) create_support_bundle ;;
    esac
  done
}

menu_advanced() {
  local selection
  while true; do
    ui_heading "Advanced Tools"
    selection="$(menu_select "Advanced:" \
      "Run a raw compose command" \
      "Run a raw docker command" \
      "Uninstall")" || return 0
    [[ -n "$selection" ]] || return 0
    case "$selection" in
      1)
        local command
        read -r -p "compose args (e.g. config): " command || return 0
        [[ -n "$command" ]] || return 0
        # shellcheck disable=SC2086
        compose $command
        ;;
      2)
        local command
        read -r -p "docker args (use with care): " command || return 0
        [[ -n "$command" ]] || return 0
        # shellcheck disable=SC2086
        docker $command
        ;;
      3) run_uninstall ;;
    esac
  done
}

menu_about() {
  ui_heading "About"
  run_version
  echo
  echo "Repository: https://github.com/HamedSanaei/telegram-media-downloader-bot"
  echo "Installation: docs/INSTALLATION.md | Operations: docs/OPERATIONS.md"
  echo "Management reference: docs/MANAGEMENT.md"
}

menu_main() {
  local selection
  while true; do
    printf '\n%s=================================================%s\n' "$C_BOLD" "$C_RESET"
    printf '%s Telegram Media Downloader Bot Manager %s\n' "$C_BOLD" "$C_RESET"
    printf '%s=================================================%s\n' "$C_BOLD" "$C_RESET"
    printf ' Version: %s   Image: %s\n' "$(installed_version)" "$(configured_image)"
    local service
    for service in bot worker redis local-api; do
      local container state
      container="$(compose --profile local-api ps -q "$service" 2>/dev/null || true)"
      if [[ -z "$container" ]]; then
        state="Disabled"
      elif container_is_running "$container"; then
        state="Running"
      else
        state="Stopped"
      fi
      printf ' %-12s %s\n' "$service:" "$state"
    done
    if config_bot_token_configured; then
      printf ' Telegram: %s\n' "Configured"
    else
      printf ' Telegram: %s\n' "Not configured"
    fi
    selection="$(menu_select "Select:" \
      "Dashboard & Status" \
      "Service Management" \
      "Telegram Setup" \
      "Required Channels" \
      "Operator Logger" \
      "Telegram / Session Management" \
      "Logs & Diagnostics" \
      "Storage & Media" \
      "Backup / Restore / Migration" \
      "Docker & Images" \
      "Update / Rollback" \
      "Configuration" \
      "Security / Doctor" \
      "Advanced Tools" \
      "About / Version")" || exit 0
    [[ -n "$selection" ]] || exit 0
    case "$selection" in
      1) menu_dashboard ;;
      2) menu_services ;;
      3) menu_telegram_setup ;;
      4) menu_channels ;;
      5) menu_logger ;;
      6) menu_sessions ;;
      7) menu_logs ;;
      8) menu_storage ;;
      9) menu_backup ;;
      10) menu_docker ;;
      11) menu_update ;;
      12) menu_config ;;
      13) menu_doctor ;;
      14) menu_advanced ;;
      15) menu_about ;;
    esac
  done
}

# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
run() {
  local command="${1:-}"
  shift || true
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    command_usage "$command"
    return 0
  fi
  case "$command" in
    ""|menu) menu_main ;;
    start) run_start ;;
    stop) run_stop ;;
    restart|recreate) run_restart ;;
    down) run_stop ;;
    status) run_status "$@" ;;
    services) run_services "$@" ;;
    health) run_services_health ;;
    logs) run_logs "$@" ;;
    doctor) run_doctor "$@" ;;
    config) run_config "$@" ;;
    update) run_update ;;
    backup) run_backup "$@" ;;
    restore) run_restore "$@" ;;
    migration) run_migration "$@" ;;
    cleanup) run_cleanup "$@" ;;
    storage|disk) run_storage "$@" ;;
    docker) run_docker "$@" ;;
    telegram) run_telegram "$@" ;;
    channels) run_channels "$@" ;;
    logger) run_logger "$@" ;;
    local-api) run_local_api "$@" ;;
    cookies) run_cookies "$@" ;;
    sessions) sessions_status ;;
    bundle|diagnostics) create_support_bundle ;;
    uninstall) run_uninstall "$@" ;;
    version|--version|-V) run_version ;;
    help|--help|-h) run_help "$@" ;;
    *)
      echo "Unknown command: $command" >&2
      command_usage ""
      return 2
      ;;
  esac
}

if [[ $# -eq 0 ]]; then
  menu_main
else
  run "$@"
fi