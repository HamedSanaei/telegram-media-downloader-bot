#!/usr/bin/env bash
set -euo pipefail

if [[ "${RUN_PRIVILEGED_UPGRADE_TESTS:-0}" != "1" ]]; then
  echo "Privileged updater integration test skipped."
  exit 0
fi

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RELEASE_VERSION="$(
  sed -n 's/^version = "\([^"]*\)"/\1/p' "$SOURCE_ROOT/pyproject.toml" | head -n 1
)"
PREVIOUS_VERSION="${TMB_TEST_PREVIOUS_VERSION:-1.0.2}"
USE_RELEASE_UPDATER_ASSET="${TMB_USE_RELEASE_UPDATER_ASSET:-0}"
ACTIVE_LOG_WRITER="${TMB_TEST_ACTIVE_LOCAL_API_LOG_WRITER:-0}"
FAILURE_STAGE="${TMB_TEST_UPDATER_FAILURE_STAGE:-none}"
INITIAL_SERVICE_STATE="${TMB_TEST_INITIAL_SERVICE_STATE:-stopped}"
TEST_ROOT="$(mktemp -d)"
INSTALL_ROOT="$TEST_ROOT/installation"
ASSET_ROOT="$TEST_ROOT/releases/download/v${RELEASE_VERSION}"
BIN_ROOT="$TEST_ROOT/bin"
REGISTRY_NAME="tmb-upgrade-registry-$$"
REGISTRY_PORT="${TMB_TEST_REGISTRY_PORT:-5055}"
IMAGE_REPOSITORY="localhost:${REGISTRY_PORT}/telegram-media-downloader-bot"
TEST_COMPOSE_PROJECT_NAME="tmb-upgrade-test-$$"
INSTALL_OWNER_UID="$(id -u)"
INSTALL_OWNER_GID="$(id -g)"
RUNTIME_UID="$INSTALL_OWNER_UID"
RUNTIME_GID="$INSTALL_OWNER_GID"
LOG_WRITER_PID=""
LOG_WRITER_LAUNCHER_PID=""

assert_owner_mode() {
  local path="$1" expected_owner="$2" expected_mode="$3" label="$4" actual
  actual="$(stat -c '%u:%g %a' "$path")"
  if [[ "$actual" != "$expected_owner $expected_mode" ]]; then
    echo "Unexpected $label permissions: $(stat -c '%U:%G %a %n' "$path")" >&2
    return 1
  fi
}

assert_mode() {
  local path="$1" expected_mode="$2" label="$3" actual
  actual="$(stat -c '%a' "$path")"
  if [[ "$actual" != "$expected_mode" ]]; then
    echo "Unexpected $label mode: $(stat -c '%U:%G %a %n' "$path")" >&2
    return 1
  fi
}

assert_update_output() {
  local expected="$1" output_file="$2"
  if ! grep -Fq "$expected" "$output_file"; then
    echo "Missing expected sanitized updater diagnostic: $expected" >&2
    sed '/DO_NOT_LEAK_PRIVILEGED/d' "$output_file" >&2
    return 1
  fi
}

assert_original_service_state() {
  local actual expected
  actual="$(docker compose --project-directory "$INSTALL_ROOT" --profile local-api \
    ps --services --filter status=running | sort)"
  case "$INITIAL_SERVICE_STATE" in
    stopped) expected="" ;;
    redis-only) expected="redis" ;;
    writer-redis) expected="$(printf 'local-api\nredis\n' | sort)" ;;
    *)
      echo "Unknown initial service state: $INITIAL_SERVICE_STATE" >&2
      return 2
      ;;
  esac
  [[ "$actual" == "$expected" ]] || {
    echo "Service state mismatch. Expected '$expected'; received '$actual'." >&2
    return 1
  }
}

