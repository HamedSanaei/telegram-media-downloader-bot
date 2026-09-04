# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Transactional restore. The flow is:
#   validate archive -> record service state -> stop writers ->
#   pre-restore backup -> extract to staging -> stage secret ownership ->
#   validate staged state -> swap persistent entries (rollback snapshot) ->
#   repair permissions -> config-check + offline doctor ->
#   restore exact service state -> online verification. Any failure rolls
#   back automatically. The mutating transaction runs in a subshell so its
#   SIGINT trap can never leak.
# --------------------------------------------------------------------------- #

# Restore validation that is safe to run without touching the installation
# (used by --dry-run and by the full flow before any mutation).
restore_dry_validation() {
  local archive="$1" kind_requirement="${2:-}"
  validate_backup_archive "$archive" || return 1
  local kind app_version image
  kind="$(backup_manifest_field "$archive" kind)"
  app_version="$(backup_manifest_field "$archive" app_version)"
  image="$(backup_manifest_field "$archive" image)"
  if [[ "$kind_requirement" == "migration" && "$kind" != "migration" ]]; then
    echo "This archive is a $kind backup; migration import requires a migration export." >&2
    return 1
  fi
  local current_version current_image
  current_version="$(installed_version)"
  current_image="$(configured_image)"
  echo "Backup summary:"
  echo "  kind: $kind"
  echo "  backup app_version: ${app_version:-unknown}"
  echo "  backup image: ${image:-unknown}"
  echo "  backup created_at: $(backup_manifest_field "$archive" created_at)"
  echo "Current installation:"
  echo "  app_version: ${current_version:-unknown}"
  echo "  image: $current_image"
  if [[ -n "$app_version" && "$app_version" != "$current_version" ]]; then
    echo "WARNING: the backup was created by application version $app_version; the current version is $current_version." >&2
    echo "Persistent state is restored as-is; update or pin the matching image after restore if required." >&2
  fi
  if [[ -n "$image" && "$image" != "$current_image" ]]; then
    echo "WARNING: the backup image reference differs from the configured image." >&2
    echo "The installed application/image is NOT replaced by restore; only persistent state is." >&2
  fi
}

# Validate the extracted staging tree with the configured runtime image.
validate_staged_state() {
  local staging="$1" uid gid
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  [[ -f "$staging/config.yaml" ]] || {
    echo "Restore validation failed: staged config.yaml is missing." >&2
    return 1
  }
  local unexpected name
  unexpected="$(find "$staging" -mindepth 1 -maxdepth 1 -print0 | while IFS= read -r -d '' path; do
    name="${path##*/}"
    case "$name" in
      config.yaml|.env|data|manifest.json|.gitkeep) ;;
      *) printf '%s\n' "$name" ;;
    esac
  done | head -n 1)"
  if [[ -n "$unexpected" ]]; then
    echo "Restore validation failed: unexpected top-level entry in archive: $unexpected" >&2
    return 1
  fi
  if [[ -f "$staging/data/state/jobs.sqlite3" ]]; then
    local integrity
    integrity="$(
      docker run --rm --user "$uid:$gid" \
        -v "$staging/data:/data:ro" \
        "$(configured_image)" \
        python -c '
import sqlite3
connection = sqlite3.connect("file:/data/state/jobs.sqlite3?immutable=1", uri=True)
try:
    print(connection.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    connection.close()
' 2>/dev/null
    )" || integrity=""
    if [[ "$(printf '%s' "$integrity" | tail -n 1)" != "ok" ]]; then
      echo "Restore validation failed: staged SQLite database failed integrity_check." >&2
      return 1
    fi
    echo "SQLite integrity: OK"
  else
    echo "SQLite integrity: (no jobs.sqlite3 in this backup)"
  fi
  if ! docker run --rm --read-only --user "$uid:$gid" \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777 \
    -v "$staging/config.yaml:/app/config.yaml:ro" \
    -v "$staging/data:/data:ro" \
    "$(configured_image)" \
    telegram-media-bot config-check --config /app/config.yaml \
      --read-only-runtime >/dev/null 2>&1; then
    echo "Restore validation failed: staged configuration is invalid." >&2
    return 1
  fi
  echo "Configuration: OK"
  return 0
}

