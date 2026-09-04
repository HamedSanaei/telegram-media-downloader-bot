#!/usr/bin/env bash
set -euo pipefail

# --------------------------------------------------------------------------- #
# Privileged migration integration test: source and destination with
# DIFFERENT runtime UID/GID and DIFFERENT absolute installation paths.
#
# Covers the production rc.5 failure: after the state swap the restored
# config.yaml was not readable by the restored runtime identity, so the
# offline doctor failed and the import rolled back. The success path here
# proves the restored config is re-owned to the restored APP_UID/APP_GID
# (mode 0600), and the failure path injects a post-swap offline-doctor
# failure and proves the full automatic rollback restores the destination's
# original owner, mode, and contents.
#
# Requires RUN_PRIVILEGED_UPGRADE_TESTS=1 and a locally loaded image tagged
# `telegram-media-downloader-bot:ci`.
# --------------------------------------------------------------------------- #

if [[ "${RUN_PRIVILEGED_UPGRADE_TESTS:-0}" != "1" ]]; then
  echo "Privileged migration UID/GID integration test skipped."
  exit 0
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
SRC="$TEST_ROOT/source"
DST="$TEST_ROOT/destination"
DST2="$TEST_ROOT/destination-2"
TOKEN="123456:MIGRATION_FAKE_TOKEN_123456"  # pragma: allowlist secret
SRC_UID=10001
SRC_GID=10001

cleanup() {
  export COMPOSE_PROJECT_NAME="tmb-uid-src-$$"
  docker compose --project-directory "$SRC" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  export COMPOSE_PROJECT_NAME="tmb-uid-dst-$$"
  docker compose --project-directory "$DST" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  export COMPOSE_PROJECT_NAME="tmb-uid-dst2-$$"
  docker compose --project-directory "$DST2" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  sudo rm -rf -- "$TEST_ROOT" 2>/dev/null || true
}
trap cleanup EXIT

for command in bash docker python3 sudo; do
  command -v "$command" >/dev/null
done
# The CI runner is not root; the fixture mirrors the updater harness by using
# passwordless sudo for cross-UID host operations and root-run exports/imports.


install_tree() {
  local target="$1"
  mkdir -p "$target"
  cp -a "$SOURCE_ROOT/scripts" "$target/scripts"
  cp "$SOURCE_ROOT/docker-compose.yml" "$target/docker-compose.yml"
  cp "$SOURCE_ROOT/pyproject.toml" "$target/pyproject.toml"
  mkdir -p "$target/data/state" "$target/data/cookies" \
    "$target/data/downloads" "$target/data/temp" \
    "$target/data/telegram-bot-api" "$target/backups"
}

# The fixture cannot reach real Telegram, so the real bot/worker commands
# would crash-loop on fake credentials. Mirroring the established updater
# harness pattern, the bot/worker containers run a stable sleep loop with a
# trivially-true healthcheck so service-state capture/restoration and the
# destination start phase are deterministic. The local-api service is NOT
# overridden: it runs the real bundled telegram-bot-api binary with the
# restored config, which is what validates the BUG 2 ownership repair
# end-to-end (readable 0600 config under the restored APP_UID/GID).
write_service_override() {
  local target="$1"
  cat >"$target/docker-compose.override.yml" <<'EOF'
services:
  bot:
    command: ["sh", "-c", "while :; do sleep 1; done"]
    healthcheck:
      test: ["CMD", "true"]
      interval: 5s
      timeout: 3s
      retries: 3
  worker:
    command: ["sh", "-c", "while :; do sleep 1; done"]
    healthcheck:
      test: ["CMD", "true"]
      interval: 5s
      timeout: 3s
      retries: 3
EOF
}