cleanup() {
  if [[ -n "$LOG_WRITER_PID" ]]; then
    sudo kill "$LOG_WRITER_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "$LOG_WRITER_LAUNCHER_PID" ]]; then
    wait "$LOG_WRITER_LAUNCHER_PID" >/dev/null 2>&1 || true
  fi
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api down \
    --remove-orphans --volumes >/dev/null 2>&1 || true
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  sudo rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

for command in bash curl docker git python sha256sum tar; do
  command -v "$command" >/dev/null
done

mkdir -p "$INSTALL_ROOT" "$ASSET_ROOT" "$BIN_ROOT"
RELEASE_INDEX="$TEST_ROOT/release.index"
RELEASE_EPOCH="$(git -C "$SOURCE_ROOT" show -s --format=%ct HEAD)"
GIT_INDEX_FILE="$RELEASE_INDEX" git -C "$SOURCE_ROOT" read-tree HEAD
GIT_INDEX_FILE="$RELEASE_INDEX" git -C "$SOURCE_ROOT" add -A
RELEASE_TREE="$(GIT_INDEX_FILE="$RELEASE_INDEX" git -C "$SOURCE_ROOT" write-tree)"
TMB_RELEASE_ARCHIVE_EPOCH="$RELEASE_EPOCH" \
  bash "$SOURCE_ROOT/scripts/build_release_archives.sh" "$RELEASE_TREE" "$ASSET_ROOT"
ARCHIVE_LISTING="$(tar -tvf "$ASSET_ROOT/telegram-media-downloader-bot.tar.gz")"
grep -Eq \
  '^lrwxrwxrwx .* telegram-media-downloader-bot/scripts/tmb.sh -> tmb-current.sh$' \
  <<<"$ARCHIVE_LISTING"
grep -Eq \
  '^-rwxr-xr-x .* telegram-media-downloader-bot/scripts/tmb-current.sh$' \
  <<<"$ARCHIVE_LISTING"
tar -xzf "$ASSET_ROOT/telegram-media-downloader-bot.tar.gz" \
  -C "$INSTALL_ROOT" --strip-components=1

# Model the exact selected previous installation. v1.2.1 cannot replace its running updater before
# preflight, so that case later executes the separately checksummed release updater asset against
# this unchanged old layout. The v1.0.2 case retains legacy self-replacement regression coverage.
sed -i \
  "s/^version = \"${RELEASE_VERSION}\"/version = \"${PREVIOUS_VERSION}\"/" \
  "$INSTALL_ROOT/pyproject.toml"
rm -f "$INSTALL_ROOT/scripts/tmb.sh" "$INSTALL_ROOT/scripts/tmb-current.sh"
git -C "$SOURCE_ROOT" show "v${PREVIOUS_VERSION}:scripts/tmb.sh" \
  >"$INSTALL_ROOT/scripts/tmb.sh"
sed -i \
  "s|^IMAGE_REPOSITORY=.*|IMAGE_REPOSITORY=\"${IMAGE_REPOSITORY}\"|" \
  "$INSTALL_ROOT/scripts/tmb.sh"
chmod 755 "$INSTALL_ROOT/scripts/tmb.sh"
cp "$INSTALL_ROOT/config.example.yaml" "$INSTALL_ROOT/config.yaml"
awk '
  /^gallery_dl:/ { skipping = 1; next }
  skipping && /^[^[:space:]#]/ { skipping = 0 }
  !skipping { print }
' "$INSTALL_ROOT/config.yaml" >"$INSTALL_ROOT/config.yaml.without-gallery"
mv "$INSTALL_ROOT/config.yaml.without-gallery" "$INSTALL_ROOT/config.yaml"
cat >"$INSTALL_ROOT/.env" <<EOF
TMB_IMAGE=${IMAGE_REPOSITORY}:${PREVIOUS_VERSION}
COMPOSE_PROJECT_NAME=${TEST_COMPOSE_PROJECT_NAME}
COMPOSE_PROFILES=local-api
APP_UID=${RUNTIME_UID}
APP_GID=${RUNTIME_GID}
TMB_WORKER_CPUS=1.5
EOF
chmod 755 "$INSTALL_ROOT"
chmod 600 "$INSTALL_ROOT/.env" "$INSTALL_ROOT/config.yaml"
mkdir -p \
  "$INSTALL_ROOT/data/state" \
  "$INSTALL_ROOT/data/cookies" \
  "$INSTALL_ROOT/data/downloads" \
  "$INSTALL_ROOT/data/temp" \
  "$INSTALL_ROOT/data/telegram-bot-api" \
  "$INSTALL_ROOT/backups"
python - "$INSTALL_ROOT/data/state/jobs.sqlite3" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("CREATE TABLE upgrade_sentinel (value TEXT NOT NULL)")
connection.execute("INSERT INTO upgrade_sentinel VALUES ('preserved')")
connection.commit()
connection.close()
PY
printf 'cookie-sentinel' >"$INSTALL_ROOT/data/cookies/cookies.txt"
printf 'download-sentinel' >"$INSTALL_ROOT/data/downloads/existing.bin"
printf 'temp-sentinel' >"$INSTALL_ROOT/data/temp/existing.part"
printf 'local-api-sentinel' >"$INSTALL_ROOT/data/telegram-bot-api/state.bin"
printf 'local-api-log-sentinel' >"$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log"
CONFIG_HASH_BEFORE="$(sha256sum "$INSTALL_ROOT/config.yaml" | cut -d' ' -f1)"
COOKIE_HASH_BEFORE="$(sha256sum "$INSTALL_ROOT/data/cookies/cookies.txt" | cut -d' ' -f1)"

docker run -d --rm \
  --name "$REGISTRY_NAME" \
  -p "127.0.0.1:${REGISTRY_PORT}:5000" \
  registry:2 >/dev/null
for _attempt in {1..20}; do
  curl -fsS "http://127.0.0.1:${REGISTRY_PORT}/v2/" >/dev/null && break
  sleep 1
done
curl -fsS "http://127.0.0.1:${REGISTRY_PORT}/v2/" >/dev/null
docker tag telegram-media-downloader-bot:ci "${IMAGE_REPOSITORY}:${PREVIOUS_VERSION}"
docker tag telegram-media-downloader-bot:ci "${IMAGE_REPOSITORY}:${RELEASE_VERSION}"
docker push "${IMAGE_REPOSITORY}:${PREVIOUS_VERSION}" >/dev/null
docker push "${IMAGE_REPOSITORY}:${RELEASE_VERSION}" >/dev/null

# The exact v1.0.2 updater has a fixed GitHub release URL. Redirect only its two release downloads
# to the locally generated, checksummed assets; Docker, filesystem, ownership, and SQLite stay real.
cat >"$BIN_ROOT/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
output=""
url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o)
      output="$2"
      shift 2
      ;;
    -*)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done
