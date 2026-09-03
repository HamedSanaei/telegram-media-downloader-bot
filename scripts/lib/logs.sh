# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Log browsing and the sanitized diagnostic support bundle. All log output
# passes through the central redaction filter; secrets never leave the host.
# --------------------------------------------------------------------------- #

# Parse service/tail/since/follow options: tmb logs [service] [--tail N] [--since 2h] [-f]
parse_log_options() {
  LOG_SERVICE=""
  LOG_TAIL=""
  LOG_SINCE=""
  LOG_FOLLOW=0
  LOG_ERRORS=0
  local arg
  for arg in "$@"; do
    case "$arg" in
      --tail)
        # handled by caller pairing
        ;;
      --tail=*)
        LOG_TAIL="${arg#*=}"
        ;;
      --since)
        ;;
      --since=*)
        LOG_SINCE="${arg#*=}"
        ;;
      -f|--follow)
        LOG_FOLLOW=1
        ;;
      errors|--errors)
        LOG_ERRORS=1
        ;;
      -h|--help)
        return 2
        ;;
      --*)
        echo "Unknown log option: $arg" >&2
        return 2
        ;;
      *)
        LOG_SERVICE="$arg"
        ;;
    esac
  done
  if [[ "$LOG_TAIL" =~ ^[0-9]+$ ]] || [[ -z "$LOG_TAIL" ]]; then
    :
  else
    echo "Invalid --tail value: $LOG_TAIL" >&2
    return 2
  fi
}

run_logs() {
  local -a normalized=()
  local previous="" arg
  for arg in "$@"; do
    if [[ -n "$previous" ]]; then
      normalized+=("$previous=$arg")
      previous=""
      continue
    fi
    case "$arg" in
      --tail|--since) previous="$arg" ;;
      *) normalized+=("$arg") ;;
    esac
  done
  if [[ -n "$previous" ]]; then
    echo "Missing value for $previous" >&2
    return 2
  fi
  if ! parse_log_options "${normalized[@]}"; then
    echo "Usage: tmb logs [bot|worker|local-api|redis] [--tail N] [--since 2h] [-f|--follow] [errors]" >&2
    return 2
  fi
  local -a compose_logs=(logs --no-log-prefix --timestamps)
  if [[ "$LOG_ERRORS" == "1" ]]; then
    compose_logs+=(--since "${LOG_SINCE:-24h}")
  elif [[ -n "$LOG_SINCE" ]]; then
    compose_logs+=(--since "$LOG_SINCE")
  fi
  if [[ "$LOG_FOLLOW" == "1" ]]; then
    compose_logs+=(--follow)
  elif [[ "$LOG_ERRORS" != "1" ]]; then
    compose_logs+=(--tail "${LOG_TAIL:-100}")
  fi
  if [[ -n "$LOG_SERVICE" ]]; then
    case "$LOG_SERVICE" in
      bot|worker|local-api|redis) compose_logs+=("$LOG_SERVICE") ;;
      *)
        echo "Unknown service: $LOG_SERVICE (expected bot|worker|local-api|redis)" >&2
        return 2
        ;;
    esac
  fi
  if [[ "$LOG_ERRORS" == "1" ]]; then
    compose --profile local-api "${compose_logs[@]}" 2>&1 |
      grep -iE 'error|fail|exception|traceback|critical' |
      sanitize_stream
  else
    compose --profile local-api "${compose_logs[@]}" 2>&1 | sanitize_stream
  fi
}

run_log_disk_usage() {
  local total
  total="$(docker system df 2>/dev/null | grep -E '^Containers|^Local Volumes' || true)"
  printf '%s\n' "$total"
  echo "Container log files:"
  docker ps -aq 2>/dev/null | while IFS= read -r container; do
    local size
    size="$(docker inspect --format '{{.LogPath}}' "$container" 2>/dev/null || true)"
    [[ -n "$size" ]] || continue
    printf '  %s %s\n' "$container" "$(human_size "$(file_size_bytes "$size")")"
  done
}

# Sanitized diagnostic support bundle -----------------------------------
create_support_bundle() {
  local stamp bundle temporary
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p backups
  bundle="backups/support-bundle-${stamp}.tar.gz"
  temporary="$(mktemp -d)" || return 1
  mkdir -p "$temporary/bundle"

  {
    echo "version: $(installed_version)"
    echo "image: $(configured_image)"
    echo "digest: $(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}none{{end}}' "$(configured_image)" 2>/dev/null || echo unknown)"
    echo "created_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$temporary/bundle/version.txt"

  {
    echo "os: $(uname -s -r -m 2>/dev/null || true)"
    echo "docker: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
    echo "compose: $(docker compose version 2>/dev/null || echo unknown)"
  } >"$temporary/bundle/environment.txt"

  compose --profile local-api ps -a 2>/dev/null | sanitize_stream >"$temporary/bundle/services.txt" || true
  df -hP / 2>/dev/null >"$temporary/bundle/disk.txt" || true
  storage_overview | sanitize_stream >"$temporary/bundle/storage.txt" || true

  local service
  for service in bot worker local-api redis; do
    compose --profile local-api logs --no-log-prefix --timestamps --tail 200 "$service" 2>/dev/null |
      sanitize_stream >"$temporary/bundle/logs-$service.txt" || true
  done

  run_redacted "doctor" compose --profile local-api run --rm --no-deps worker \
    telegram-media-bot doctor --config /app/config.yaml >"$temporary/bundle/doctor.txt" 2>&1 || true
  run_redacted "config-check" compose --profile local-api run --rm --no-deps worker \
    telegram-media-bot config-check --config /app/config.yaml >"$temporary/bundle/config-check.txt" 2>&1 || true
  docker run --rm --user "$(runtime_identity APP_UID 10001):$(runtime_identity APP_GID 10001)" \
    -v "$ROOT_DIR/data:/data:ro" \
    "$(configured_image)" python -c '
import sqlite3
connection = sqlite3.connect("file:/data/state/jobs.sqlite3?immutable=1", uri=True)
try:
    print(connection.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    connection.close()
' 2>/dev/null | sanitize_stream >"$temporary/bundle/sqlite.txt" || true
  {
    echo "redis ping:"
    docker exec "$(compose --profile local-api ps -q redis 2>/dev/null || true)" redis-cli ping 2>/dev/null || echo "unavailable"
  } | sanitize_stream >"$temporary/bundle/redis.txt" || true

  (
    umask 077
    tar --force-local -czf "$temporary/bundle.tar.gz" -C "$temporary" bundle
  ) || {
    rm -rf -- "$temporary"
    return 1
  }
  mv -f -- "$temporary/bundle.tar.gz" "$bundle"
  chmod 600 "$bundle"
  rm -rf -- "$temporary"
  echo "Support bundle created: $bundle"
  echo "It contains only sanitized diagnostics; config.yaml, cookies, and secrets are excluded."
}