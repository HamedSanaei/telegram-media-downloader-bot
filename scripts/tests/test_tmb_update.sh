#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap '[[ "${TMB_KEEP_TEST_ROOT:-0}" == "1" ]] || rm -rf -- "$TEST_ROOT"' EXIT

fail() {
  echo "tmb update test failed: $1" >&2
  exit 1
}

set_running_services() {
  local case_root="$1"
  shift
  printf '%s\n' "$@" | sed '/^$/d' >"$case_root/running-services"
}

prepare_case() {
  local case_root="$1"
  mkdir -p \
    "$case_root/scripts" \
    "$case_root/fake-bin" \
    "$case_root/data/state" \
    "$case_root/data/cookies" \
    "$case_root/data/telegram-bot-api" \
    "$case_root/data/downloads" \
    "$case_root/data/temp"
  cp "$SOURCE_ROOT/scripts/tmb.sh" "$case_root/scripts/tmb.sh"
  cat >"$case_root/config.yaml" <<'EOF'
telegram:
  bot_token: V1_CONFIG_SENTINEL
  admin_ids:
    - 111111111
    - 222222222
  required_channels:
    enabled: true
    channels:
      - chat_id: -1000000000001
        title: Fixture Channel One
        join_url: https://t.me/fixture_channel_one
      - chat_id: -1000000000002
        title: Fixture Channel Two
        join_url: https://t.me/fixture_channel_two
      - chat_id: -1000000000003
        title: Fixture Channel Three
        join_url: https://t.me/fixture_channel_three
yt_dlp:
  cookies_file: /data/cookies/cookies.txt
EOF
  printf 'TMB_IMAGE=example.invalid/tmb:1.0.2\nCOMPOSE_PROFILES=local-api\nAPP_UID=10001\nAPP_GID=10001\nTMB_WORKER_CPUS=1.5\n' \
    >"$case_root/.env"
  chmod 600 "$case_root/.env"
  printf 'version = "1.0.2"\n' >"$case_root/pyproject.toml"
  printf 'sqlite-v1-state' >"$case_root/data/state/jobs.sqlite3"
  printf 'cookies-v1-state' >"$case_root/data/cookies/cookies.txt"
  printf 'local-api-v1-state' >"$case_root/data/telegram-bot-api/state.bin"
  printf 'runtime-media-v1' >"$case_root/data/downloads/large.mp4"
  printf 'volatile-local-api-log' >"$case_root/data/telegram-bot-api/telegram-bot-api.log"
  set_running_services "$case_root" bot worker local-api redis

  cat >"$case_root/fake-bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$TMB_TEST_LOG"
if [[ "$*" == *"telegram-media-bot config-check"* ]]; then
  [[ "$*" == *"run --rm --read-only --user 10001:10001"* ]] || exit 91
  [[ "$*" == *"--tmpfs /tmp:rw,noexec,nosuid,size=16m,mode=1777"* ]] || exit 92
  [[ "$*" == *"-v $TMB_CASE_ROOT/config.yaml:/app/config.yaml:ro"* ]] || exit 93
  [[ "$*" == *"-v $TMB_CASE_ROOT/data:/data:ro"* ]] || exit 94
  [[ "$*" == *"--read-only-runtime"* ]] || exit 95
  [[ "${TMB_PREFLIGHT_COOKIE_UNREADABLE:-0}" != "1" ]] || exit 96
  [[ -r "$TMB_CASE_ROOT/data/cookies/cookies.txt" ]] || exit 97
fi
if [[ "$*" == *"telegram-media-bot doctor"* ]] \
  && [[ "$*" == *"--read-only-runtime"* ]]; then
  [[ "$*" == *"run --rm --read-only --user 10001:10001"* ]] || exit 89
  [[ "$*" == *"--tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777"* ]] || exit 90
  [[ "$*" == *"--offline"* ]] || exit 98
  [[ "$*" == *"--expected-version 1.0.3"* ]] || exit 99
  if [[ "${TMB_INTERRUPT_PREFLIGHT:-0}" == "1" ]]; then
    printf 'Traceback (most recent call last):\nKeyboardInterrupt\n' >&2
    kill -INT "$PPID"
    sleep 0.05
    exit 130
  fi
fi
if [[ "$*" == *"run --rm --no-deps worker telegram-media-bot doctor"* ]] \
  && [[ "$*" == *"--offline"* ]]; then
  if grep -Eq '^(bot|worker|local-api)$' "$TMB_CASE_ROOT/running-services"; then
    printf 'offline verification observed a running writer\n' >&2
    exit 88
  fi
fi
if [[ "$*" == *"run --rm --no-deps worker telegram-media-bot doctor"* ]] \
  && [[ "$*" == *"--offline"* ]] \
  && [[ "${TMB_FAIL_OFFLINE_DOCTOR:-0}" == "1" ]] \
  && [[ ! -e "$TMB_CASE_ROOT/offline-doctor-failed-once" ]]; then
  touch "$TMB_CASE_ROOT/offline-doctor-failed-once"
  printf 'database schema compatibility check failed\n' >&2
  printf 'proxy connection %s%s%s%s@example.invalid failed\n' \
    'https://' operator :DO_NOT_LEAK_PROXY_CREDENTIAL >&2
  printf 'BOT_TOKEN=DO_NOT_LEAK_DIAGNOSTIC_SECRET\n' >&2
  exit 98
fi
if [[ "$*" == *"run --rm --no-deps worker telegram-media-bot doctor"* ]] \
  && [[ "$*" == *"--online-service"* ]]; then
  if [[ "$*" == *"--online-service bot"* ]] \
    && ! grep -Fxq bot "$TMB_CASE_ROOT/running-services"; then
    printf 'online bot verification ran before bot restore\n' >&2
    exit 87
  fi
  if [[ "$*" == *"--online-service local-api"* ]] \
    && ! grep -Fxq local-api "$TMB_CASE_ROOT/running-services"; then
    printf 'online Local API verification ran before Local API restore\n' >&2
    exit 86
  fi
  if [[ "${TMB_FAIL_ONLINE_DOCTOR:-0}" == "1" ]] \
    && [[ ! -e "$TMB_CASE_ROOT/online-doctor-failed-once" ]]; then
    touch "$TMB_CASE_ROOT/online-doctor-failed-once"
    printf 'restored Telegram endpoint did not become reachable\n' >&2
    printf 'BOT_TOKEN=DO_NOT_LEAK_ONLINE_DIAGNOSTIC_SECRET\n' >&2
    exit 85
  fi
fi
if [[ "$*" == *" ps --services --filter status=running"* ]]; then
  cat "$TMB_CASE_ROOT/running-services"
  exit 0
fi
if [[ "$*" == *" ps -a -q"* ]]; then
  printf 'stopped-project-container\n'
fi
if [[ "$*" == *"run --rm --no-deps worker python -c"* ]]; then
  printf '1.0.3\n'
fi
if [[ "$*" == *"operations.update.prune_old_project_images_after_success"* ]]; then
  printf 'true\n'
fi
if [[ "$*" == image\ inspect\ --format\ \{\{.Id\}\}\ * ]]; then
  printf 'sha256:current\n'
fi
if [[ "$*" == "image inspect --format {{.Size}} sha256:old-unused" ]]; then
  printf '12345\n'
fi
if [[ "$*" == "image ls --no-trunc --format {{.Repository}}|{{.ID}}" ]]; then
  printf '%s\n' \
    'ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:current' \
    'ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:old-unused' \
    'ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:old-used' \
    'ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:shared-tag' \
    'example/another-project|sha256:shared-tag' \
    'redis|sha256:redis'
fi
if [[ "$*" == "ps -aq" ]]; then
  printf 'referenced-container\n'
fi
if [[ "$*" == "inspect --format {{.Image}} referenced-container" ]]; then
  printf 'sha256:old-used\n'
  exit 0
fi
if [[ "$*" == "inspect --format {{.State.Status}} stopped-project-container" ]]; then
  printf 'exited\n'
  exit 0
fi
if [[ "$*" == "inspect --format {{.Image}} stopped-project-container" ]]; then
  printf 'sha256:old-unused\n'
  exit 0
fi
case "$*" in
  *" ps -q bot") printf 'bot-container\n' ;;
  *" ps -q worker") printf 'worker-container\n' ;;
  *" ps -q local-api") printf 'local-api-container\n' ;;
  *" ps -q redis") printf 'redis-container\n' ;;