write_source_config() {
  # $1 = logger enabled (true|false) - the failure archive enables the logger
  # without any durable outbox activity, which fails the offline doctor AFTER
  # the swap (staged config-check does not inspect the logger).
  local logger_enabled="$1"
  # Derive the fixture from config.example.yaml (mirroring the privileged
  # updater harness) so the schema always stays complete; a hand-written
  # subset drifts and rejects at load (e.g. missing FormatSection entries).
  cp "$SOURCE_ROOT/config.example.yaml" "$SRC/config.yaml"
  sed -i \
    -e "s|^  bot_token: CHANGE_ME$|  bot_token: \"$TOKEN\"|" \
    -e 's|^  local_api_base_url: null$|  local_api_base_url: http://local-api:8081|' \
    -e 's|^  local_api_is_local: false$|  local_api_is_local: true|' \
    -e 's|^    executable: CHANGE_ME$|    executable: /usr/local/bin/telegram-bot-api|' \
    -e 's|^    api_id: 0$|    api_id: 12345|' \
    -e 's|^    api_hash: CHANGE_ME$|    api_hash: "0123456789abcdef0123456789abcdef"|' \
    -e 's|^    host: 127.0.0.1$|    host: 0.0.0.0|' \
    -e 's|^    working_directory: ./data/telegram-bot-api$|    working_directory: /data/telegram-bot-api|' \
    -e 's|^    temp_directory: ./data/telegram-bot-api/temp$|    temp_directory: /data/telegram-bot-api/temp|' \
    -e 's|^    log_file: ./data/telegram-bot-api/telegram-bot-api.log$|    log_file: /data/telegram-bot-api/telegram-bot-api.log|' \
    -e 's|^    lifecycle_owner: application$|    lifecycle_owner: service|' \
    -e 's|^      state_file: ./data/state/telegram-api-migration.json$|      state_file: /data/state/telegram-api-migration.json|' \
    -e '/^  local_bot_api:$/ { n; s/enabled: false/enabled: true/; }' \
    -e '/^  required_channels:$/ { n; s/enabled: false/enabled: true/; }' \
    -e 's|^        join_url: "https://t.me/CHANGE_ME"$|        join_url: "https://t.me/required_channel"|' \
    -e 's|^  cookies_file: null$|  cookies_file: /data/cookies/cookies.txt|' \
    "$SRC/config.yaml"
  if [[ "$logger_enabled" == "true" ]]; then
    sed -i '/^  logger:$/ { n; s/enabled: false/enabled: true/; }' "$SRC/config.yaml"
  fi
  chmod 600 "$SRC/config.yaml"
  # Mirror a real production install: install.sh re-owns the private config
  # to the runtime identity. Without this the real local-api service (running
  # as APP_UID here) cannot read the 0600 config or write its data tree.
  # The privileged harness convention is host sudo for cross-UID chowns (the
  # CI runner itself is not root; see test_tmb_upgrade_integration.sh).
  sudo chown "${SRC_UID}:${SRC_GID}" "$SRC/config.yaml"
  [[ -d "$SRC/data" ]] && sudo chown -R "${SRC_UID}:${SRC_GID}" "$SRC/data"
}

# --- Source installation (runtime identity 10001:10001) --------------------
install_tree "$SRC"
write_service_override "$SRC"
# Seed the durable state and cookies BEFORE write_source_config re-owns the
# data tree to the source runtime identity; a non-root CI runner cannot write
# into the 10001-owned directories afterwards.
mkdir -p "$SRC/data/state"
cat >"$SRC/data/state/telegram-api-migration.json" <<'EOF'
{"version": 1, "phase": "local", "updated_at": "2026-09-04T00:00:00+00:00"}
EOF
printf '# Netscape HTTP Cookie File\n.fixture.example\tTRUE\t/\tTRUE\t0\tfixture\tvalue\n' \
  >"$SRC/data/cookies/cookies.txt"
chmod 600 "$SRC/data/cookies/cookies.txt"
write_source_config false
cat >"$SRC/.env" <<EOF
TMB_IMAGE=telegram-media-downloader-bot:ci
COMPOSE_PROJECT_NAME=tmb-uid-src-$$
COMPOSE_PROFILES=local-api
APP_UID=${SRC_UID}
APP_GID=${SRC_GID}
EOF
chmod 600 "$SRC/.env"