[[ -n "$output" && -n "$url" ]]
cp "$TMB_TEST_ASSET_ROOT/${url##*/}" "$output"
EOF
chmod 755 "$BIN_ROOT/curl"

REAL_DOCKER="$(command -v docker)"
REAL_TAR="$(command -v tar)"
cat >"$BIN_ROOT/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${TMB_TEST_UPDATER_FAILURE_STAGE:-none}" == "doctor" ]] \
  && [[ "$*" == *"telegram-media-bot doctor"* ]]; then
  echo "configured queue connectivity check failed" >&2
  echo "BOT_TOKEN=DO_NOT_LEAK_PRIVILEGED_DOCTOR_SECRET" >&2
  exit 81
fi
real_docker="${TMB_TEST_REAL_DOCKER:-}"
if [[ -z "$real_docker" ]]; then
  for candidate in /usr/bin/docker /usr/local/bin/docker; do
    if [[ -x "$candidate" ]]; then
      real_docker="$candidate"
      break
    fi
  done
fi
[[ -n "$real_docker" ]]
exec "$real_docker" "$@"
EOF
cat >"$BIN_ROOT/tar" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${TMB_TEST_UPDATER_FAILURE_STAGE:-none}" == "backup" ]] \
  && [[ "${1:-}" == "-czf" ]] \
  && [[ "${2:-}" == backups/.tmb-* || "${2:-}" == */backups/.tmb-* ]]; then
  echo "persistent filesystem snapshot failed" >&2
  echo "BOT_TOKEN=DO_NOT_LEAK_PRIVILEGED_BACKUP_SECRET" >&2
  printf 'partial' >"$2"
  exit 82
fi
exec "${TMB_TEST_REAL_TAR:-/usr/bin/tar}" "$@"
EOF
chmod 755 "$BIN_ROOT/docker" "$BIN_ROOT/tar"