esac
if [[ " $* " == *" stop "* ]]; then
  temporary="$TMB_CASE_ROOT/running-services.next"
  cp "$TMB_CASE_ROOT/running-services" "$temporary"
  after_stop=false
  skip_timeout=false
  for argument in "$@"; do
    if [[ "$after_stop" == "false" ]]; then
      [[ "$argument" == "stop" ]] && after_stop=true
      continue
    fi
    if [[ "$argument" == "-t" ]]; then
      skip_timeout=true
      continue
    fi
    if [[ "$skip_timeout" == "true" ]]; then
      skip_timeout=false
      continue
    fi
    case "$argument" in
      bot|worker|local-api|redis)
        sed -i "/^${argument}$/d" "$temporary"
        ;;
    esac
  done
  mv "$temporary" "$TMB_CASE_ROOT/running-services"
fi
if [[ " $* " == *" up "* ]]; then
  after_up=false
  for argument in "$@"; do
    if [[ "$after_up" == "false" ]]; then
      [[ "$argument" == "up" ]] && after_up=true
      continue
    fi
    case "$argument" in
      bot|worker|local-api|redis)
        grep -Fxq "$argument" "$TMB_CASE_ROOT/running-services" \
          || printf '%s\n' "$argument" >>"$TMB_CASE_ROOT/running-services"
        ;;
    esac
  done