export COMPOSE_PROJECT_NAME="tmb-uid-src-$$"
docker compose --project-directory "$SRC" --profile local-api up -d --no-build redis bot worker local-api
for _attempt in {1..60}; do
  running="$(docker compose --project-directory "$SRC" --profile local-api \
    ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
  [[ "$running" == *"bot"* && "$running" == *"worker"* && "$running" == *"redis"* ]] && break
  sleep 2
done
running="$(docker compose --project-directory "$SRC" --profile local-api \
  ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
[[ "$running" == *"bot"* && "$running" == *"worker"* && "$running" == *"redis"* ]] || {
  echo "source services did not start: $running" >&2
  exit 1
}
echo "OK source services running: $running"

# The source data tree is owned by the source runtime identity (0600/0700),
# so the export that reads it runs as root - the updater-harness convention
# (test_tmb_upgrade_integration.sh runs its updater via sudo env).
EXPORT_OUTPUT="$(sudo bash "$SRC/scripts/tmb.sh" migration export 2>&1)"
if [[ "$EXPORT_OUTPUT" == *"$TOKEN"* ]]; then
  echo "FAIL: migration export leaked the bot token." >&2
  printf '%s\n' "$EXPORT_OUTPUT" >&2
  exit 1
fi
MIGRATION_ARCHIVE="$(printf '%s\n' "$EXPORT_OUTPUT" | sed -n 's/^Backup created: //p' | head -n 1)"
[[ -f "$SRC/$MIGRATION_ARCHIVE" ]] || {
  echo "migration export did not produce an archive: $EXPORT_OUTPUT" >&2
  exit 1
}
echo "OK migration export produced $MIGRATION_ARCHIVE"

running="$(docker compose --project-directory "$SRC" --profile local-api \
  ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
[[ "$running" == *"bot"* && "$running" == *"worker"* && "$running" == *"redis"* ]] || {
  echo "source services were not restored after export: $running" >&2
  exit 1
}
echo "OK export restored the exact source service state"

# --- Destination 1 (runtime identity 54321:54321, different path) ----------
install_tree "$DST"
write_service_override "$DST"
cat >"$DST/config.yaml" <<'EOF'
telegram:
  bot_token: "CHANGE_ME"
EOF
chmod 600 "$DST/config.yaml"
cat >"$DST/.env" <<EOF
TMB_IMAGE=telegram-media-downloader-bot:ci
COMPOSE_PROJECT_NAME=tmb-uid-dst-$$
COMPOSE_PROFILES=local-api
APP_UID=54321
APP_GID=54321
EOF
chmod 600 "$DST/.env"
mkdir -p "$DST/backups"
sudo cp "$SRC/$MIGRATION_ARCHIVE" "$DST/backups/"
sudo cp "$SRC/$MIGRATION_ARCHIVE.sha256" "$DST/backups/"

export COMPOSE_PROJECT_NAME="tmb-uid-dst-$$"
IMPORT_OUTPUT="$(sudo bash "$DST/scripts/tmb.sh" migration import \
  "backups/$(basename "$MIGRATION_ARCHIVE")" 2>&1)"
IMPORT_STATUS=$?
if [[ "$IMPORT_STATUS" -ne 0 ]]; then
  echo "FAIL: migration import failed (status $IMPORT_STATUS)." >&2
  printf '%s\n' "$IMPORT_OUTPUT" >&2
  exit 1
fi
if [[ "$IMPORT_OUTPUT" != *"Restore completed successfully"* ]]; then
  echo "FAIL: migration import did not complete: $IMPORT_OUTPUT" >&2
  exit 1
fi
if [[ "$IMPORT_OUTPUT" == *"$TOKEN"* ]]; then
  echo "FAIL: migration import leaked the bot token." >&2
  exit 1
