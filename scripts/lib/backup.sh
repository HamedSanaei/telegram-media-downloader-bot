# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Backup subsystem. Archives are private (umask 077, mode 0600), atomically
# published, contain a manifest plus SHA-256 checksum, and never include the
# volatile Local Bot API log, downloads, or temp work (unless an explicit
# migration export with --include-downloads is requested).
# --------------------------------------------------------------------------- #

BACKUP_SCHEMA_VERSION=1

# Core archive creation. `backup` is invoked by the updater after writers are
# stopped; `perform_consistent_manual_backup` wraps it with writer stop/start.
backup() {
  backup_archive operational 0
}

backup_archive() {
  local kind="${1:-operational}" include_downloads="${2:-0}"
  mkdir -p backups
  local stamp archive temporary_archive suffix=0 manifest_dir manifest
  local -a backup_items=(config.yaml .env)
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="backups/tmb-${stamp}.tar.gz"
  while [[ -e "$archive" ]]; do
    suffix=$((suffix + 1))
    archive="backups/tmb-${stamp}-${suffix}.tar.gz"
  done
  temporary_archive="$(mktemp "backups/.tmb-${stamp}.XXXXXX.tar.gz")" || return 1
  for item in data/state data/cookies data/telegram-bot-api; do
    if [[ -e "$item" ]]; then
      backup_items+=("$item")
    fi
  done
  if [[ "$include_downloads" == "1" && -d data/downloads ]]; then
    backup_items+=("data/downloads")
  fi
  manifest="$(generate_backup_manifest "$kind" "${backup_items[@]}")"
  manifest_dir="$(mktemp -d)" || {
    rm -f -- "$temporary_archive"
    return 1
  }
  printf '%s\n' "$manifest" >"$manifest_dir/manifest.json"
  if ! (
    umask 077
    # Relative path only (backups/.tmb-*); no --force-local so the invocation keeps the
    # canonical `tar -czf <target>` shape that failure-injection fixtures rely on.
    tar -czf "$temporary_archive" \
      --exclude='data/telegram-bot-api/telegram-bot-api.log' \
      -C "$manifest_dir" manifest.json \
      -C "$ROOT_DIR" "${backup_items[@]}"
  ); then
    rm -f -- "$temporary_archive"
    rm -rf -- "$manifest_dir"
    return 1
  fi
  rm -rf -- "$manifest_dir"
  chmod 600 "$temporary_archive" || {
    rm -f -- "$temporary_archive"
    return 1
  }
  mv -f -- "$temporary_archive" "$archive" || {
    rm -f -- "$temporary_archive"
    return 1
  }
  # The checksum file must reference the archive by basename: validation runs
  # `sha256sum --check` from inside the backups directory.
  if (
    umask 077
    cd backups
    sha256sum "$(basename "$archive")" >"$(basename "$archive").sha256"
  ); then
    chmod 600 "$archive.sha256" 2>/dev/null || true
  fi
  echo "Backup created: $archive"
}

generate_backup_manifest() {
  local kind="$1"
  shift
  local app_version image contents=""
  app_version="$(installed_version)"
  image="$(configured_image)"
  local item
  for item in "$@"; do
    if [[ -n "$contents" ]]; then
      contents="$contents, "
    fi
    contents="$contents\"$item\""
  done
  cat <<EOF
{
  "schema_version": $BACKUP_SCHEMA_VERSION,
  "kind": "$kind",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "$app_version",
  "image": "$image",
  "contents": [$contents]
}
EOF
}

backup_manifest_field() {
  local archive="$1" field="$2" temporary status
  # The manifest is JSON; parsing it with grep/sed corrupts values containing
  # ':' (image refs), '@sha256:', hyphens, or full ISO timestamps. Extract it
  # and parse with the Python stdlib json module.
  temporary="$(mktemp -d)" || return 1
  if ! tar --force-local -xzOf "$archive" manifest.json \
    >"$temporary/manifest.json" 2>/dev/null; then
    rm -rf -- "$temporary"
    return 1
  fi
  python3 - "$temporary/manifest.json" "$field" <<'PY' || status=$?
import json
import sys

path, field = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
except Exception:
    sys.exit(1)
value = manifest.get(field)
if value is None:
    sys.exit(1)
print(value)
PY
  status=${status:-0}
  rm -rf -- "$temporary"
  return "$status"
}