fi
if [[ "$*" == *"run --rm --user 0 --entrypoint sh"* ]] \
  && [[ "${TMB_FAIL_PERMISSIONS:-0}" == "1" ]] \
  && [[ ! -e "$TMB_CASE_ROOT/permission-failed-once" ]]; then
  touch "$TMB_CASE_ROOT/permission-failed-once"
  exit 1
fi
if [[ "$*" == inspect* ]]; then
  if [[ "${TMB_FAIL_HEALTH:-0}" == "1" ]] \
    && [[ ! -e "$TMB_CASE_ROOT/health-failed-once" ]]; then
    touch "$TMB_CASE_ROOT/health-failed-once"
    printf 'restarting\n'
  elif [[ "$*" == *".State.Status"* ]]; then
    printf 'running\n'
  else
    printf 'healthy\n'
  fi
fi
EOF
  cat >"$case_root/fake-bin/curl" <<'EOF'
#!/usr/bin/env bash
output=""
if [[ "${TMB_FAIL_DOWNLOAD:-0}" == "1" ]]; then
  printf 'curl failed\n' >>"$TMB_TEST_LOG"
  exit 22
fi
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-o" ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
printf 'curl %s\n' "$output" >>"$TMB_TEST_LOG"
mkdir -p "$(dirname "$output")"
printf 'fixture' >"$output"
EOF
  cat >"$case_root/fake-bin/sha256sum" <<'EOF'
#!/usr/bin/env bash
printf 'checksum\n' >>"$TMB_TEST_LOG"
[[ "${TMB_FAIL_CHECKSUM:-0}" != "1" ]]
EOF
  cat >"$case_root/fake-bin/tar" <<'EOF'
#!/usr/bin/env bash
printf 'tar %s\n' "$*" >>"$TMB_TEST_LOG"
if [[ "${TMB_USE_REAL_TAR:-0}" == "1" ]]; then
  exec /usr/bin/tar "$@"
fi
if [[ "$1" == "-czf" ]]; then
  if [[ "${TMB_FAIL_BACKUP:-0}" == "1" ]]; then
    printf 'filesystem snapshot read failed\n' >&2
    printf 'BOT_TOKEN=DO_NOT_LEAK_BACKUP_SECRET\n' >&2
    printf 'partial-backup' >"$2"
    exit 73
  fi
  mkdir -p "$(dirname "$2")"
  printf 'backup' >"$2"
  exit 0
fi
destination=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-C" ]]; then
    destination="$2"
    break
  fi
  shift
done
[[ -n "$destination" ]]
printf 'version = "1.0.3"\n' >"$destination/pyproject.toml"
printf 'services: {}\n' >"$destination/docker-compose.yml"
mkdir -p "$destination/data/state" "$destination/data/cookies" "$destination/data/downloads"
mkdir -p "$destination/scripts/tests"
cp "$TMB_SOURCE_ROOT/install.sh" "$destination/install.sh"
cp "$TMB_SOURCE_ROOT/manage.sh" "$destination/manage.sh"
cp "$TMB_SOURCE_ROOT/scripts/tmb.sh" "$destination/scripts/tmb.sh"
cp "$TMB_SOURCE_ROOT/scripts/build_release_archives.sh" \
  "$destination/scripts/build_release_archives.sh"
cp "$TMB_SOURCE_ROOT/scripts/tests/test_tmb_update.sh" \
  "$destination/scripts/tests/test_tmb_update.sh"
cp "$TMB_SOURCE_ROOT/scripts/tests/test_tmb_upgrade_integration.sh" \
  "$destination/scripts/tests/test_tmb_upgrade_integration.sh"
chmod 644 \
  "$destination/install.sh" \
  "$destination/manage.sh" \
  "$destination/scripts/tmb.sh" \
  "$destination/scripts/build_release_archives.sh" \
  "$destination/scripts/tests/test_tmb_update.sh" \
  "$destination/scripts/tests/test_tmb_upgrade_integration.sh"
printf 'release-placeholder' >"$destination/data/state/.gitkeep"
printf 'release-placeholder' >"$destination/data/cookies/README.md"
printf 'release-placeholder' >"$destination/data/downloads/.gitkeep"
EOF
  cat >"$case_root/fake-bin/ln" <<'EOF'
#!/usr/bin/env bash
target="${@: -2:1}"
link="${@: -1}"
mkdir -p "$(dirname "$link")"
cp "$target" "$link"
chmod +x "$link"
EOF
  cat >"$case_root/fake-bin/readlink" <<'EOF'
#!/usr/bin/env bash
candidate="${@: -1}"
if [[ "$candidate" == "$TMB_CASE_ROOT/bin/tmb" ]]; then
  printf '%s\n' "$TMB_CASE_ROOT/scripts/tmb.sh"
