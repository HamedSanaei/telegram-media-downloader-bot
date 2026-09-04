#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------------------------------------- #
# Privileged Local Bot API container health integration test.
#
# Boots the REAL production compose `local-api` service (the image-bundled
# telegram-bot-api binary via `telegram-media-bot local-api serve`), waits for
# the compose healthcheck to report healthy, keeps it healthy long enough to
# catch restart-loop behavior, and asserts RestartCount == 0. It also verifies
# that the app-level status surface agrees with the running container
# (endpoint_reachable/process_running true) - the same contract `tmb status`,
# `tmb local-api status`, and `tmb doctor` rely on.
#
# Requires RUN_PRIVILEGED_UPGRADE_TESTS=1 and a locally loaded image tagged
# `telegram-media-downloader-bot:ci` (exactly what the CI lanes provide).
# --------------------------------------------------------------------------- #

if [[ "${RUN_PRIVILEGED_UPGRADE_TESTS:-0}" != "1" ]]; then
  echo "Privileged Local Bot API container health integration test skipped."
  exit 0
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
INSTALL_ROOT="$TEST_ROOT/installation"
RUNTIME_UID="$(id -u)"
RUNTIME_GID="$(id -g)"
COMPOSE_PROJECT_NAME="tmb-lapi-health-$$"

cleanup() {
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

for command in bash docker; do
  command -v "$command" >/dev/null
done

mkdir -p "$INSTALL_ROOT/data/state" "$INSTALL_ROOT/data/cookies" \
  "$INSTALL_ROOT/data/downloads" "$INSTALL_ROOT/data/temp" \
  "$INSTALL_ROOT/data/telegram-bot-api" "$INSTALL_ROOT/backups"
cat >"$INSTALL_ROOT/config.yaml" <<'EOF'
app:
  environment: production
  log_level: INFO
  log_format: json
  language: fa
  timezone: Asia/Tehran
telegram:
  bot_token: "123456:LOCAL_API_HEALTH_FAKE_TOKEN"  # pragma: allowlist secret
  admin_ids: []
  upload_as_document: true
  max_upload_size_mb: 49
  upload_timeout_seconds: 14400
  local_api_base_url: http://local-api:8081
  local_api_is_local: true
  local_bot_api:
    enabled: true
    mode: managed
    executable: /usr/local/bin/telegram-bot-api
    api_id: 12345
    api_hash: "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    host: 0.0.0.0
    port: 8081
    local_mode: true
    working_directory: /data/telegram-bot-api
    temp_directory: /data/telegram-bot-api/temp
    log_file: /data/telegram-bot-api/telegram-bot-api.log
    lifecycle_owner: service
    startup_timeout_seconds: 60
    migration:
      auto_logout_from_cloud: false
      state_file: /data/state/telegram-api-migration.json
  required_channels:
    enabled: false
    positive_cache_ttl_seconds: 300
    negative_cache_ttl_seconds: 30
    channels: []
redis:
  url: redis://redis:6379/0
  queue_name: media-downloads
queue:
  max_jobs: 3
  job_timeout_seconds: 1800
  max_tries: 2
  keep_result_seconds: 3600
  retry_delay_seconds: 15
storage:
  root_directory: /data
  downloads_directory: downloads
  temp_directory: temp
  state_directory: state
  delete_after_upload: true
  orphan_grace_seconds: 300
  job_retention_days: 30
media:
  enabled_sources:
    - youtube
    - soundcloud
    - instagram
    - twitter
    - pinterest
    - tiktok
  enabled_modes:
    - best
    - best_original
    - video_1080
    - video_720
    - audio_best
    - audio_mp3
  default_mode: best
  allow_playlists: false
  playlist_max_items: 20
  max_file_size_mb: 49
  max_source_size_mb: 1024
  max_duration_seconds: 14400
  mp4_native_fallback: lower_resolution
  formats:
    best: bv*+ba/b
    best_original: bv*+ba/b
    video_2160: bv*[height<=2160]+ba/b[height<=2160]
    video_1440: bv*[height<=1440]+ba/b[height<=1440]
    video_1080: bv*[height<=1080]+ba/b[height<=1080]
    video_720: bv*[height<=720]+ba/b[height<=720]
    video_480: bv*[height<=480]+ba/b[height<=480]
    audio_best: ba/b
    audio_mp3: ba/b
  instagram:
    auto_download: true
    force_mp4: true
    ignore_images: true
    max_videos: 50
    max_total_size_mb: 4096
  transcode:
    enabled: true
    explicit_mp4_enabled: false
    threads: 2
    max_concurrent: 1
    timeout_seconds: 1500
    progress_interval_seconds: 10
  workspace:
    cleanup_on_success: true
    cleanup_on_failure: true
    cleanup_on_cancel: true
    cleanup_on_timeout: true
multipart:
  enabled: true
  seven_zip_executable: 7zz
  part_size_mb: 1850
  max_total_size_mb: 4096
  compression_level: 0
yt_dlp:
  cookies_file: null
  proxy_enabled: false
  proxy: null
  socket_timeout_seconds: 30
  retries: 5
  fragment_retries: 10
  concurrent_fragments: 4
  extractor_retries: 3
  restrict_filenames: true
  write_thumbnail: false
  embed_metadata: true
  embed_thumbnail: false
  audio_format: mp3
  audio_quality: "192"
  user_agent: null
  javascript_runtime: deno
security:
  allowed_user_ids: []
  blocked_user_ids: []
  requests_per_minute: 5
  reject_private_network_urls: true
persistence:
  database_filename: jobs.sqlite3
  selection_ttl_seconds: 600
  cleanup_interval_seconds: 60
observability:
  health_host: 0.0.0.0
  health_port: 8080
  telegram_readiness_check: true
  metrics_enabled: true
operations:
  update:
    prune_old_project_images_after_success: true
EOF
cat >"$INSTALL_ROOT/.env" <<EOF
TMB_IMAGE=telegram-media-downloader-bot:ci
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}
COMPOSE_PROFILES=local-api
APP_UID=${RUNTIME_UID}
APP_GID=${RUNTIME_GID}
EOF
# Exact production Compose contract: the real service command
# (`telegram-media-bot local-api ... serve`), read-only config, writable data,
# runtime user, and the TCP healthcheck on 127.0.0.1:8081.
cp "$SOURCE_ROOT/docker-compose.yml" "$INSTALL_ROOT/docker-compose.yml"
sed -i '/^name: telegram-media-downloader$/d' "$INSTALL_ROOT/docker-compose.yml"
chmod 600 "$INSTALL_ROOT/.env" "$INSTALL_ROOT/config.yaml"

