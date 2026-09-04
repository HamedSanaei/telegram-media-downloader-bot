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

  # Telegram / logger / channels / Local Bot API display. Every value is
  # derived from the authoritative application CLI (Pydantic configuration and
  # runtime probes on the compose network), so this dashboard can never
  # contradict `tmb telegram status`, `tmb local-api status`, or `tmb doctor`.
  # Values that cannot be obtained are reported as unavailable, never guessed
  # from partial raw greps.
  local telegram_status api_mode token_state logger_state required_state
  telegram_status="$(compose_run_app telegram-media-bot config-edit \
    --config /app/config.yaml telegram-status 2>/dev/null || true)"
  if [[ -n "$telegram_status" ]]; then
    token_state="$(printf '%s\n' "$telegram_status" | sed -n 's/^bot_token: //p' | head -n 1)"
    api_mode="$(printf '%s\n' "$telegram_status" | sed -n 's/^api_mode: //p' | head -n 1)"
    logger_state="$(printf '%s\n' "$telegram_status" | sed -n 's/^logger: //p' | head -n 1)"
    required_state="$(printf '%s\n' "$telegram_status" | sed -n 's/^required_channels: \([a-z]*\).*/\1/p' | head -n 1)"
    if [[ "$token_state" == "configured" || "$token_state" == "not configured" ]]; then
      printf 'Telegram: token %s' "$token_state"
      if [[ "$api_mode" == "local" ]]; then
        printf ', Local Bot API mode'
      elif [[ "$api_mode" == "cloud" ]]; then
        printf ', Cloud Bot API mode'
      fi
      printf '\n'
    else
      printf 'Telegram: unavailable\n'
    fi
  else
    printf 'Telegram: unavailable\n'
  fi
  printf 'Operator Logger: %s\n' "${logger_state:-unavailable}"
  printf 'Required channels policy: %s\n' "${required_state:-unavailable}"
  if [[ "${logger_state:-}" == "enabled" ]]; then
    local logger_outbox active effective pending
    logger_outbox="$(compose_run_app telegram-media-bot config-edit \
      --config /app/config.yaml logger-status 2>/dev/null || true)"
    if [[ -n "$logger_outbox" ]]; then
      active="$(printf '%s\n' "$logger_outbox" | sed -n 's/^active_destinations: //p' | head -n 1)"
      effective="$(printf '%s\n' "$logger_outbox" | sed -n 's/^effective_destinations: //p' | head -n 1)"
      pending="$(printf '%s\n' "$logger_outbox" | sed -n 's/^pending_effects: //p' | head -n 1)"
      printf 'Logger outbox: effective=%s active=%s pending=%s\n' \
        "${effective:-?}" "${active:-?}" "${pending:-?}"
    else
      printf 'Logger outbox: unavailable\n'
    fi
  fi

  # Local Bot API migration state from the application CLI on the compose
  # network (the local-api hostname only resolves there), so endpoint
  # reachability reflects the running service.
  local local_api_status
  local_api_status="$(compose_run_app telegram-media-bot config-edit \
    --config /app/config.yaml local-api-status 2>/dev/null || true)"
  if [[ -n "$local_api_status" ]]; then
    printf '%s\n' "$local_api_status" | sed 's/^/Local API  /'
  else
    printf 'Local API  migration state unavailable (image or config not ready)\n'
  fi
}