else
  /usr/bin/readlink "$@"
fi
EOF
  chmod +x "$case_root/fake-bin/"*
}

run_success_case() {
  local case_root="$TEST_ROOT/success"
  local log="$case_root/operations.log"
  local env_owner_before env_mode_before
  prepare_case "$case_root"
  env_owner_before="$(stat -c '%u:%g' "$case_root/.env")"
  env_mode_before="$(stat -c '%a' "$case_root/.env")"
  cp "$case_root/config.yaml" "$TEST_ROOT/success-config.expected"
  cp "$case_root/data/cookies/cookies.txt" "$TEST_ROOT/success-cookie.expected"
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  )

  grep -q '^TMB_IMAGE=ghcr.io/hamedsanaei/telegram-media-downloader-bot:1.0.3$' \
    "$case_root/.env" || fail "successful update did not pin the verified version"
  diff -u <(
    printf 'TMB_IMAGE=ghcr.io/hamedsanaei/telegram-media-downloader-bot:1.0.3\nCOMPOSE_PROFILES=local-api\nAPP_UID=10001\nAPP_GID=10001\nTMB_WORKER_CPUS=1.5\n'
  ) "$case_root/.env" || fail "update changed .env beyond TMB_IMAGE"
  [[ "$(stat -c '%u:%g' "$case_root/.env")" == "$env_owner_before" ]] \
    || fail "update changed .env ownership"
  [[ "$(stat -c '%a' "$case_root/.env")" == "$env_mode_before" ]] \
    || fail "update changed .env mode"
  grep -q 'V1_CONFIG_SENTINEL' "$case_root/config.yaml" \
    || fail "successful update overwrote config.yaml"
  cmp "$TEST_ROOT/success-config.expected" "$case_root/config.yaml" \
    || fail "successful update changed config.yaml bytes"
  cmp "$TEST_ROOT/success-cookie.expected" "$case_root/data/cookies/cookies.txt" \
    || fail "successful update changed cookie bytes"
  if grep -q '^gallery_dl:' "$case_root/config.yaml"; then
    fail "update required a temporary gallery_dl override"
  fi
  [[ "$(grep -c 'Fixture Channel' "$case_root/config.yaml")" == "3" ]] \
    || fail "successful update changed required channels"
  [[ "$(grep -cE '^    - (111111111|222222222)$' "$case_root/config.yaml")" == "2" ]] \
    || fail "successful update changed administrator IDs"
  grep -q '^sqlite-v1-state$' "$case_root/data/state/jobs.sqlite3" \
    || fail "successful update overwrote SQLite state"
  grep -q '^cookies-v1-state$' "$case_root/data/cookies/cookies.txt" \
    || fail "successful update overwrote cookies"
  grep -q '^local-api-v1-state$' "$case_root/data/telegram-bot-api/state.bin" \
    || fail "successful update overwrote Local Bot API state"
  grep -q '^runtime-media-v1$' "$case_root/data/downloads/large.mp4" \
    || fail "successful update overwrote existing downloads"
  grep -q 'docker .* stop -t 45 bot worker local-api' "$log" \
    || fail "application writers were not stopped before backup"
  if grep -q 'stop .*redis' "$log"; then
    fail "Redis was stopped before the state backup"
  fi
  grep -q 'tar .*data/state.*data/cookies.*data/telegram-bot-api' "$log" \
    || fail "durable state was not included in backup"
  if grep -q 'tar .*data/downloads' "$log"; then
    fail "large runtime downloads were copied into the backup"
  fi
  local stop_line backup_line download_line permission_line start_line pull_line
  stop_line="$(grep -n 'docker .* stop -t 45 bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  backup_line="$(grep -n '^tar -czf' "$log" | head -n 1 | cut -d: -f1)"
  download_line="$(grep -n '^curl ' "$log" | head -n 1 | cut -d: -f1)"
  pull_line="$(grep -n 'docker compose .* --profile local-api pull' "$log" | head -n 1 | cut -d: -f1)"
  ((download_line < pull_line && pull_line < stop_line && stop_line < backup_line)) \
    || fail "release validation, backup, and service-stop ordering is wrong"
  grep -q -- "--exclude=data/telegram-bot-api/telegram-bot-api.log" "$log" \
    || fail "backup did not exclude only the audited Local Bot API log"
  grep -q 'docker run --rm --read-only --user 10001:10001 .*config.yaml:/app/config.yaml:ro .*data:/data:ro ghcr.io/hamedsanaei/telegram-media-downloader-bot:1.0.3 telegram-media-bot config-check --config /app/config.yaml --read-only-runtime' "$log" \
    || fail "prepared-release config check did not use the read-only runtime data contract"
  local preflight_line candidate_doctor_line
  preflight_line="$(grep -n 'config-check --config /app/config.yaml --read-only-runtime' "$log" | head -n 1 | cut -d: -f1)"
  candidate_doctor_line="$(grep -n 'doctor --config /app/config.yaml --offline --expected-version 1.0.3 --read-only-runtime' "$log" | head -n 1 | cut -d: -f1)"
  ((preflight_line < stop_line)) \
    || fail "prepared-release config check ran after service stop"
  ((preflight_line < candidate_doctor_line && candidate_doctor_line < stop_line)) \
    || fail "candidate offline prerequisites did not finish before service stop"
  grep -q 'docker .* pull' "$log" || fail "updated images were not pulled"
  grep -q 'docker run --rm --user 0 --entrypoint sh' "$log" \
    || fail "runtime permissions were not normalized through the release image"
  permission_line="$(grep -n 'docker run --rm --user 0 --entrypoint sh' "$log" | head -n 1 | cut -d: -f1)"
  start_line="$(grep -n 'docker .* up -d --no-build --force-recreate bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  ((permission_line < start_line)) \
    || fail "runtime permission migration did not finish before service start"
  local offline_doctor_line online_doctor_line
  offline_doctor_line="$(grep -n 'run --rm --no-deps worker telegram-media-bot doctor --config /app/config.yaml --offline --expected-version 1.0.3' "$log" | head -n 1 | cut -d: -f1)"
  online_doctor_line="$(grep -n 'run --rm --no-deps worker telegram-media-bot doctor --config /app/config.yaml --online-service local-api --online-service bot' "$log" | head -n 1 | cut -d: -f1)"
  ((offline_doctor_line < start_line && start_line < online_doctor_line)) \
    || fail "offline/start/online verification ordering is wrong"
  [[ -x "$case_root/bin/tmb" ]] \
    || fail "updater did not repair the global tmb command"
  [[ -x "$case_root/scripts/tmb.sh" ]] \
    || fail "release extraction lost the executable mode of scripts/tmb.sh"
  bash -n "$case_root/scripts/tmb.sh" \
    || fail "installed scripts/tmb.sh does not parse"
  PATH="$case_root/bin:$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
    TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" tmb status >/dev/null
  grep -q 'docker .* up -d --no-build --force-recreate bot worker local-api' "$log" \
    || fail "successful update did not recreate the stack"
  grep -q -- '--offline --expected-version 1.0.3' "$log" \
    || fail "successful update did not verify static prerequisites and package version"
  grep -q -- '--online-service local-api --online-service bot' "$log" \
    || fail "successful update did not verify restored live services"
  diff -u <(printf 'bot\nworker\nlocal-api\nredis\n' | sort) \
    <(sort "$case_root/running-services") \
    || fail "successful update did not preserve the exact running service set"
  grep -q 'docker image rm sha256:old-unused' "$log" \
    || fail "unused old project image was not removed after verification"
  grep -q 'docker rm stopped-project-container' "$log" \
    || fail "stopped old Compose project container was not removed"
  if grep -q 'docker image rm sha256:old-used' "$log"; then
    fail "container-referenced old project image was removed"
  fi
  if grep -q 'docker image rm sha256:redis' "$log"; then
    fail "image from another repository was removed"
  fi
  if grep -q 'docker image rm sha256:shared-tag' "$log"; then
    fail "image ID with a foreign repository tag was removed"
  fi
  if grep -Eq 'docker (image prune|system prune|volume prune)' "$log"; then
    fail "unsafe global prune command was used"
  fi
  local doctor_line cleanup_line
  doctor_line="$(grep -n 'doctor --config /app/config.yaml' "$log" | tail -n 1 | cut -d: -f1)"
  cleanup_line="$(grep -n 'docker image rm sha256:old-unused' "$log" | tail -n 1 | cut -d: -f1)"
  ((doctor_line < cleanup_line)) \
    || fail "old image cleanup ran before runtime verification"
}