# Boot ONLY the Local Bot API service - the exact command from the task.
docker compose --project-directory "$INSTALL_ROOT" --profile local-api up -d local-api

LOCAL_API_CONTAINER="$(docker compose --project-directory "$INSTALL_ROOT" --profile local-api ps -q local-api)"
[[ -n "$LOCAL_API_CONTAINER" ]] || {
  echo "local-api container was not created." >&2
  exit 1
}

health_state() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$LOCAL_API_CONTAINER" 2>/dev/null || echo "none"
}

HEALTHY=0
for _attempt in {1..60}; do
  state="$(health_state)"
  if [[ "$state" == "healthy" ]]; then
    HEALTHY=1
    break
  fi
  if docker inspect --format '{{.State.Status}}' "$LOCAL_API_CONTAINER" 2>/dev/null |
    grep -Eq '^(exited|dead)$'; then
    echo "local-api exited before becoming healthy." >&2
    docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs local-api >&2 || true
    exit 1
  fi
  sleep 4
done
if [[ "$HEALTHY" != "1" ]]; then
  echo "local-api did not become healthy within the timeout (state: $(health_state))." >&2
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs local-api >&2 || true
  exit 1
fi
echo "OK local-api is healthy."

# Stay healthy long enough to catch restart-loop behavior, then re-check.
sleep 20
state="$(health_state)"
[[ "$state" == "healthy" ]] || {
  echo "local-api lost health after startup (state: $state)." >&2
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs local-api >&2 || true
  exit 1
}
RESTARTS="$(docker inspect --format '{{.RestartCount}}' "$LOCAL_API_CONTAINER")"
[[ "$RESTARTS" == "0" ]] || {
  echo "local-api RestartCount is $RESTARTS; the service is restart-looping." >&2
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs local-api >&2 || true
  exit 1
}
echo "OK local-api RestartCount=0 after sustained health."

# The app-level status surface must agree with the running container: the same
# probe `tmb status` / `tmb local-api status` use, on the compose network.
STATUS_OUTPUT="$(
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api \
    run --rm --no-deps worker \
    telegram-media-bot local-api --config /app/config.yaml status
)"
printf '%s\n' "$STATUS_OUTPUT" | grep -Fxq "enabled: true"
printf '%s\n' "$STATUS_OUTPUT" | grep -Fxq "endpoint_reachable: true"
printf '%s\n' "$STATUS_OUTPUT" | grep -Fxq "process_running: true"
echo "OK app-level local-api status agrees with the healthy container."

echo "Privileged Local Bot API container health integration test passed."