# After the state swap the restored config.yaml may be owned by the source
# server's operator while the restored .env carries the SOURCE runtime
# APP_UID/APP_GID. Containers run as that restored identity, so the private
# 0600 config must be chowned to it before any offline doctor or service
# start. Runs through the image as root (same pattern as
# normalize_runtime_permissions) so no host sudo is required. Rollback
# restores the pre-restore file - owner, mode, and contents - from the
# rollback snapshot via plain mv.
# Staged validation (SQLite integrity + config-check) runs as the CURRENT
# runtime identity from .env, but the operator may run the import as root, so
# the freshly extracted tree (config.yaml 0600, data directories 0700 from
# the source archive) would be root-only and unreadable by an unprivileged
# destination APP_UID. Re-own only the STAGED copy to the current runtime
# identity before validation (config.yaml stays 0600, data stays 0700/0600);
# the live installation is untouched until the swap, and rollback semantics
# are unchanged. After the swap the live file is re-owned again to the
# RESTORED identity by repair_restored_secret_permissions.
stage_secret_permissions() {
  local staging="$1" uid gid image
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  image="$(configured_image)"
  docker run --rm --user 0 --entrypoint sh \
    -e "APP_UID=$uid" -e "APP_GID=$gid" \
    -v "$staging:/staging:rw" "$image" -c '
      set -eu
      test -f /staging/config.yaml
      if [ -e /staging/data ]; then
        chown -R "$APP_UID:$APP_GID" /staging/data
      fi
      chown "$APP_UID:$APP_GID" /staging/config.yaml
      chmod 600 /staging/config.yaml
    '
}

repair_restored_secret_permissions() {
  local image="$1" uid gid
  uid="$(runtime_identity APP_UID 10001)"
  gid="$(runtime_identity APP_GID 10001)"
  if ! docker run --rm --user 0 --entrypoint sh \
    -e "APP_UID=$uid" -e "APP_GID=$gid" \
    -v "$ROOT_DIR:/workspace" "$image" -c '
      set -eu
      test -f /workspace/config.yaml
      chown "$APP_UID:$APP_GID" /workspace/config.yaml
      chmod 600 /workspace/config.yaml
    '; then
    echo "Restore failed: could not apply the restored runtime ownership to config.yaml; rolling back." >&2
    return 1
  fi
}

# Swap staged persistent entries into place, keeping a rollback snapshot.
# Only entries present in the archive are replaced; current downloads/temp
# are preserved when the archive does not contain them.
swap_staged_entries() {
  local staging="$1" rollback="$2" name
  for name in config.yaml .env; do
    [[ -e "$staging/$name" ]] || continue
    if [[ -e "$ROOT_DIR/$name" || -L "$ROOT_DIR/$name" ]]; then
      mv "$ROOT_DIR/$name" "$rollback/$name" || return 1
    fi
    mv "$staging/$name" "$ROOT_DIR/$name" || return 1
    RESTORE_ENTRIES+=("$name")
  done
  # Restore data subdirectories individually so downloads/temp that are not
  # part of the backup survive the swap.
  if [[ -d "$staging/data" ]]; then
    local sub
    while IFS= read -r sub; do
      name="${sub##*/}"
      case "$name" in
        state|cookies|telegram-bot-api|downloads|temp) ;;
        *) continue ;;
      esac
      [[ -e "$staging/data/$name" ]] || continue
      if [[ -e "$ROOT_DIR/data/$name" || -L "$ROOT_DIR/data/$name" ]]; then
        mv "$ROOT_DIR/data/$name" "$rollback/data-$name" || return 1
      fi
      mv "$staging/data/$name" "$ROOT_DIR/data/$name" || return 1
      RESTORE_DATA_ENTRIES+=("$name")
    done < <(find "$staging/data" -mindepth 1 -maxdepth 1 -print0 | while IFS= read -r -d '' path; do printf '%s\n' "${path##*/}"; done)
  fi
}