run_missing_cookie_preflight_case() {
  local case_root="$TEST_ROOT/missing-cookie"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  cp "$case_root/config.yaml" "$TEST_ROOT/missing-cookie-config.expected"
  rm "$case_root/data/cookies/cookies.txt"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "missing preflight cookie unexpectedly succeeded"
  fi

  cmp "$TEST_ROOT/missing-cookie-config.expected" "$case_root/config.yaml" \
    || fail "missing-cookie preflight changed config.yaml"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "missing-cookie preflight changed installed source"
  if grep -q 'docker .* stop -t 45' "$log"; then
    fail "missing-cookie preflight stopped application services"
  fi
  if grep -q '^tar -czf' "$log"; then
    fail "missing-cookie preflight created a post-validation backup"
  fi
}

run_unreadable_cookie_preflight_case() {
  local case_root="$TEST_ROOT/unreadable-cookie"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_PREFLIGHT_COOKIE_UNREADABLE=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "unreadable preflight cookie unexpectedly succeeded"
  fi
  if grep -q 'docker .* stop -t 45' "$log"; then
    fail "unreadable-cookie preflight stopped application services"
  fi
}

run_checksum_failure_case() {
  local case_root="$TEST_ROOT/checksum-failure"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_CHECKSUM=1 \
      TMB_BIN_DIR="$case_root/bin" \
      TMB_CASE_ROOT="$case_root" TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "checksum mismatch unexpectedly succeeded"
  fi

  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "checksum failure did not restore the previous image pin"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "unverified release content was extracted"
  if grep -q 'docker .* pull' "$log"; then
    fail "image pull ran after checksum failure"
  fi
  if grep -q 'docker .* stop -t 45' "$log" || grep -q 'docker .* up -d --no-build' "$log"; then
    fail "pre-stop checksum failure changed service state"
  fi
}