fi
echo "OK migration import completed without leaking secrets"

# Restored runtime identity honored: config.yaml owned by the RESTORED
# APP_UID/APP_GID (the source's 10001:10001) and still mode 0600.
OWNER="$(stat -c '%u:%g' "$DST/config.yaml")"
MODE="$(stat -c '%a' "$DST/config.yaml")"
[[ "$OWNER" == "${SRC_UID}:${SRC_GID}" ]] || {
  echo "FAIL: restored config.yaml owner is $OWNER, expected ${SRC_UID}:${SRC_GID}." >&2
  exit 1
}
[[ "$MODE" == "600" ]] || {
  echo "FAIL: restored config.yaml mode is $MODE, expected 600." >&2
  exit 1
}
echo "OK restored config.yaml owner=${OWNER} mode=${MODE} (restored runtime identity honored)"

sudo grep -q '^APP_UID=10001$' "$DST/.env" || {
  echo "FAIL: restored .env does not carry the source runtime identity." >&2
  exit 1
}
echo "OK restored .env carries the source APP_UID/GID"

# SQLite/state, cookies, and Local Bot API durable state restored.
sudo test -f "$DST/data/state/jobs.sqlite3" || {
  echo "FAIL: restored SQLite state missing." >&2
  exit 1
}
sudo grep -q 'fixture' "$DST/data/cookies/cookies.txt" || {
  echo "FAIL: restored cookies missing." >&2
  exit 1
}
COOKIE_MODE="$(stat -c '%a' "$DST/data/cookies/cookies.txt")"
[[ "$COOKIE_MODE" == "600" ]] || {
  echo "FAIL: restored cookies mode is $COOKIE_MODE, expected 600." >&2
  exit 1
}
sudo grep -q '"phase": "local"' "$DST/data/state/telegram-api-migration.json" || {
  echo "FAIL: restored Local Bot API durable state missing." >&2
  exit 1
}
echo "OK SQLite, cookies (0600), and Local Bot API durable state restored"

# Import must never activate services: the destination project stays stopped.
running="$(docker compose --project-directory "$DST" --profile local-api \
  ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
[[ -z "${running//[[:space:]]/}" ]] || {
  echo "FAIL: import left destination services running: $running" >&2
  exit 1
}
echo "OK import left the destination stopped (no simultaneous source/destination polling)"

# --- Cutover: stop the source BEFORE activating the destination -------------- #
# The restored .env carries the source's COMPOSE_PROJECT_NAME, so the cutover
# commands must target the SOURCE project explicitly (the shell env still
# carries the destination project name from the import phase). This is also
# the documented production cutover: never poll from both servers at once.
export COMPOSE_PROJECT_NAME="tmb-uid-src-$$"
for service in bot worker local-api redis; do
  running="$(docker compose --project-directory "$SRC" --profile local-api \
    ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
  [[ "$running" == *"$service"* ]] || continue
  docker compose --project-directory "$SRC" --profile local-api stop "$service" >/dev/null
done
for _attempt in {1..30}; do
  running="$(docker compose --project-directory "$SRC" --profile local-api \
    ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
  [[ -z "${running//[[:space:]]/}" ]] && break
  sleep 2
done
[[ -z "${running//[[:space:]]/}" ]] || {
  echo "FAIL: source services did not stop before destination activation: $running" >&2
  exit 1
}
# Remove the source project's containers so its published ports (e.g. the
# Local Bot API 127.0.0.1:8081 binding) are released before the destination
# activates. The source stays offline for the remainder of the scenario.
docker compose --project-directory "$SRC" --profile local-api down >/dev/null 2>&1 || true
echo "OK source stopped before destination activation (single-poller cutover)"

# --- Destination activation: real local-api + stable bot/worker -------------- #
DST_ACTIVATION_OUTPUT="$(docker compose --project-directory "$DST" --profile local-api \
  up -d --no-build redis local-api bot worker 2>&1)"
