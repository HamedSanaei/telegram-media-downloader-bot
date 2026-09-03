# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Diagnostics: doctor, config-check, SQLite/queue health, Redis state.
# No private job content is ever exposed.
# --------------------------------------------------------------------------- #

run_doctor() {
  local -a arguments=(telegram-media-bot doctor --config /app/config.yaml)
  if [[ "${1:-}" == "--offline" ]]; then
    arguments+=(--offline)
    shift
  elif [[ "${1:-}" == "--online-service" && -n "${2:-}" ]]; then
    arguments+=(--online-service "$2")
    shift 2
  elif [[ -n "${1:-}" ]]; then
    echo "Usage: tmb doctor [--offline] [--online-service bot|local-api]" >&2
    return 2
  fi
  compose --profile local-api run --rm --no-deps worker "${arguments[@]}"
}

run_config_check() {
  compose --profile local-api run --rm --no-deps worker \
    telegram-media-bot config-check --config /app/config.yaml
}

run_db_health() {
  local database="$ROOT_DIR/data/state/jobs.sqlite3"
  if [[ ! -f "$database" ]]; then
    echo "SQLite database not found: $database"
    return 0
  fi
  local size wal shm
  size="$(human_size "$(file_size_bytes "$database")")"
  wal="$(human_size "$(file_size_bytes "$database-wal")")"
  shm="$(human_size "$(file_size_bytes "$database-shm")")"
  echo "SQLite database: $database"
  echo "  size: $size  WAL: $wal  SHM: $shm"
  echo "  integrity_check:"
  docker run --rm --user "$(runtime_identity APP_UID 10001):$(runtime_identity APP_GID 10001)" \
    -v "$ROOT_DIR/data:/data:ro" \
    "$(configured_image)" python -c '
import sqlite3
connection = sqlite3.connect("file:/data/state/jobs.sqlite3?immutable=1", uri=True)
try:
    print("    " + connection.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    connection.close()
' 2>/dev/null || echo "    unavailable"
  echo "Queue health:"
  docker exec "$(compose --profile local-api ps -q redis 2>/dev/null || true)" \
    redis-cli ping 2>/dev/null | sed 's/^/    redis: /' || echo "    redis: unavailable"
  docker exec "$(compose --profile local-api ps -q redis 2>/dev/null || true)" \
    redis-cli info persistence 2>/dev/null | grep -E 'rdb_last_save|aof_enabled' | sed 's/^/    /' || true
  echo "Logger outbox:"
  config_edit_run logger-status 2>/dev/null | sed 's/^/    /' || echo "    unavailable"
}