run_download_failure_case() {
  local case_root="$TEST_ROOT/download-failure"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_DOWNLOAD=1 \
      TMB_BIN_DIR="$case_root/bin" \
      TMB_CASE_ROOT="$case_root" TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "release download failure unexpectedly succeeded"
  fi

  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "download failure did not retain the previous image pin"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "download failure changed installed source"
  if grep -q 'docker .* pull' "$log"; then
    fail "image pull ran after release download failure"
  fi
  if grep -q 'docker .* stop -t 45' "$log" || grep -q 'docker .* up -d --no-build' "$log"; then
    fail "pre-stop download failure changed service state"
  fi
}

run_permission_failure_case() {
  local case_root="$TEST_ROOT/permission-failure"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_PERMISSIONS=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "permission normalization failure unexpectedly succeeded"
  fi
  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "permission failure did not restore the previous image"
  grep -q 'docker .* up -d --no-build bot worker local-api redis' "$log" \
    || fail "permission failure did not restart the previous services after rollback"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "permission failure installed new source before rollback"
}

run_health_failure_case() {
  local case_root="$TEST_ROOT/health-failure"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_HEALTH=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ); then
    fail "post-start crash/restart state unexpectedly succeeded"
  fi
  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "health failure did not restore the previous image"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "health failure did not restore the previous application"
  grep -q 'docker .* stop bot' "$log" \
    || fail "crash-looping candidate service was not stopped"
  grep -q 'docker .* up -d --no-build bot worker local-api redis' "$log" \
    || fail "health failure did not restart the previous service set"
  if grep -q 'docker image rm' "$log"; then
    fail "health failure removed rollback images"
  fi
}

run_backup_failure_case() {
  local case_root="$TEST_ROOT/backup-failure"
  local log="$case_root/operations.log" output="$case_root/update-output.log"
  prepare_case "$case_root"
  cp "$case_root/.env" "$case_root/env.expected"
  cp "$case_root/config.yaml" "$case_root/config.expected"
  cp "$case_root/pyproject.toml" "$case_root/pyproject.expected"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_BACKUP=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ) >"$output" 2>&1; then
    fail "persistent-state backup failure unexpectedly succeeded"
  fi
  cmp "$case_root/env.expected" "$case_root/.env" \
    || fail "backup failure changed the image pin"
  cmp "$case_root/config.expected" "$case_root/config.yaml" \
    || fail "backup failure changed config.yaml"
  cmp "$case_root/pyproject.expected" "$case_root/pyproject.toml" \
    || fail "backup failure changed installed application files"
  diff -u <(printf 'bot\nworker\nlocal-api\nredis\n' | sort) \
    <(sort "$case_root/running-services") \
    || fail "backup failure did not restore the exact original service state"
  grep -q 'Update stage failed: consistent persistent-state backup' "$output" \
    || fail "backup failure did not identify its verification stage"
  grep -q 'filesystem snapshot read failed' "$output" \
    || fail "backup failure hid its safe diagnostic reason"
  if grep -q 'DO_NOT_LEAK_BACKUP_SECRET' "$output"; then
    fail "backup failure leaked a secret in diagnostics"
  fi
  if find "$case_root/backups" -maxdepth 1 -type f -name '.tmb-*' | grep -q .; then
    fail "backup failure left a partial archive"
  fi
}

