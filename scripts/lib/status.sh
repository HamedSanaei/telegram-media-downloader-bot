# shellcheck shell=bash
# --------------------------------------------------------------------------- #
# Dashboard / status. Every probe is guarded so partial environments degrade
# to "unknown" instead of failing; nothing secret is ever printed.
# --------------------------------------------------------------------------- #

container_field() {
  local container="$1" format="$2" value
  value="$(docker inspect --format "$format" "$container" 2>/dev/null || true)"
  printf '%s' "$value"
}

container_is_running() {
  local container="$1" state
  [[ -n "$container" ]] || return 1
  state="$(container_field "$container" '{{.State.Status}}')"
  [[ "$state" == "running" ]]
}

project_image_reclaimable_bytes() {
  # Approximate reclaimable space: project-repository image IDs not referenced
  # by any container and not equal to the configured image.
  local current_id repository image_id state
  current_id="$(docker image inspect --format '{{.Id}}' "$(configured_image)" 2>/dev/null || true)"
  local -a referenced_ids=() candidate_ids=()
  local total=0
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    image_id="$(docker inspect --format '{{.Image}}' "$container" 2>/dev/null || true)"
    [[ -n "$image_id" ]] && referenced_ids+=("$image_id")
  done < <(docker ps -aq 2>/dev/null || true)
  while IFS='|' read -r repository image_id; do
    [[ -n "$image_id" && "$repository" == "$IMAGE_REPOSITORY" ]] || continue
    [[ "$image_id" == "$current_id" ]] && continue
    if printf '%s\n' "${referenced_ids[@]}" | grep -Fxq -- "$image_id"; then
      continue
    fi
    if ! printf '%s\n' "${candidate_ids[@]}" | grep -Fxq -- "$image_id"; then
      candidate_ids+=("$image_id")
    fi
  done < <(docker image ls --no-trunc --format '{{.Repository}}|{{.ID}}' 2>/dev/null || true)
  for image_id in "${candidate_ids[@]}"; do
    local size
    size="$(docker image inspect --format '{{.Size}}' "$image_id" 2>/dev/null || printf '0')"
    [[ "$size" =~ ^[0-9]+$ ]] || size=0
    total=$((total + size))
  done
  printf '%s' "$total"
}

redis_volume_bytes() {
  # The named volume is project-scoped (compose project telegram-media-downloader).
  local volume mountpoint
  volume="$(docker volume ls --format '{{.Name}}' 2>/dev/null |
    grep -E 'telegram-media-downloader[_-]redis-data$' | head -n 1 || true)"
  if [[ -n "$volume" ]]; then
    mountpoint="$(docker volume inspect --format '{{.Mountpoint}}' "$volume" 2>/dev/null || true)"
    if [[ -n "$mountpoint" ]]; then
      printf '%s' "$(human_size "$(dir_size_bytes "$mountpoint")")"
      return 0
    fi
  fi
  printf 'n/a'
}