case "$INITIAL_SERVICE_STATE" in
  stopped) ;;
  redis-only)
    docker compose --project-directory "$INSTALL_ROOT" --profile local-api up -d redis
    ;;
  writer-redis)
    cat >"$INSTALL_ROOT/docker-compose.override.yml" <<'EOF'
services:
  local-api:
    command:
      - sh
      - -c
      - >-
        exec 3>>/data/telegram-bot-api/telegram-bot-api.log;
        while :; do printf 'active-local-api-service-log\n' >&3; sleep 0.01; done
    healthcheck:
      disable: true
EOF
    docker compose --project-directory "$INSTALL_ROOT" --profile local-api \
      up -d redis local-api
    SERVICE_LOG_SIZE_BEFORE="$(stat -c '%s' \
      "$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log")"
    sleep 0.1
    test "$(stat -c '%s' "$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log")" \
      -gt "$SERVICE_LOG_SIZE_BEFORE"
    ;;
  *)
    echo "Unknown initial service state: $INITIAL_SERVICE_STATE" >&2
    exit 2
    ;;
esac
assert_original_service_state

# Reproduce unusable production ownership/modes. The release tar deliberately carries a symlinked
# executable updater so v1.0.2 cannot truncate its own executing inode during `cp -a`.
sudo chown -R 0:0 "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups"
sudo find "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups" -type d -exec chmod 500 {} +
sudo find "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups" -type f -exec chmod 400 {} +
if [[ "$USE_RELEASE_UPDATER_ASSET" == "1" ]]; then
  sudo chown "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" \
    "$INSTALL_ROOT/data" \
    "$INSTALL_ROOT/data/cookies" \
    "$INSTALL_ROOT/data/cookies/cookies.txt"
fi
if [[ "$INITIAL_SERVICE_STATE" == "writer-redis" ]]; then
  sudo chown -R "${RUNTIME_UID}:${RUNTIME_GID}" \
    "$INSTALL_ROOT/data/telegram-bot-api"
  sudo chmod 700 "$INSTALL_ROOT/data/telegram-bot-api"
  sudo find "$INSTALL_ROOT/data/telegram-bot-api" -type f -exec chmod 600 {} +
fi

assert_owner_mode "$TEST_ROOT" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 700 \
  "temporary parent"
assert_owner_mode "$INSTALL_ROOT" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 755 \
  "installation root"
assert_owner_mode "$INSTALL_ROOT/.env" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 600 \
  "deployment environment"
assert_owner_mode "$INSTALL_ROOT/config.yaml" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 600 \
  "application config"
[[ -x "$TEST_ROOT" && -x "$INSTALL_ROOT" && -r "$INSTALL_ROOT/.env" ]]
if [[ "$USE_RELEASE_UPDATER_ASSET" == "1" ]]; then
  assert_owner_mode "$INSTALL_ROOT/data" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 500 \
    "preflight data root"
  assert_owner_mode "$INSTALL_ROOT/data/cookies" \
    "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 500 "preflight cookie directory"
  assert_owner_mode "$INSTALL_ROOT/data/cookies/cookies.txt" \
    "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 400 "preflight cookie"
else
  assert_owner_mode "$INSTALL_ROOT/data" "0:0" 500 "legacy root-owned data"
fi

UPDATER_PATH="$INSTALL_ROOT/scripts/tmb.sh"
if [[ "$USE_RELEASE_UPDATER_ASSET" == "1" ]]; then
  (
    cd "$ASSET_ROOT"
    sha256sum --check --status tmb-updater.sh.sha256
  )
  UPDATER_PATH="$TEST_ROOT/tmb-updater.sh"
  cp "$ASSET_ROOT/tmb-updater.sh" "$UPDATER_PATH"
  chmod 755 "$UPDATER_PATH"
fi