rollback_staged_entries() {
  local rollback="$1" name
  for name in "${RESTORE_ENTRIES[@]}"; do
    rm -rf -- "${ROOT_DIR:?}/$name"
    if [[ -e "$rollback/$name" || -L "$rollback/$name" ]]; then
      mv "$rollback/$name" "$ROOT_DIR/$name" || return 1
    fi
  done
  RESTORE_ENTRIES=()
  for name in "${RESTORE_DATA_ENTRIES[@]}"; do
    rm -rf -- "${ROOT_DIR:?}/data/$name"
    if [[ -e "$rollback/data-$name" || -L "$rollback/data-$name" ]]; then
      mv "$rollback/data-$name" "$ROOT_DIR/data/$name" || return 1
    fi
  done
  RESTORE_DATA_ENTRIES=()
}

restore_rollback_and_restore_services() {
  local rollback="$1" parent="$2" pre_restore_backup="$3"
  if ! rollback_staged_entries "$rollback"; then
    echo "ROLLBACK FAILED: the installation is partially restored." >&2
    echo "Recovery material is preserved in $parent; pre-restore backup: ${pre_restore_backup:-backups/}" >&2
    echo "Stop the project writers (tmb stop), then restore the pre-restore backup manually:" >&2
    echo "  tar --force-local -xzf \"$pre_restore_backup\" -C \"$ROOT_DIR\"" >&2
    return 1
  fi
  start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
  verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || true
  verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
  rm -rf -- "$parent" 2>/dev/null || true
  return 0
}

restore_interrupt() {
  trap - INT
  echo "Restore interrupted by operator." >&2
  if [[ "${RESTORE_SWAPPED:-0}" == "1" ]]; then
    compose --profile local-api stop "${FILESYSTEM_WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
    restore_rollback_and_restore_services \
      "$RESTORE_ROLLBACK_DIRECTORY" "$RESTORE_PARENT_DIRECTORY" "$RESTORE_PRE_BACKUP" || true
  else
    start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    rm -rf -- "$RESTORE_PARENT_DIRECTORY" 2>/dev/null || true
  fi
  exit 130
}