# The manifest `contents` array as a display string (JSON list joined with ", ").
backup_manifest_contents() {
  local archive="$1" temporary status
  temporary="$(mktemp -d)" || return 1
  if ! tar --force-local -xzOf "$archive" manifest.json \
    >"$temporary/manifest.json" 2>/dev/null; then
    rm -rf -- "$temporary"
    return 1
  fi
  python3 - "$temporary/manifest.json" <<'PY' || status=$?
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        manifest = json.load(handle)
except Exception:
    sys.exit(1)
contents = manifest.get("contents")
if not isinstance(contents, list):
    sys.exit(1)
print(", ".join(str(item) for item in contents))
PY
  status=${status:-0}
  rm -rf -- "$temporary"
  return "$status"
}

backup_list() {
  local archive size mtime kind app
  mkdir -p backups
  if ! compgen -G "backups/tmb-*.tar.gz" >/dev/null 2>&1; then
    echo "No backups found under $ROOT_DIR/backups."
    return 0
  fi
  printf '%-36s %-9s %-19s %-12s %s\n' "ARCHIVE" "SIZE" "CREATED" "KIND" "APP"
  for archive in backups/tmb-*.tar.gz; do
    [[ -f "$archive" ]] || continue
    size="$(human_size "$(file_size_bytes "$archive")")"
    mtime="$(stat -c '%y' "$archive" 2>/dev/null | cut -d. -f1)"
    kind="$(backup_manifest_field "$archive" kind 2>/dev/null || true)"
    kind="${kind:-unknown}"
    app="$(backup_manifest_field "$archive" app_version 2>/dev/null || true)"
    app="${app:-unknown}"
    printf '%-36s %-9s %-19s %-12s %s\n' "$(basename "$archive")" "$size" "$mtime" "$kind" "$app"
  done
}

