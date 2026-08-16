#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_PRIVILEGED_UPGRADE_TESTS:-0}" != "1" ]]; then
  echo "Privileged Local API readiness integration test skipped."
  exit 0
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
INSTALL_ROOT="$TEST_ROOT/installation"
RUNTIME_UID="$(id -u)"
RUNTIME_GID="$(id -g)"
COMPOSE_PROJECT_NAME="tmb-readiness-test-$$"
STUB_PATH="$TEST_ROOT/stub.py"
DELAY_SECONDS="${TMB_TEST_LOCAL_API_DELAY_SECONDS:-15}"
TIMEOUT_SECONDS="${TMB_TEST_LOCAL_API_TIMEOUT_SECONDS:-3}"

cleanup() {
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

for command in bash docker python; do
  command -v "$command" >/dev/null
done

cat >"$STUB_PATH" <<'PY'
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
time.sleep(delay)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.endswith("/getUpdates"):
            body = {"ok": True, "result": []}
        elif self.path.endswith("/getMe"):
            body = {
                "ok": True,
                "result": {
                    "id": 123456789,
                    "is_bot": True,
                    "first_name": "Readiness",
                    "username": "readiness_test_bot",
                },
            }
        else:
            body = {"ok": True, "result": {}}
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


HTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
PY

mkdir -p "$INSTALL_ROOT/data/state" "$INSTALL_ROOT/data/cookies" \
  "$INSTALL_ROOT/data/downloads" "$INSTALL_ROOT/data/temp" \
  "$INSTALL_ROOT/data/telegram-bot-api" "$INSTALL_ROOT/backups"
cat >"$INSTALL_ROOT/config.yaml" <<EOF
app:
  environment: production
  log_level: INFO
  log_format: json
  language: fa
  timezone: Asia/Tehran
telegram:
  bot_token: "123456:READINESS_TEST_FAKE_TOKEN"
  admin_ids: []
  upload_as_document: true
  max_upload_size_mb: 49
  upload_timeout_seconds: 14400
  local_api_base_url: http://local-api:8081
  local_api_is_local: true
  local_bot_api:
    enabled: true
    mode: external
    host: 0.0.0.0
    port: 8081
    local_mode: true
    working_directory: /data/telegram-bot-api
    temp_directory: /data/telegram-bot-api/temp
    log_file: /data/telegram-bot-api/telegram-bot-api.log
    lifecycle_owner: service
    startup_timeout_seconds: ${TMB_TEST_LOCAL_API_STARTUP_TIMEOUT_SECONDS:-30}
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
python - "$INSTALL_ROOT/data/state/telegram-api-migration.json" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps({"version": 1, "phase": "local", "updated_at": "2026-08-16T00:00:00+00:00"}),
    encoding="utf-8",
)
PY
cat >"$INSTALL_ROOT/.env" <<EOF
TMB_IMAGE=telegram-media-downloader-bot:ci
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}
COMPOSE_PROFILES=local-api
APP_UID=${RUNTIME_UID}
APP_GID=${RUNTIME_GID}
EOF
# Use the exact production Compose contract (read-only config, writable data, runtime user)
# with the project name driven by the test's unique .env value.
cp "$SOURCE_ROOT/docker-compose.yml" "$INSTALL_ROOT/docker-compose.yml"
sed -i '/^name: telegram-media-downloader$/d' "$INSTALL_ROOT/docker-compose.yml"
cat >"$INSTALL_ROOT/docker-compose.override.yml" <<EOF
services:
  local-api:
    command: ["python", "/stub.py", "${DELAY_SECONDS}"]
    volumes:
      - ${STUB_PATH}:/stub.py:ro
EOF
chmod 600 "$INSTALL_ROOT/.env" "$INSTALL_ROOT/config.yaml"

docker compose --project-directory "$INSTALL_ROOT" --profile local-api up -d redis bot local-api
for _attempt in {1..30}; do
  if docker compose --project-directory "$INSTALL_ROOT" --profile local-api \
    ps --services --filter status=running | grep -Fxq bot; then
    break
  fi
  sleep 2
done

BOT_LOG="$TEST_ROOT/bot.log"
for _attempt in {1..60}; do
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs bot \
    >"$BOT_LOG" 2>&1 || true
  grep -Fq 'bot_started' "$BOT_LOG" && break
  sleep 1
done
grep -Fq 'local_api_startup_wait' "$BOT_LOG"
grep -Fq 'local_api_startup_ready' "$BOT_LOG"
grep -Fq 'bot_started' "$BOT_LOG"

BOT_CONTAINER="$(docker compose --project-directory "$INSTALL_ROOT" --profile local-api ps -q bot)"
LOCAL_API_CONTAINER="$(docker compose --project-directory "$INSTALL_ROOT" --profile local-api ps -q local-api)"
BOT_RESTARTS="$(docker inspect --format '{{.RestartCount}}' "$BOT_CONTAINER")"
LOCAL_API_RESTARTS="$(docker inspect --format '{{.RestartCount}}' "$LOCAL_API_CONTAINER")"
if [[ "$BOT_RESTARTS" != "0" || "$LOCAL_API_RESTARTS" != "0" ]]; then
  echo "Delayed Local API startup caused container restarts: bot=$BOT_RESTARTS local-api=$LOCAL_API_RESTARTS" >&2
  cat "$BOT_LOG" >&2
  exit 1
fi
echo "OK bot RestartCount=$BOT_RESTARTS local-api RestartCount=$LOCAL_API_RESTARTS"

# Permanently unavailable endpoint: bounded wait must fail non-zero without a restart loop.
docker compose --project-directory "$INSTALL_ROOT" --profile local-api stop local-api >/dev/null 2>&1
docker compose --project-directory "$INSTALL_ROOT" --profile local-api rm -f local-api >/dev/null 2>&1
sed -i \
  "s/startup_timeout_seconds: 30/startup_timeout_seconds: ${TIMEOUT_SECONDS}/" \
  "$INSTALL_ROOT/config.yaml"
set +e
docker compose --project-directory "$INSTALL_ROOT" --profile local-api run --rm --no-deps bot \
  >"$TEST_ROOT/unavailable.log" 2>&1
UNAVAILABLE_STATUS=$?
set -e
if [[ "$UNAVAILABLE_STATUS" -eq 0 ]]; then
  echo "Bot unexpectedly started while the Local API endpoint was permanently unavailable." >&2
  cat "$TEST_ROOT/unavailable.log" >&2
  exit 1
fi
grep -Fq 'local_api_startup_timeout' "$TEST_ROOT/unavailable.log"
echo "OK permanently-unavailable endpoint failed after bounded wait (status $UNAVAILABLE_STATUS)."

echo "Privileged Local API startup readiness integration test passed."