if [[ "$ACTIVE_LOG_WRITER" == "1" && "$INITIAL_SERVICE_STATE" != "writer-redis" ]]; then
  sudo env \
    "TMB_TEST_LOG_PATH=$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log" \
    "TMB_TEST_WRITER_PID_PATH=$TEST_ROOT/log-writer.pid" \
    bash -c '
      echo $$ >"$TMB_TEST_WRITER_PID_PATH"
      trap "exit 0" TERM INT
      while :; do
        printf "active-local-api-log-line-%s\n" "$(date +%s%N)" >>"$TMB_TEST_LOG_PATH"
        sleep 0.01
      done
    ' &
  LOG_WRITER_LAUNCHER_PID=$!
  for _attempt in {1..50}; do
    [[ -s "$TEST_ROOT/log-writer.pid" ]] && break
    sleep 0.02
  done
  LOG_WRITER_PID="$(cat "$TEST_ROOT/log-writer.pid")"
  LOG_SIZE_BEFORE="$(sudo stat -c '%s' "$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log")"
  sleep 0.1
  test "$(sudo stat -c '%s' "$INSTALL_ROOT/data/telegram-bot-api/telegram-bot-api.log")" \
    -gt "$LOG_SIZE_BEFORE"
fi

UPDATE_OUTPUT="$TEST_ROOT/update-output.log"
if sudo env \
  "PATH=$BIN_ROOT:$PATH" \
  "TMB_BIN_DIR=$BIN_ROOT" \
  "TMB_RELEASE_TAG=v${RELEASE_VERSION}" \
  "TMB_IMAGE_REPOSITORY=$IMAGE_REPOSITORY" \
  "TMB_TEST_ASSET_ROOT=$ASSET_ROOT" \
  "TMB_TEST_REAL_DOCKER=$REAL_DOCKER" \
  "TMB_TEST_REAL_TAR=$REAL_TAR" \
  "TMB_TEST_UPDATER_FAILURE_STAGE=$FAILURE_STAGE" \
  "TMB_TEST_UPDATER_PATH=$UPDATER_PATH" \
  "TMB_TEST_UPDATE_OUTPUT=$UPDATE_OUTPUT" \
  "TMB_ROOT_DIR=$INSTALL_ROOT" \
  bash -c 'bash "$TMB_TEST_UPDATER_PATH" update >"$TMB_TEST_UPDATE_OUTPUT" 2>&1'; then
  UPDATE_STATUS=0
else
  UPDATE_STATUS=$?
fi

if [[ "$FAILURE_STAGE" != "none" ]]; then
  [[ "$UPDATE_STATUS" -ne 0 ]]
  grep -q "^version = \"${PREVIOUS_VERSION}\"$" "$INSTALL_ROOT/pyproject.toml"
  grep -q "^TMB_IMAGE=${IMAGE_REPOSITORY}:${PREVIOUS_VERSION}$" "$INSTALL_ROOT/.env"
  test "$(sudo sha256sum "$INSTALL_ROOT/config.yaml" | cut -d' ' -f1)" = "$CONFIG_HASH_BEFORE"
  test "$(sudo sha256sum "$INSTALL_ROOT/data/cookies/cookies.txt" | cut -d' ' -f1)" = \
    "$COOKIE_HASH_BEFORE"
  if grep -q 'DO_NOT_LEAK_PRIVILEGED' "$UPDATE_OUTPUT"; then
    echo "Privileged updater diagnostics leaked a secret." >&2
    exit 1
  fi
  case "$FAILURE_STAGE" in
    backup)
      assert_update_output 'Update stage failed: consistent persistent-state backup' "$UPDATE_OUTPUT"
      assert_update_output 'persistent filesystem snapshot failed' "$UPDATE_OUTPUT"
      ;;
    doctor)
      assert_update_output 'Update stage failed: offline runtime doctor' "$UPDATE_OUTPUT"
      assert_update_output 'configured queue connectivity check failed' "$UPDATE_OUTPUT"
      ;;
    *)
      echo "Unknown privileged failure stage: $FAILURE_STAGE" >&2
      exit 2
      ;;
  esac
  if ! assert_original_service_state; then
    sed '/DO_NOT_LEAK_PRIVILEGED/d' "$UPDATE_OUTPUT" >&2
    docker compose --project-directory "$INSTALL_ROOT" --profile local-api ps -a >&2
    docker compose --project-directory "$INSTALL_ROOT" --profile local-api logs local-api >&2 \
      || true
    exit 1
  fi
  if sudo find "$INSTALL_ROOT/backups" -maxdepth 1 -type f -name '.tmb-*' | grep -q .; then
    echo "Failure path left a partial backup archive." >&2
    exit 1
  fi
  echo "Privileged updater $FAILURE_STAGE rollback test passed for previous ${PREVIOUS_VERSION}."
  exit 0
