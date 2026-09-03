# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Service lifecycle: compose wrapper, exact service-state tracking, and
# health verification. Ordinary stop/start/restart never deletes persistent
# data; the `unless-stopped` restart contract is preserved.
# --------------------------------------------------------------------------- #

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

load_running_project_services() {
  local service output
  output="$(compose --profile local-api ps --services --filter status=running)" || return 1
  PREVIOUS_PROJECT_SERVICES=()
  PREVIOUS_WRITER_SERVICES=()
  while IFS= read -r service; do
    case "$service" in
      bot|worker|local-api|redis) PREVIOUS_PROJECT_SERVICES+=("$service") ;;
    esac
    case "$service" in
      bot|worker|local-api) PREVIOUS_WRITER_SERVICES+=("$service") ;;
    esac
  done <<<"$output"
}

list_contains() {
  local needle="$1" item
  shift
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

verify_exact_project_service_state() {
  local output service should_run is_running
  local -a running_services=()
  output="$(compose --profile local-api ps --services --filter status=running)" || return 1
  while IFS= read -r service; do
    case "$service" in
      bot|worker|local-api|redis) running_services+=("$service") ;;
    esac
  done <<<"$output"
  for service in "${PROJECT_SERVICES[@]}"; do
    should_run=false
    is_running=false
    list_contains "$service" "$@" && should_run=true
    list_contains "$service" "${running_services[@]}" && is_running=true
    if [[ "$should_run" != "$is_running" ]]; then
      echo "Service-state mismatch for $service (expected running: $should_run)." >&2
      return 1
    fi
  done
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
        echo "Service $service entered a crash/restart state." >&2
        compose --profile local-api stop "$service" || true
        return 1
      fi
      all_ready=false
    done
    [[ "$all_ready" == "true" ]] && return 0
    sleep 5
  done
  echo "Services did not become healthy before the timeout." >&2
  compose --profile local-api stop "$@" || true
  return 1
}

# Lifecycle handlers ---------------------------------------------------

run_services() {
  local action="${1:-status}"
  shift || true
  case "$action" in
    start|up)
      compose --profile local-api up -d --no-build
      ;;
    stop|down)
      # Intentionally stopped services are never resurrected by the
      # unless-stopped policy; persistent data and volumes are preserved.
      compose --profile local-api down
      ;;
    restart|recreate)
      compose --profile local-api up -d --no-build --force-recreate
      ;;
    ps|show|status)
      compose --profile local-api ps
      ;;
    health)
      run_services_health
      ;;
    start-one)
      local service="${1:-}"
      [[ -n "$service" ]] || { echo "Usage: tmb services start-one bot|worker|local-api|redis" >&2; return 2; }
      compose --profile local-api up -d --no-build "$service"
      ;;
    stop-one)
      local service="${1:-}"
      [[ -n "$service" ]] || { echo "Usage: tmb services stop-one bot|worker|local-api|redis" >&2; return 2; }
      compose --profile local-api stop "$service"
      ;;
    restart-one)
      local service="${1:-}"
      [[ -n "$service" ]] || { echo "Usage: tmb services restart-one bot|worker|local-api|redis" >&2; return 2; }
      compose --profile local-api up -d --no-build --force-recreate "$service"
      ;;
    *)
      echo "Usage: tmb services start|stop|restart|recreate|ps|health|start-one|stop-one|restart-one" >&2
      return 2
      ;;
  esac
}

# Backward-compatible service commands ---------------------------------
run_start() {
  compose --profile local-api up -d --no-build
}

run_stop() {
  compose --profile local-api down
}

run_restart() {
  compose --profile local-api up -d --no-build --force-recreate
}

run_services_health() {
  local service container state health restart_count started
  for service in "${PROJECT_SERVICES[@]}"; do
    container="$(compose --profile local-api ps -q "$service" 2>/dev/null || true)"
    if [[ -z "$container" ]]; then
      printf '%-10s %s\n' "$service" "not created"
      continue
    fi
    state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)"
    restart_count="$(docker inspect --format '{{.RestartCount}}' "$container" 2>/dev/null || true)"
    [[ "$restart_count" =~ ^[0-9]+$ ]] || restart_count="unknown"
    started="$(docker inspect --format '{{.State.StartedAt}}' "$container" 2>/dev/null || true)"
    printf '%-10s state=%-10s health=%-9s restarts=%s started=%s\n' \
      "$service" "${state:-unknown}" "${health:-unknown}" "$restart_count" "${started:-unknown}"
  done
}