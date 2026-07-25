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
  printf 'telegram:\n  bot_token: CHANGE_ME\n' >"$case_root/config.yaml"
  printf 'TMB_IMAGE=example.invalid/tmb:1.0.0\n' >"$case_root/.env"
  printf 'version = "1.0.0"\n' >"$case_root/pyproject.toml"
  printf 'db' >"$case_root/data/state/jobs.sqlite3"
  printf 'runtime-media' >"$case_root/data/downloads/large.mp4"

  cat >"$case_root/fake-bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$TMB_TEST_LOG"
if [[ "$*" == *" ps --services --filter status=running"* ]]; then
  printf 'bot\nworker\nlocal-api\nredis\n'
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
printf 'version = "2.0.0"\n' >"$destination/pyproject.toml"
printf 'services: {}\n' >"$destination/docker-compose.yml"
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
      bash scripts/tmb.sh update
  )

  grep -q '^TMB_IMAGE=ghcr.io/hamedsanaei/telegram-media-downloader-bot:2.0.0$' \
    "$case_root/.env" || fail "successful update did not pin the verified version"
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
  local stop_line backup_line download_line
  stop_line="$(grep -n 'docker .* stop -t 45 bot worker local-api' "$log" | head -n 1 | cut -d: -f1)"
  backup_line="$(grep -n '^tar -czf' "$log" | head -n 1 | cut -d: -f1)"
  download_line="$(grep -n '^curl ' "$log" | head -n 1 | cut -d: -f1)"
  ((stop_line < backup_line && backup_line < download_line)) \
    || fail "stop, consistent backup, and release download ordering is wrong"
  grep -q 'docker .* pull' "$log" || fail "updated images were not pulled"
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
      bash scripts/tmb.sh update
  ); then
    fail "checksum mismatch unexpectedly succeeded"
  fi

  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.0$' "$case_root/.env" \
    || fail "checksum failure did not restore the previous image pin"
  grep -q '^version = "1.0.0"$' "$case_root/pyproject.toml" \
    || fail "unverified release content was extracted"
  if grep -q 'docker .* pull' "$log"; then
    fail "image pull ran after checksum failure"
  fi
  grep -q 'docker .* up -d --no-build bot worker local-api$' "$log" \
    || fail "previous stack was not restarted after checksum failure"
}

run_download_failure_case() {
  local case_root="$TEST_ROOT/download-failure"
  local log="$case_root/operations.log"
  prepare_case "$case_root"
  if (
    cd "$case_root"
    PATH="$case_root/fake-bin:$PATH" TMB_TEST_LOG="$log" TMB_FAIL_DOWNLOAD=1 \
      bash scripts/tmb.sh update
  ); then
    fail "release download failure unexpectedly succeeded"
  fi

  grep -q '^TMB_IMAGE=example.invalid/tmb:1.0.0$' "$case_root/.env" \
    || fail "download failure did not retain the previous image pin"
  grep -q '^version = "1.0.0"$' "$case_root/pyproject.toml" \
    || fail "download failure changed installed source"
  if grep -q 'docker .* pull' "$log"; then
    fail "image pull ran after release download failure"
  fi
  grep -q 'docker .* up -d --no-build bot worker local-api$' "$log" \
    || fail "previous stack was not restarted after download failure"
}

run_success_case
run_checksum_failure_case
run_download_failure_case
echo "Linux tmb update recovery tests passed."