resolve_backup_path() {
  local candidate="$1"
  if [[ "$candidate" != /* ]]; then
    candidate="$ROOT_DIR/$candidate"
  fi
  readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate"
}

# Warn (never fail) when an archive or its checksum is group/world readable,
# e.g. after scp/rsync copied it with default umask. Migration import accepts
# such files (the incoming archive is not required to be runtime-owned), but
# operators should re-secure them with `tmb backup secure FILE`.
warn_if_backup_world_readable() {
  local archive="$1" mode checksum_mode
  mode="$(stat -c '%a' "$archive" 2>/dev/null || echo unknown)"
  case "$mode" in
    [0-7][0-7][0-7]) ;;
    *) mode="unknown" ;;
  esac
  if [[ "$mode" != "unknown" && "${mode:1:1}" =~ [4-7] || "$mode" != "unknown" && "${mode:2:1}" =~ [4-7] ]]; then
    echo "Warning: the archive is group/world readable (mode $mode); it was copied with a permissive umask." >&2
    echo "Re-secure it with: tmb backup secure $(basename "$archive")" >&2
  fi
  if [[ -f "$archive.sha256" ]]; then
    checksum_mode="$(stat -c '%a' "$archive.sha256" 2>/dev/null || echo unknown)"
    case "$checksum_mode" in
      [0-7][0-7][0-7]) ;;
      *) checksum_mode="unknown" ;;
    esac
    if [[ "$checksum_mode" != "unknown" && "${checksum_mode:1:1}" =~ [4-7] || "$checksum_mode" != "unknown" && "${checksum_mode:2:1}" =~ [4-7] ]]; then
      echo "Warning: the checksum file is group/world readable (mode $checksum_mode)." >&2
      echo "Re-secure it with: tmb backup secure $(basename "$archive")" >&2
    fi
  fi
}

# Structural + security validation of an archive (used by verify, inspect,
# and restore). Prints a safe summary; returns non-zero on any failure.
validate_backup_archive() {
  local archive="$1"
  if [[ ! -f "$archive" ]]; then
    echo "Backup file not found or not a regular file: $(printf '%s' "$archive" | redact_string)" >&2
    return 1
  fi
  if ! gzip -t "$archive" 2>/dev/null; then
    echo "Backup is not a valid gzip archive: $(printf '%s' "$archive" | redact_string)" >&2
    return 1
  fi
  local entry
  while IFS= read -r entry; do
    case "$entry" in
      /*|*../*)
        echo "Backup contains an unsafe path entry: $(printf '%s' "$entry" | redact_string)" >&2
        return 1
        ;;
      ./*|*//*)
        echo "Backup contains an unexpected path entry: $(printf '%s' "$entry" | redact_string)" >&2
        return 1
        ;;
    esac
  done < <(tar --force-local -tzf "$archive" 2>/dev/null)
  local line
  while IFS= read -r line; do
    case "$line" in
      l*|c*|b*|p*)
        echo "Backup contains a symlink or special file; rejecting." >&2
        return 1
        ;;
    esac
  done < <(tar --force-local -tvzf "$archive" 2>/dev/null)
  if [[ -f "$archive.sha256" ]]; then
    if ! (
      cd "$(dirname "$archive")"
      sha256sum --check --status "$(basename "$archive").sha256"
    ); then
      echo "Backup checksum verification failed: $(printf '%s' "$archive" | redact_string)" >&2
      return 1
    fi
    echo "Checksum: OK"
  else
    echo "Checksum: (no sibling .sha256 file present)"
  fi
  warn_if_backup_world_readable "$archive"
  local schema kind
  schema="$(backup_manifest_field "$archive" schema_version 2>/dev/null || true)"
  kind="$(backup_manifest_field "$archive" kind 2>/dev/null || true)"
  if [[ "$schema" != "$BACKUP_SCHEMA_VERSION" ]]; then
    echo "Backup manifest is missing or uses an unsupported schema version: ${schema:-none}" >&2
    return 1
  fi
  if [[ "$kind" != "operational" && "$kind" != "migration" ]]; then
    echo "Backup manifest has an unknown kind: ${kind:-none}" >&2
    return 1
  fi
  # Member existence check: `tar -tzf ARCHIVE MEMBER` exits non-zero when the
  # member is absent without any grep -q/pipefail SIGPIPE race.
  if ! tar --force-local -tzf "$archive" config.yaml >/dev/null 2>&1; then
    echo "Backup is missing the required config.yaml entry." >&2
    return 1
  fi
  return 0
}

backup_verify() {
  local archive="$1"
  [[ -n "$archive" ]] || {
    echo "Usage: tmb backup verify FILE" >&2
    return 2
  }
  archive="$(resolve_backup_path "$archive")"
  validate_backup_archive "$archive" || return 1
  echo "Backup is valid: $(printf '%s' "$archive" | redact_string)"
  echo "  kind: $(backup_manifest_field "$archive" kind)"
  echo "  app_version: $(backup_manifest_field "$archive" app_version)"
  echo "  image: $(backup_manifest_field "$archive" image)"
  echo "  created_at: $(backup_manifest_field "$archive" created_at)"
}

backup_inspect() {
  local archive="$1"
  [[ -n "$archive" ]] || {
    echo "Usage: tmb backup inspect FILE" >&2
    return 2
  }
  archive="$(resolve_backup_path "$archive")"
  validate_backup_archive "$archive" || return 1
  local size mode
  size="$(human_size "$(file_size_bytes "$archive")")"
  mode="$(stat -c '%a' "$archive" 2>/dev/null || echo unknown)"
  echo "Backup: $(printf '%s' "$archive" | redact_string)"
  echo "  size: $size"
  echo "  mode: $mode"
  echo "  kind: $(backup_manifest_field "$archive" kind)"
  echo "  created_at: $(backup_manifest_field "$archive" created_at)"
  echo "  app_version: $(backup_manifest_field "$archive" app_version)"
  echo "  image: $(backup_manifest_field "$archive" image)"
  echo "  contents: $(backup_manifest_contents "$archive" 2>/dev/null || true)"
  echo "  entries:"
  tar --force-local -tzf "$archive" 2>/dev/null | grep -v '^$' | sed 's/^/    /' | redact_string
}

# Re-secure an archive copied in with a permissive umask: archive and checksum
# are both set to 0600. The archive contents are never modified.
backup_secure() {
  local archive="$1"
  [[ -n "$archive" ]] || {
    echo "Usage: tmb backup secure FILE [--yes]" >&2
    return 2
  }
  archive="$(resolve_backup_path "$archive")"
  [[ -f "$archive" ]] || {
    echo "Backup file not found: $(printf '%s' "$archive" | redact_string)" >&2
    return 1
  }
  chmod 600 "$archive" || {
    echo "Unable to secure the archive; is it owned by this user?" >&2
    return 1
  }
  if [[ -f "$archive.sha256" ]]; then
    chmod 600 "$archive.sha256" 2>/dev/null || true
  fi
  echo "Backup secured (mode 0600): $(printf '%s' "$archive" | redact_string)"
}

backup_delete() {
  local archive="$1" assume_yes=0
  [[ -n "$archive" ]] || {
    echo "Usage: tmb backup delete FILE [--yes]" >&2
    return 2
  }
  if [[ "${2:-}" == "--yes" ]]; then
    assume_yes=1
  fi
  archive="$(resolve_backup_path "$archive")"
  case "$archive" in
    "$ROOT_DIR"/backups/*) ;;
    *)
      echo "Refusing: backup deletion is limited to files under $ROOT_DIR/backups." >&2
      return 1
      ;;
  esac
  [[ -f "$archive" ]] || {
    echo "Backup file not found: $archive" >&2
    return 1
  }
  if [[ "$assume_yes" != "1" ]]; then
    require_confirmation "DELETE-BACKUP $(basename "$archive")" || return 1
  fi
  rm -f -- "$archive" "$archive.sha256"
  echo "Deleted backup: $(basename "$archive")"
}

# tmb backup [create|list|inspect FILE|verify FILE|delete FILE] ------------
run_backup() {
  local action="${1:-create}"
  case "$action" in
    create|"")
      shift || true
      acquire_management_lock || return 1
      perform_consistent_manual_backup
      ;;
    list)
      backup_list
      ;;
    inspect)
      backup_inspect "${2:-}"
      ;;
    verify)
      backup_verify "${2:-}"
      ;;
    secure)
      backup_secure "${2:-}"
      ;;
    delete)
      acquire_management_lock || return 1
      backup_delete "${2:-}" "${3:-}"
      ;;
    *)
      echo "Usage: tmb backup [create|list|inspect FILE|verify FILE|secure FILE|delete FILE]" >&2
      return 2
      ;;
  esac
}

# Migration export -----------------------------------------------------
perform_consistent_backup_kind() {
  local kind="$1" include_downloads="$2"
  local backup_status=0 restore_status=0 stop_status=0
  PREVIOUS_PROJECT_SERVICES=()
  PREVIOUS_WRITER_SERVICES=()
  load_running_project_services || return 1
  if [[ ${#PREVIOUS_WRITER_SERVICES[@]} -gt 0 ]]; then
    compose --profile local-api stop -t 45 "${PREVIOUS_WRITER_SERVICES[@]}" || stop_status=$?
  fi
  if [[ "$stop_status" -ne 0 ]]; then
    start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || true
    return "$stop_status"
  fi
  backup_archive "$kind" "$include_downloads" || backup_status=$?
  start_services false "${PREVIOUS_PROJECT_SERVICES[@]}" || restore_status=$?
  if [[ "$restore_status" -eq 0 ]]; then
    verify_services_healthy "${PREVIOUS_PROJECT_SERVICES[@]}" || restore_status=$?
  fi
  if [[ "$restore_status" -eq 0 ]]; then
    verify_exact_project_service_state "${PREVIOUS_PROJECT_SERVICES[@]}" || restore_status=$?
  fi
  if [[ "$restore_status" -ne 0 ]]; then
    echo "Backup service-state restoration failed." >&2
    return "$restore_status"
  fi
  return "$backup_status"
}

run_migration_export() {
  local include_downloads=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --include-downloads) include_downloads=1 ;;
      *)
        echo "Usage: tmb migration export [--include-downloads]" >&2
        return 2
        ;;
    esac
    shift
  done
  if [[ "$include_downloads" == "1" ]]; then
    echo "Migration export will include data/downloads. This can make the archive very large."
    require_confirmation "INCLUDE-DOWNLOADS" || return 1
  fi
  acquire_management_lock || return 1
  perform_consistent_backup_kind migration "$include_downloads"
}