fi

[[ "$UPDATE_STATUS" -eq 0 ]]
cat "$UPDATE_OUTPUT"
assert_original_service_state

if [[ "$ACTIVE_LOG_WRITER" == "1" && "$INITIAL_SERVICE_STATE" != "writer-redis" ]]; then
  sudo kill "$LOG_WRITER_PID"
  wait "$LOG_WRITER_LAUNCHER_PID" || true
  LOG_WRITER_PID=""
  LOG_WRITER_LAUNCHER_PID=""
fi

assert_mode "$INSTALL_ROOT" 755 "post-update application root"
assert_owner_mode "$INSTALL_ROOT/.env" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 600 \
  "post-update deployment environment"
assert_owner_mode "$INSTALL_ROOT/config.yaml" "${INSTALL_OWNER_UID}:${INSTALL_OWNER_GID}" 600 \
  "post-update application config"
assert_owner_mode "$INSTALL_ROOT/data" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "runtime data root"
assert_owner_mode "$INSTALL_ROOT/data/state" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "runtime state directory"
assert_owner_mode "$INSTALL_ROOT/data/state/jobs.sqlite3" \
  "${RUNTIME_UID}:${RUNTIME_GID}" 600 "runtime SQLite database"
assert_owner_mode "$INSTALL_ROOT/data/downloads" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "runtime downloads directory"
assert_owner_mode "$INSTALL_ROOT/data/downloads/existing.bin" \
  "${RUNTIME_UID}:${RUNTIME_GID}" 600 "existing runtime download"
assert_owner_mode "$INSTALL_ROOT/data/temp" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "runtime temporary directory"
assert_owner_mode "$INSTALL_ROOT/data/cookies" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "restricted cookie directory"
assert_owner_mode "$INSTALL_ROOT/data/cookies/cookies.txt" \
  "${RUNTIME_UID}:${RUNTIME_GID}" 600 "restricted cookie file"
assert_owner_mode "$INSTALL_ROOT/data/telegram-bot-api" \
  "${RUNTIME_UID}:${RUNTIME_GID}" 700 "Local Bot API state directory"
assert_owner_mode "$INSTALL_ROOT/data/telegram-bot-api/state.bin" \
  "${RUNTIME_UID}:${RUNTIME_GID}" 600 "existing Local Bot API state"
assert_owner_mode "$INSTALL_ROOT/backups" "${RUNTIME_UID}:${RUNTIME_GID}" 700 \
  "runtime backup directory"
[[ -x "$TEST_ROOT" && -x "$INSTALL_ROOT" && -r "$INSTALL_ROOT/.env" ]]
PATH="$BIN_ROOT:$PATH" command -v tmb >/dev/null
test -x "$(readlink -f "$(PATH="$BIN_ROOT:$PATH" command -v tmb)")"
bash -n "$INSTALL_ROOT/scripts/tmb.sh"
sudo env "PATH=$BIN_ROOT:$PATH" "TMB_ROOT_DIR=$INSTALL_ROOT" tmb status >/dev/null
sudo grep -q "^TMB_IMAGE=.*:${RELEASE_VERSION}$" "$INSTALL_ROOT/.env"
grep -q "^APP_UID=${RUNTIME_UID}$" "$INSTALL_ROOT/.env"
grep -q "^APP_GID=${RUNTIME_GID}$" "$INSTALL_ROOT/.env"
sudo grep -q 'CHANGE_ME' "$INSTALL_ROOT/config.yaml"
if sudo grep -q '^gallery_dl:' "$INSTALL_ROOT/config.yaml"; then
  echo "Update unexpectedly injected a gallery_dl configuration section." >&2
  exit 1