run_offline_doctor_failure_case() {
  local case_root="$TEST_ROOT/offline-doctor-failure"
  local log="$case_root/operations.log" output="$case_root/update-output.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_OFFLINE_DOCTOR=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ) >"$output" 2>&1; then
    fail "offline doctor failure unexpectedly succeeded"
  fi
  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "doctor failure did not restore the previous image"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "doctor failure did not restore the previous application"
  diff -u <(printf 'bot\nworker\nlocal-api\nredis\n' | sort) \
    <(sort "$case_root/running-services") \
    || fail "doctor failure did not restore the exact original service state"
  grep -q 'Update stage failed: offline post-install verification' "$output" \
    || fail "doctor failure did not identify its verification stage"
  grep -q 'database schema compatibility check failed' "$output" \
    || fail "doctor failure hid its safe diagnostic reason"
  if grep -q 'DO_NOT_LEAK_DIAGNOSTIC_SECRET' "$output"; then
    fail "doctor failure leaked a secret in diagnostics"
  fi
  if grep -q 'DO_NOT_LEAK_PROXY_CREDENTIAL' "$output"; then
    fail "doctor failure leaked proxy credentials in diagnostics"
  fi
  grep -Fq 'https://[redacted]@example.invalid' "$output" \
    || fail "doctor diagnostics did not retain a useful redacted endpoint"
  local doctor_line candidate_start_line
  doctor_line="$(grep -n 'run --rm --no-deps worker telegram-media-bot doctor .*--offline' "$log" | head -n 1 | cut -d: -f1)"
  candidate_start_line="$(grep -n 'up -d --no-build --force-recreate' "$log" | head -n 1 | cut -d: -f1 || true)"
  [[ -z "$candidate_start_line" || "$doctor_line" -lt "$candidate_start_line" ]] \
    || fail "doctor failure occurred after candidate writers started"
}

run_online_doctor_failure_case() {
  local case_root="$TEST_ROOT/online-doctor-failure"
  local log="$case_root/operations.log" output="$case_root/update-output.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_ONLINE_DOCTOR=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ) >"$output" 2>&1; then
    fail "post-start online doctor failure unexpectedly succeeded"
  fi
  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.2$' "$case_root/.env" \
    || fail "online doctor failure did not restore the previous image"
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "online doctor failure did not restore the previous application"
  diff -u <(printf 'bot\nworker\nlocal-api\nredis\n' | sort) \
    <(sort "$case_root/running-services") \
    || fail "online doctor failure did not restore the exact original service state"
  grep -q 'Update stage failed: post-start online verification' "$output" \
    || fail "online doctor failure did not identify its verification stage"
  grep -q 'restored Telegram endpoint did not become reachable' "$output" \
    || fail "online doctor failure hid its safe diagnostic reason"
  if grep -q 'DO_NOT_LEAK_ONLINE_DIAGNOSTIC_SECRET' "$output"; then
    fail "online doctor failure leaked a secret in diagnostics"
  fi
  local start_line online_line rollback_line
  start_line="$(grep -n 'up -d --no-build --force-recreate bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  online_line="$(grep -n -- '--online-service local-api --online-service bot' "$log" | head -n 1 | cut -d: -f1)"
  rollback_line="$(grep -n 'stop bot worker local-api' "$log" | tail -n 1 | cut -d: -f1)"
  ((start_line < online_line && online_line < rollback_line)) \
    || fail "online failure did not occur between candidate start and rollback"
}

run_bot_without_local_api_case() {
  local case_root="$TEST_ROOT/bot-without-local-api"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  set_running_services "$case_root" bot worker redis
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  )
  grep -q ' stop -t 45 bot worker$' "$log" \
    || fail "bot/no-Local-API state stopped the wrong writers"
  grep -q -- '--online-service bot' "$log" \
    || fail "restored bot was not online-verified"
  if grep -q -- '--online-service local-api' "$log"; then
    fail "intentionally stopped Local API was online-verified"
  fi
  diff -u <(printf 'bot\nworker\nredis\n' | sort) <(sort "$case_root/running-services") \
    || fail "bot/no-Local-API state was not preserved"
}

run_local_api_without_bot_case() {
  local case_root="$TEST_ROOT/local-api-without-bot"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  set_running_services "$case_root" worker local-api redis
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  )
  grep -q ' stop -t 45 worker local-api$' "$log" \
    || fail "Local-API/no-bot state stopped the wrong writers"
  grep -q -- '--online-service local-api' "$log" \
    || fail "restored Local API was not online-verified"
  if grep -q -- '--online-service bot' "$log"; then
    fail "intentionally stopped bot was online-verified"
  fi
  diff -u <(printf 'worker\nlocal-api\nredis\n' | sort) <(sort "$case_root/running-services") \
    || fail "Local-API/no-bot state was not preserved"
}