if [[ "$DST_ACTIVATION_OUTPUT" == *"$TOKEN"* ]]; then
  echo "FAIL: destination activation leaked the bot token." >&2
  exit 1
fi

local_api_container="$(docker compose --project-directory "$DST" --profile local-api ps -q local-api)"
[[ -n "$local_api_container" ]] || {
  echo "FAIL: destination local-api container was not created." >&2
  exit 1
}
health_state() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$local_api_container" 2>/dev/null || echo "none"
}
for _attempt in {1..60}; do
  state="$(health_state)"
  [[ "$state" == "healthy" ]] && break
  if docker inspect --format '{{.State.Status}}' "$local_api_container" 2>/dev/null |
    grep -Eq '^(exited|dead)$'; then
    echo "FAIL: destination local-api exited before becoming healthy." >&2
    docker compose --project-directory "$DST" --profile local-api logs local-api >&2 || true
    exit 1
  fi
  sleep 4
done
[[ "$(health_state)" == "healthy" ]] || {
  echo "FAIL: destination local-api did not become healthy." >&2
  docker compose --project-directory "$DST" --profile local-api logs local-api >&2 || true
  exit 1
}
for _attempt in {1..30}; do
  running="$(docker compose --project-directory "$DST" --profile local-api \
    ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
  [[ "$running" == *"bot"* && "$running" == *"worker"* && "$running" == *"redis"* ]] && break
  sleep 2
done
[[ "$running" == *"bot"* && "$running" == *"worker"* && "$running" == *"redis"* ]] || {
  echo "FAIL: destination services did not start: $running" >&2
  exit 1
}
sleep 20
for container in bot worker local-api redis; do
  id="$(docker compose --project-directory "$DST" --profile local-api ps -q "$container")"
  restarts="$(docker inspect --format '{{.RestartCount}}' "$id")"
  [[ "$restarts" == "0" ]] || {
    echo "FAIL: destination $container RestartCount is $restarts." >&2
    exit 1
  }
  status="$(docker inspect --format '{{.State.Status}}' "$id")"
  [[ "$status" == "running" ]] || {
    echo "FAIL: destination $container is not running ($status)." >&2
    exit 1
  }
done
echo "OK destination services running with RestartCount=0 (local-api real, bot/worker stable)"

# The REAL offline doctor against the RESTORED config as the RESTORED runtime
# identity: this is the end-to-end proof of the BUG 2 ownership repair.
DOCTOR_OUTPUT="$(docker compose --project-directory "$DST" --profile local-api \
  run --rm --no-deps worker telegram-media-bot doctor --config /app/config.yaml \
  --offline 2>&1)"
if [[ "$DOCTOR_OUTPUT" == *"$TOKEN"* ]]; then
  echo "FAIL: destination doctor leaked the bot token." >&2
  exit 1
fi
printf '%s\n' "$DOCTOR_OUTPUT" | grep -q "OK   local_api_executable" || {
  echo "FAIL: destination offline doctor did not pass local_api checks." >&2
  printf '%s\n' "$DOCTOR_OUTPUT" >&2
  exit 1
}
echo "OK destination offline doctor succeeded as the restored runtime identity"

# App-level local-api status agrees with the healthy container (BUG 6).
LAPI_STATUS="$(docker compose --project-directory "$DST" --profile local-api \
  run --rm --no-deps worker telegram-media-bot local-api --config /app/config.yaml \
  status 2>&1)"
if [[ "$LAPI_STATUS" == *"$TOKEN"* ]]; then
  echo "FAIL: destination local-api status leaked the bot token." >&2
  exit 1
fi
printf '%s\n' "$LAPI_STATUS" | grep -Fxq "enabled: true"
printf '%s\n' "$LAPI_STATUS" | grep -Fxq "endpoint_reachable: true"
printf '%s\n' "$LAPI_STATUS" | grep -Fxq "process_running: true"
echo "OK destination local-api app status agrees with the healthy container"