# The mutating transaction. Runs in a subshell so the SIGINT trap is scoped.
restore_transaction() {
  local archive="$1" parent="$2"
  local staging="$parent/extracted" rollback="$parent/rollback"
  mkdir -p "$staging" "$rollback"
  RESTORE_ENTRIES=()
  RESTORE_DATA_ENTRIES=()
  RESTORE_PARENT_DIRECTORY="$parent"
  RESTORE_ROLLBACK_DIRECTORY="$rollback"
  RESTORE_SWAPPED=0
  trap restore_interrupt INT

  PREVIOUS_PROJECT_SERVICES=()
  PREVIOUS_WRITER_SERVICES=()
  load_running_project_services || return 1
  if [[ ${#PREVIOUS_WRITER_SERVICES[@]} -gt 0 ]]; then
    if ! compose --profile local-api stop -t 45 "${PREVIOUS_WRITER_SERVICES[@]}"; then
      echo "Restore failed: could not stop filesystem writers; nothing was changed." >&2
      rm -rf -- "$parent"
      return 1
    fi
  fi

  local pre_restore_backup
  pre_restore_backup="$(backup 2>/dev/null | sed -n 's/^Backup created: //p' || true)"
  RESTORE_PRE_BACKUP="$pre_restore_backup"
  echo "Pre-restore safety backup: ${pre_restore_backup:-(failed; continuing with writers stopped)}"

  if ! tar --force-local -xzf "$archive" -C "$staging" --no-same-owner 2>/dev/null; then
    echo "Restore failed: archive extraction failed; nothing was changed." >&2
    start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    rm -rf -- "$parent"
    return 1
  fi

  if ! stage_secret_permissions "$staging"; then
    echo "Restore failed: could not stage the private configuration for validation; nothing was changed." >&2
    start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    rm -rf -- "$parent"
    return 1
  fi

  if ! validate_staged_state "$staging"; then
    echo "Restore failed validation; the installation was not modified." >&2
    start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    rm -rf -- "$parent"
    return 1
  fi

  local configured
  configured="$(configured_image)"
  RESTORE_SWAPPED=1
  if ! swap_staged_entries "$staging" "$rollback"; then
    echo "Restore failed during the state swap; rolling back." >&2
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi

  if ! repair_restored_secret_permissions "$configured"; then
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi

  if ! normalize_runtime_permissions "$configured"; then
    echo "Restore failed: runtime permission repair failed; rolling back." >&2
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi
  if ! probe_runtime_writes "$configured"; then
    echo "Restore failed: runtime write probe failed; rolling back." >&2
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi

  if ! compose --profile local-api run --rm --no-deps worker \
    telegram-media-bot doctor --config /app/config.yaml --offline >/dev/null 2>&1; then
    echo "Restore failed the offline doctor check; rolling back." >&2
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi

  if ! start_services false "${PREVIOUS_PROJECT_SERVICES[@]}"; then
    echo "Restore failed to restart services; rolling back." >&2
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi
  if ! verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}"; then
    echo "Restore failed service health verification; rolling back." >&2
    compose --profile local-api stop "${FILESYSTEM_WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi
  if ! verify_restored_services_online; then
    echo "Restore failed the online verification; rolling back." >&2
    compose --profile local-api stop "${FILESYSTEM_WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi
  if ! verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}"; then
    echo "Restore failed the exact service-state verification; rolling back." >&2
    compose --profile local-api stop "${FILESYSTEM_WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
    restore_rollback_and_restore_services "$rollback" "$parent" "$pre_restore_backup" || return 1
    return 1
  fi

  rm -rf -- "$parent"
  echo "Restore completed successfully."
  echo "The pre-restore safety backup is retained: ${pre_restore_backup:-backups/}"
  return 0
}

restore_archive() {
  local archive="$1" dry_run="${2:-0}" kind_requirement="${3:-}" parent status
  archive="$(resolve_backup_path "$archive")"
  restore_dry_validation "$archive" "$kind_requirement" || return 1
  if [[ "$dry_run" == "1" ]]; then
    echo "Dry run: archive validation passed; nothing was changed."
    return 0
  fi
  parent="$(mktemp -d "$ROOT_DIR/.tmb-restore.XXXXXX")" || return 1
  status=0
  (restore_transaction "$archive" "$parent") || status=$?
  if [[ "$status" -ne 0 && -d "$parent" ]]; then
    # The transaction failed before its own cleanup; preserve recovery material
    # and tell the operator exactly what to do.
    echo "Restore failed; recovery material is preserved in $parent." >&2
    echo "Review it, then remove it with: rm -rf -- \"$parent\"" >&2
  fi
  return "$status"
}

run_restore() {
  local dry_run=0 file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1 ;;
      --yes)
        # Read by require_confirmation (common.sh) through the flow below.
        # shellcheck disable=SC2034
        TMB_ASSUME_YES=1
        ;;
      -*) echo "Unknown option: $1" >&2; return 2 ;;
      *) file="$1" ;;
    esac
    shift
  done
  [[ -n "$file" ]] || {
    echo "Usage: tmb restore [--dry-run] FILE" >&2
    return 2
  }
  acquire_management_lock || return 1
  restore_archive "$file" "$dry_run"
}

run_migration() {
  local action="${1:-}"
  case "$action" in
    export)
      shift
      run_migration_export "$@"
      ;;
    import)
      local file="${2:-}"
      [[ -n "$file" ]] || {
        echo "Usage: tmb migration import FILE" >&2
        return 2
      }
      acquire_management_lock || return 1
      restore_archive "$file" 0 migration
      ;;
    *)
      echo "Usage: tmb migration export [--include-downloads] | import FILE" >&2
      return 2
      ;;
  esac
}