run_preflight_interrupt_case() {
  local case_root="$TEST_ROOT/preflight-interrupt"
  local log="$case_root/operations.log" output="$case_root/update-output.log" status
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_INTERRUPT_PREFLIGHT=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  ) >"$output" 2>&1; then
    fail "interrupted candidate preflight unexpectedly succeeded"
  else
    status=$?
  fi
  [[ "$status" -eq 130 ]] || fail "preflight interrupt did not return status 130"
  grep -q '^Update interrupted by operator\.$' "$output" \
    || fail "preflight interrupt did not print a concise operator message"
  grep -q '^Installed release and project service state were unchanged\.$' "$output" \
    || fail "preflight interrupt did not report the unchanged installation"
  if grep -Eq 'Traceback|KeyboardInterrupt' "$output"; then
    fail "preflight interrupt exposed a Python traceback"
  fi
  grep -q '^version = "1.0.2"$' "$case_root/pyproject.toml" \
    || fail "preflight interrupt changed installed application files"
  diff -u <(printf 'bot\nworker\nlocal-api\nredis\n' | sort) \
    <(sort "$case_root/running-services") \
    || fail "preflight interrupt changed service state"
  if grep -q ' stop -t 45 ' "$log" || grep -q '^tar -czf' "$log"; then
    fail "preflight interrupt reached downtime or backup"
  fi
}

run_all_stopped_case() {
  local case_root="$TEST_ROOT/all-stopped"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  set_running_services "$case_root"
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  )
  [[ ! -s "$case_root/running-services" ]] \
    || fail "all-stopped update started services that were intentionally stopped"
  if grep -q ' stop -t 45 ' "$log" || grep -q ' up -d --no-build ' "$log"; then
    fail "all-stopped update changed service state"
  fi
}

run_mixed_service_state_case() {
  local case_root="$TEST_ROOT/mixed-state"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  set_running_services "$case_root" worker redis
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh update
  )
  grep -q ' stop -t 45 worker$' "$log" \
    || fail "mixed-state update did not stop only the running writer"
  grep -q ' up -d --no-build --force-recreate worker$' "$log" \
    || fail "mixed-state update did not restart only the original writer"
  if grep -Eq ' stop .*redis|force-recreate .*(bot|local-api|redis)' "$log"; then
    fail "mixed-state update changed an intentionally preserved service"
  fi
  diff -u <(printf 'worker\nredis\n' | sort) <(sort "$case_root/running-services") \
    || fail "mixed-state update did not preserve the exact running service set"
}

run_real_backup_archive_case() {
  local case_root="$TEST_ROOT/real-backup"
  local log="$case_root/operations.log" archive listing
  prepare_case "$case_root"
  set_running_services "$case_root"
  printf 'wal-sentinel' >"$case_root/data/state/jobs.sqlite3-wal"
  printf 'shm-sentinel' >"$case_root/data/state/jobs.sqlite3-shm"
  printf 'temp-sentinel' >"$case_root/data/temp/existing.part"
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_USE_REAL_TAR=1 \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh backup
  )
  archive="$(find "$case_root/backups" -maxdepth 1 -type f -name 'tmb-*.tar.gz' \
    -print | head -n 1)"
  [[ -n "$archive" && "$(stat -c '%a' "$archive")" == "600" ]] \
    || fail "real backup was not atomically published with mode 0600"
  listing="$(/usr/bin/tar -tzf "$archive")"
  for expected in \
    config.yaml \
    .env \
    data/state/jobs.sqlite3 \
    data/state/jobs.sqlite3-wal \
    data/state/jobs.sqlite3-shm \
    data/cookies/cookies.txt \
    data/telegram-bot-api/state.bin; do
    grep -Fxq "$expected" <<<"$listing" \
      || fail "real backup omitted $expected"
  done
  for excluded in \
    data/telegram-bot-api/telegram-bot-api.log \
    data/downloads/large.mp4 \
    data/temp/existing.part; do
    if grep -Fxq "$excluded" <<<"$listing"; then
      fail "real backup unexpectedly included $excluded"
    fi
  done
}

run_cleanup_dry_run_case() {
  local case_root="$TEST_ROOT/cleanup-dry-run"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" \
      TMB_BIN_DIR="$case_root/bin" TMB_CASE_ROOT="$case_root" \
      TMB_SOURCE_ROOT="$SOURCE_ROOT" \
      bash scripts/tmb.sh cleanup --dry-run
  )
  grep -q 'cleanup-workspaces --config /app/config.yaml --dry-run' "$log" \
    || fail "cleanup dry-run did not plan workspace cleanup"
  if grep -q 'docker image rm' "$log" || grep -q '^docker rm ' "$log"; then
    fail "cleanup dry-run changed Docker resources"
  fi
  if grep -Eq 'docker (image prune|system prune|volume prune)' "$log"; then
    fail "cleanup dry-run invoked unsafe prune"
  fi
}

run_success_case
run_missing_cookie_preflight_case
run_unreadable_cookie_preflight_case
run_checksum_failure_case
run_download_failure_case
run_permission_failure_case
run_health_failure_case
run_backup_failure_case
run_offline_doctor_failure_case
run_online_doctor_failure_case
run_bot_without_local_api_case
run_local_api_without_bot_case
run_preflight_interrupt_case
run_all_stopped_case
run_mixed_service_state_case
run_real_backup_archive_case
run_cleanup_dry_run_case
echo "Linux tmb update recovery tests passed."