docker compose --project-directory "$DST" --profile local-api stop >/dev/null 2>&1 || true

# --- Failure after swap: offline doctor fails -> full automatic rollback ----
write_source_config true
EXPORT2_OUTPUT="$(sudo bash "$SRC/scripts/tmb.sh" migration export 2>&1)"
MIGRATION_ARCHIVE2="$(printf '%s\n' "$EXPORT2_OUTPUT" | sed -n 's/^Backup created: //p' | head -n 1)"

install_tree "$DST2"
write_service_override "$DST2"
cat >"$DST2/config.yaml" <<'EOF'
# dst2-original-marker
telegram:
  bot_token: "CHANGE_ME"
EOF
chmod 600 "$DST2/config.yaml"
cat >"$DST2/.env" <<EOF
TMB_IMAGE=telegram-media-downloader-bot:ci
COMPOSE_PROJECT_NAME=tmb-uid-dst2-$$
COMPOSE_PROFILES=local-api
APP_UID=54321
APP_GID=54321
EOF
chmod 600 "$DST2/.env"
mkdir -p "$DST2/backups"
sudo cp "$SRC/$MIGRATION_ARCHIVE2" "$DST2/backups/"
sudo cp "$SRC/$MIGRATION_ARCHIVE2.sha256" "$DST2/backups/"

export COMPOSE_PROJECT_NAME="tmb-uid-dst2-$$"
FAIL_STATUS=0
FAIL_OUTPUT="$(sudo bash "$DST2/scripts/tmb.sh" migration import \
  "backups/$(basename "$MIGRATION_ARCHIVE2")" 2>&1)" || FAIL_STATUS=$?
if [[ "$FAIL_STATUS" -eq 0 ]]; then
  echo "FAIL: import with a post-swap-doctor-failing archive unexpectedly succeeded." >&2
  printf '%s\n' "$FAIL_OUTPUT" >&2
  exit 1
fi
if [[ "$FAIL_OUTPUT" == *"$TOKEN"* ]]; then
  echo "FAIL: failed import leaked the bot token." >&2
  exit 1
fi
grep -q "rolling back" <<<"$FAIL_OUTPUT" || {
  echo "FAIL: failed import did not report rollback: $FAIL_OUTPUT" >&2
  exit 1
}
# The rollback must restore the destination's ORIGINAL config: content, owner,
# and mode.
sudo grep -q "dst2-original-marker" "$DST2/config.yaml" || {
  echo "FAIL: rollback did not restore the destination's original config content." >&2
  exit 1
}
ROLLBACK_OWNER="$(stat -c '%u:%g' "$DST2/config.yaml")"
ROLLBACK_MODE="$(stat -c '%a' "$DST2/config.yaml")"
[[ "$ROLLBACK_OWNER" == "$(id -u):$(id -g)" ]] || {
  echo "FAIL: rollback did not restore the original config owner ($ROLLBACK_OWNER, expected $(id -u):$(id -g))." >&2
  exit 1
}
[[ "$ROLLBACK_MODE" == "600" ]] || {
  echo "FAIL: rollback config mode is $ROLLBACK_MODE, expected 600." >&2
  exit 1
}
LEFTOVERS="$(find "$DST2" -maxdepth 1 -name '.tmb-restore.*' | wc -l)"
[[ "$LEFTOVERS" == "0" ]] || {
  echo "FAIL: rollback left transaction directories behind." >&2
  exit 1
}
running="$(docker compose --project-directory "$DST2" --profile local-api \
  ps --services --filter status=running | tr -d '\r' | tr '\n' ' ')"
[[ -z "${running//[[:space:]]/}" ]] || {
  echo "FAIL: rollback left destination-2 services running: $running" >&2
  exit 1
}
echo "OK post-swap failure rolled back contents, ownership, mode, and service state"

echo "Privileged migration UID/GID integration test passed."