fi
sudo grep -q '^cookie-sentinel$' "$INSTALL_ROOT/data/cookies/cookies.txt"
sudo grep -q '^download-sentinel$' "$INSTALL_ROOT/data/downloads/existing.bin"
sudo grep -q '^temp-sentinel$' "$INSTALL_ROOT/data/temp/existing.part"
sudo grep -q '^local-api-sentinel$' "$INSTALL_ROOT/data/telegram-bot-api/state.bin"
test "$(sudo sha256sum "$INSTALL_ROOT/config.yaml" | cut -d' ' -f1)" = "$CONFIG_HASH_BEFORE"
test "$(sudo sha256sum "$INSTALL_ROOT/data/cookies/cookies.txt" | cut -d' ' -f1)" = \
  "$COOKIE_HASH_BEFORE"
grep -q "^version = \"${RELEASE_VERSION}\"$" "$INSTALL_ROOT/pyproject.toml"

if [[ "$ACTIVE_LOG_WRITER" == "1" ]]; then
  BACKUP_ARCHIVE="$(sudo find "$INSTALL_ROOT/backups" -maxdepth 1 -type f \
    -name 'tmb-*.tar.gz' -print | sort | tail -n 1)"
  [[ -n "$BACKUP_ARCHIVE" ]]
  assert_mode "$BACKUP_ARCHIVE" 600 "atomic update backup"
  BACKUP_LISTING="$(sudo tar -tzf "$BACKUP_ARCHIVE")"
  for archived_path in \
    config.yaml \
    .env \
    data/state/jobs.sqlite3 \
    data/cookies/cookies.txt \
    data/telegram-bot-api/state.bin; do
    grep -Fxq "$archived_path" <<<"$BACKUP_LISTING"
  done
  for excluded_path in \
    data/telegram-bot-api/telegram-bot-api.log \
    data/downloads/existing.bin \
    data/temp/existing.part; do
    if grep -Fxq "$excluded_path" <<<"$BACKUP_LISTING"; then
      echo "Backup unexpectedly archived $excluded_path." >&2
      exit 1
    fi
  done
  if sudo find "$INSTALL_ROOT/backups" -maxdepth 1 -type f -name '.tmb-*' | grep -q .; then
    echo "Update left a partial backup archive." >&2
    exit 1
  fi
fi

COMPOSE_CONTRACT="$TEST_ROOT/compose-contract.json"
docker compose --project-directory "$INSTALL_ROOT" --profile local-api \
  config --format json >"$COMPOSE_CONTRACT"
python - "$COMPOSE_CONTRACT" "$TEST_COMPOSE_PROJECT_NAME" \
  "${RUNTIME_UID}:${RUNTIME_GID}" "$INSTALL_ROOT/config.yaml" "$INSTALL_ROOT/data" <<'PY'
import json
from pathlib import Path
import sys

contract_path, expected_project, expected_user, config_path, data_path = sys.argv[1:]
contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
assert contract["name"] == expected_project
for service_name in ("bot", "worker", "local-api"):
    service = contract["services"][service_name]
    assert service["user"] == expected_user
    volumes = {volume["target"]: volume for volume in service["volumes"]}
    assert volumes["/app/config.yaml"]["source"] == config_path
    assert volumes["/app/config.yaml"]["read_only"] is True
    assert volumes["/data"]["source"] == data_path
    assert not volumes["/data"].get("read_only", False)
PY

DOCTOR_OUTPUT="$(
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api run --rm --no-deps \
    worker telegram-media-bot doctor --config /app/config.yaml
)"
grep -Eq '^OK +gallery_dl_cookie_instagram$' <<<"$DOCTOR_OUTPUT"

docker run --rm --user "${RUNTIME_UID}:${RUNTIME_GID}" --entrypoint python \
  -v "$INSTALL_ROOT/data:/data" \
  "${IMAGE_REPOSITORY}:${RELEASE_VERSION}" -c '
import pathlib
import sqlite3

probe = pathlib.Path("/data/state/.integration-write-probe")
probe.write_text("ok", encoding="utf-8")
probe.unlink()
connection = sqlite3.connect("/data/state/jobs.sqlite3")
assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0].casefold() == "wal"
assert connection.execute("SELECT value FROM upgrade_sentinel").fetchone() == ("preserved",)
connection.close()
'

echo "Privileged filesystem/SQLite updater integration test passed for previous ${PREVIOUS_VERSION}."
