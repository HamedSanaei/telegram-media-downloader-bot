# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Storage overview and project-scoped cleanup. Every destructive cleanup is
# bounded to a known root and requires a strong typed confirmation (or --yes).
# No server-wide prune is ever used for normal project cleanup.
# --------------------------------------------------------------------------- #

storage_overview() {
  local label path
  printf '%-28s %-10s %s\n' "PATH" "SIZE" "LOCATION"
  while IFS='|' read -r label path; do
    printf '%-28s %-10s %s\n' "$label" "$(human_size "$(dir_size_bytes "$path")")" "$path"
  done <<EOF
project|$ROOT_DIR
data|$ROOT_DIR/data
downloads|$ROOT_DIR/data/downloads
temp|$ROOT_DIR/data/temp
state|$ROOT_DIR/data/state
cookies|$ROOT_DIR/data/cookies
telegram-bot-api|$ROOT_DIR/data/telegram-bot-api
backups|$ROOT_DIR/backups
EOF
  printf '%-28s %-10s %s\n' "sqlite" "$(human_size "$(file_size_bytes "$ROOT_DIR/data/state/jobs.sqlite3")")" "$ROOT_DIR/data/state/jobs.sqlite3"
  printf 'Root filesystem: %s\n' "$(df -hP / 2>/dev/null | tail -n 1 | awk '{print $3 " used / " $2 " (" $5 ")"}')"
}

# Bounded deletion helper: $1 = root that must contain the target.
remove_under_root() {
  local root="$1" target="$2"
  case "$target" in
    "$root"/*) ;;
    *)
      echo "Refusing: cleanup target escapes its safe root: $target" >&2
      return 1
      ;;
  esac
  rm -rf -- "$target"
}

cleanup_downloads() {
  local assume_yes=0
  [[ "${1:-}" == "--yes" ]] && assume_yes=1
  if [[ "$assume_yes" != "1" ]]; then
    require_confirmation "DELETE-DOWNLOADS" || return 1
  fi
  mkdir -p data/downloads
  local count
  count="$(find data/downloads -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)"
  remove_under_root "$ROOT_DIR/data" "$ROOT_DIR/data/downloads"
  mkdir -p data/downloads
  echo "Downloads cleanup completed ($count top-level items removed)."
}

cleanup_temp() {
  local assume_yes=0
  [[ "${1:-}" == "--yes" ]] && assume_yes=1
  if [[ "$assume_yes" != "1" ]]; then
    require_confirmation "DELETE-TEMP" || return 1
  fi
  mkdir -p data/temp
  local count
  count="$(find data/temp -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)"
  remove_under_root "$ROOT_DIR/data" "$ROOT_DIR/data/temp"
  mkdir -p data/temp
  echo "Temporary cleanup completed ($count top-level items removed)."
}

cleanup_orphan_workspaces() {
  # Project-owned workspace sweeper (dry-run by default; --yes to reclaim).
  local dry_run=1
  if [[ "${1:-}" == "--yes" ]]; then
    dry_run=0
  fi
  local -a arguments=(telegram-media-bot cleanup-workspaces --config /app/config.yaml)
  if [[ "$dry_run" == "1" ]]; then
    arguments+=(--dry-run)
  fi
  compose --profile local-api run --rm --no-deps worker "${arguments[@]}"
}

cleanup_old_backups() {
  local keep="${1:-10}" assume_yes=0
  [[ "${2:-}" == "--yes" ]] && assume_yes=1
  mkdir -p backups
  local -a archives=()
  local archive
  while IFS= read -r archive; do
    archives+=("$archive")
  done < <(find backups -maxdepth 1 -type f -name 'tmb-*.tar.gz' | sort)
  if [[ ${#archives[@]} -le "$keep" ]]; then
    echo "No old backups to remove (${#archives[@]} archives; keeping $keep)."
    return 0
  fi
  local -a remove=("${archives[@]:0:$((${#archives[@]} - keep))}")
  if [[ "$assume_yes" != "1" ]]; then
    echo "Removing ${#remove[@]} old backup archive(s); newest $keep are kept."
    require_confirmation "DELETE-OLD-BACKUPS" || return 1
  fi
  local item
  for item in "${remove[@]}"; do
    case "$item" in
      "$ROOT_DIR"/backups/*) rm -f -- "$item" "$item.sha256" ;;
    esac
  done
  echo "Removed ${#remove[@]} old backup(s)."
}

# tmb storage / tmb disk ------------------------------------------------
run_storage() {
  local action="${1:-overview}"
  case "$action" in
    overview|"")
      storage_overview
      ;;
    cleanup-downloads)
      acquire_management_lock || return 1
      cleanup_downloads "${2:-}"
      ;;
    cleanup-temp)
      acquire_management_lock || return 1
      cleanup_temp "${2:-}"
      ;;
    cleanup-workspaces|orphan-workspaces)
      acquire_management_lock || return 1
      cleanup_orphan_workspaces "${2:-}"
      ;;
    cleanup-backups|old-backups)
      acquire_management_lock || return 1
      cleanup_old_backups "${2:-10}" "${3:-}"
      ;;
    *)
      echo "Usage: tmb storage [overview|cleanup-downloads [--yes]|cleanup-temp [--yes]|orphan-workspaces [--yes]|old-backups [KEEP] [--yes]]" >&2
      return 2
      ;;
  esac
}

# tmb cleanup (backward compatible: workspaces + project image cleanup) ------
run_cleanup() {
  acquire_management_lock || return 1
  local dry_run=false
  local -a workspace_cleanup_arguments=(
    telegram-media-bot cleanup-workspaces --config /app/config.yaml
  )
  if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=true
    workspace_cleanup_arguments+=(--dry-run)
  elif [[ -n "${1:-}" ]]; then
    echo "Usage: tmb cleanup [--dry-run]" >&2
    return 2
  fi
  compose --profile local-api run --rm --no-deps worker \
    "${workspace_cleanup_arguments[@]}"
  cleanup_project_resources "$dry_run"
}