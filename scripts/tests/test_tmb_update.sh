#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() {
  echo "tmb update test failed: $1" >&2
  exit 1
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
  printf 'telegram:\n  bot_token: V1_CONFIG_SENTINEL\n' >"$case_root/config.yaml"
  printf 'TMB_IMAGE=example.invalid/tmb:1.0.2\nCOMPOSE_PROFILES=local-api\nAPP_UID=10001\nAPP_GID=10001\nTMB_WORKER_CPUS=1.5\n' \
    >"$case_root/.env"
  printf 'version = "1.0.2"\n' >"$case_root/pyproject.toml"
  printf 'sqlite-v1-state' >"$case_root/data/state/jobs.sqlite3"
  printf 'cookies-v1-state' >"$case_root/data/cookies/cookies.txt"
  printf 'local-api-v1-state' >"$case_root/data/telegram-bot-api/state.bin"
  printf 'runtime-media-v1' >"$case_root/data/downloads/large.mp4"

  cat >"$case_root/fake-bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$TMB_TEST_LOG"
if [[ "$*" == *" ps --services --filter status=running"* ]]; then
  printf 'bot\nworker\nlocal-api\nredis\n'
fi
case "$*" in
  *" ps -q bot") printf 'bot-container\n' ;;
  *" ps -q worker") printf 'worker-container\n' ;;
  *" ps -q local-api") printf 'local-api-container\n' ;;
esac
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
if [[ "$1" == "-czf" ]]; then
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
  prepare_case "$case_root"
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
  grep -q 'V1_CONFIG_SENTINEL' "$case_root/config.yaml" \
    || fail "successful update overwrote config.yaml"
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
  local stop_line backup_line download_line permission_line start_line
  stop_line="$(grep -n 'docker .* stop -t 45 bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  backup_line="$(grep -n '^tar -czf' "$log" | head -n 1 | cut -d: -f1)"
  download_line="$(grep -n '^curl ' "$log" | head -n 1 | cut -d: -f1)"
  ((download_line < backup_line && backup_line < stop_line)) \
    || fail "release validation, backup, and service-stop ordering is wrong"
  grep -q 'docker .* pull' "$log" || fail "updated images were not pulled"
  grep -q 'docker run --rm --user 0 --entrypoint sh' "$log" \
    || fail "runtime permissions were not normalized through the release image"
  permission_line="$(grep -n 'docker run --rm --user 0 --entrypoint sh' "$log" | head -n 1 | cut -d: -f1)"
  start_line="$(grep -n 'docker .* up -d --no-build --force-recreate bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  ((permission_line < start_line)) \
    || fail "runtime permission migration did not finish before service start"
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
  grep -q 'docker .* up -d --no-build bot worker local-api' "$log" \
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
  grep -q 'docker .* up -d --no-build bot worker local-api' "$log" \
    || fail "health failure did not restart the previous service set"
}

run_success_case
run_checksum_failure_case
run_download_failure_case
run_permission_failure_case
run_health_failure_case
echo "Linux tmb update recovery tests passed."