run_status() {
  local service container state health restart_count started
  local version image digest
  version="$(installed_version)"
  image="$(configured_image)"
  digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}none{{end}}' "$image" 2>/dev/null || true)"
  printf 'Application version: %s\n' "${version:-unknown}"
  printf 'Configured image: %s\n' "$image"
  printf 'Image digest: %s\n' "${digest:-unknown}"

  for service in "${PROJECT_SERVICES[@]}"; do
    container="$(compose --profile local-api ps -q "$service" 2>/dev/null || true)"
    if [[ -z "$container" ]]; then
      printf '%-12s %s\n' "$service" "not created"
      continue
    fi
    state="$(container_field "$container" '{{.State.Status}}')"
    health="$(container_field "$container" '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
    restart_count="$(container_field "$container" '{{.RestartCount}}')"
    [[ "$restart_count" =~ ^[0-9]+$ ]] || restart_count="unknown"
    started="$(container_field "$container" '{{.State.StartedAt}}')"
    case "$started" in
      [0-9][0-9][0-9][0-9]-*) ;;
      *) started="unknown" ;;
    esac
    printf '%-12s state=%-9s health=%-8s restarts=%-6s started=%s\n' \
      "$service" "${state:-unknown}" "${health:-unknown}" "$restart_count" "$started"
  done

  # CPU/memory summary when Docker stats is available and quiet. Only the
  # containers of this compose project are matched, never foreign projects.
  local stats project_ids
  project_ids="$(compose --profile local-api ps -q 2>/dev/null | tr '\n' '|' | sed 's/|$//')"
  if command -v timeout >/dev/null 2>&1; then
    stats="$(timeout 10 docker stats --no-stream --format '{{.ID}} {{.Name}} {{.CPUPerc}} {{.MemUsage}}' 2>/dev/null || true)"
  else
    stats="$(docker stats --no-stream --format '{{.ID}} {{.Name}} {{.CPUPerc}} {{.MemUsage}}' 2>/dev/null || true)"
  fi
  if [[ -n "$stats" && -n "$project_ids" ]]; then
    printf '%s\n' "$stats" | while IFS= read -r line; do
      local stats_id="${line%% *}"
      if printf '%s\n' "$project_ids" | tr '|' '\n' | grep -Fxq -- "$stats_id"; then
        printf 'stats   %s\n' "$line"
      fi
    done
  else
    printf 'stats   unavailable\n'
  fi

  # Filesystem and directory usage.
  local root_usage data_size downloads_size temp_size state_size cookies_size \
    local_api_size backups_size db_size
  root_usage="$(df -hP / 2>/dev/null | tail -n 1 | awk '{print $3 " used / " $2 " (" $5 ")"}')"
  printf 'Root filesystem: %s\n' "${root_usage:-unknown}"
  data_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data")")"
  downloads_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data/downloads")")"
  temp_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data/temp")")"
  state_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data/state")")"
  cookies_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data/cookies")")"
  local_api_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/data/telegram-bot-api")")"
  backups_size="$(human_size "$(dir_size_bytes "$ROOT_DIR/backups")")"
  db_size="$(human_size "$(file_size_bytes "$ROOT_DIR/data/state/jobs.sqlite3")")"
  printf 'Data directory: %s (downloads %s, temp %s, state %s, cookies %s, local-api %s)\n' \
    "$data_size" "$downloads_size" "$temp_size" "$state_size" "$cookies_size" "$local_api_size"
  printf 'SQLite database: %s\n' "$db_size"
  printf 'Backup directory: %s\n' "$backups_size"
  printf 'Redis volume: %s\n' "$(redis_volume_bytes)"
  printf 'Project image reclaimable: %s\n' \
    "$(human_size "$(project_image_reclaimable_bytes)")"

  # Telegram / logger / channels / migration display (config read-only).
  if config_bot_token_configured; then
    printf 'Telegram: token configured'
  else
    printf 'Telegram: not configured'
  fi
  if config_flag '^  local_api_is_local: *true'; then
    printf ', Local Bot API mode'
  else
    printf ', Cloud Bot API mode'
  fi
  printf '\n'
  local logger_state required_state
  logger_state="disabled"
  if grep -A 6 '^  logger:' "$ROOT_DIR/config.yaml" 2>/dev/null | grep -m1 '^  enabled:' | grep -q 'true'; then
    logger_state="enabled"
  fi
  required_state="disabled"
  if grep -A 6 '^  required_channels:' "$ROOT_DIR/config.yaml" 2>/dev/null | grep -m1 '^  enabled:' | grep -q 'true'; then
    required_state="enabled"
  fi
  printf 'Operator Logger: %s\n' "$logger_state"
  printf 'Required channels policy: %s\n' "$required_state"

  # Local Bot API migration state from the application CLI when possible.
  local local_api_status
  local_api_status="$(
    docker run --rm --user "$(id -u):$(id -g)" \
      -v "$ROOT_DIR/config.yaml:/app/config.yaml:ro" \
      "$(configured_image)" \
      telegram-media-bot config-edit local-api-status 2>/dev/null || true
  )"
  if [[ -n "$local_api_status" ]]; then
    printf '%s\n' "$local_api_status" | sed 's/^/Local API  /'
  else
    printf 'Local API  migration state unavailable (image or config not ready)\n'
  fi
}