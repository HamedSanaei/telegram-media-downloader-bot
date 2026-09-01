#!/usr/bin/env bash
set -euo pipefail

# BUG 1 regression (v1.4.0-rc.2): the candidate read-only doctor preflight must
# never open the WAL-backed SQLite logger database.
#
# RC2 symptom (reproduced): a cleanly closed WAL database has no -wal/-shm
# sidecars, so ANY sqlite open on a read-only bind mount must create -shm and
# fails with `sqlite3.OperationalError: unable to open database file`
# (SQLITE_CANTOPEN=14); the doctor reported
# `FAIL operator_logger: enabled;durable_state=unavailable` and blocked updates
# whenever the operator logger was enabled.
#
# The fix: `--read-only-runtime` performs filesystem-level validation only and
# reports `OK operator_logger: enabled;durable_state=deferred-readonly` without
# touching SQLite. The strong post-stop verification (no --read-only-runtime)
# still opens the database and validates the full health snapshot.

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d)"
IMAGE="${TMB_READONLY_IMAGE:-telegram-media-downloader-bot:readonly-test}"
RELEASE_VERSION="$(
  sed -n 's/^version = "\([^"]*\)"/\1/p' "$SOURCE_ROOT/pyproject.toml" | head -n 1
)"
DATA="$TEST_ROOT/data"

cleanup() {
  if [[ "${TMB_KEEP_TEST_ROOT:-0}" != "1" ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}
trap cleanup EXIT

fail() {
  echo "read-only logger preflight test failed: $1" >&2
  exit 1
}

mkdir -p "$DATA/state" "$DATA/cookies" "$DATA/temp" "$DATA/downloads"

write_config() {
  local enabled="$1"
  docker run --rm -i \
    -e LOGGER_ENABLED="$enabled" \
    -v "$SOURCE_ROOT/config.example.yaml:/input/config.example.yaml:ro" \
    -v "$DATA:/data" \
    "$IMAGE" python - <<'PY'
from pathlib import Path
import os
import yaml
source = Path("/input/config.example.yaml")
path = Path("/data/config.yaml")
raw = yaml.safe_load(source.read_text(encoding="utf-8"))
raw["storage"]["root_directory"] = "/data"
raw["telegram"]["logger"] = {
    "enabled": os.environ["LOGGER_ENABLED"] == "true",
    "channels": [-1001234567890],
    "alerts_enabled": True,
    "submission_mirror_enabled": True,
    "operator_privacy_attested": True,
    "privacy_notice_version": "logger-v1",
}
raw["yt_dlp"]["cookies_file"] = None
path.write_text(yaml.safe_dump(raw), encoding="utf-8")
PY
}

ensure_image() {
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    return 0
  fi
  docker build --build-arg PYTHON_VERSION=3.14.5 -t "$IMAGE" "$SOURCE_ROOT"
}

snapshot_state() {
  (cd "$DATA" && find . -type f -exec sha256sum {} + | sort)
}

preflight() {
  docker run --rm --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
    -v "$DATA/config.yaml:/app/config.yaml:ro" \
    -v "$DATA:/data:ro" \
    "$IMAGE" \
    telegram-media-bot doctor --config /app/config.yaml --offline \
      --expected-version "$RELEASE_VERSION" --read-only-runtime "$@"
}

full_doctor() {
  docker run --rm \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
    -v "$DATA/config.yaml:/app/config.yaml:ro" \
    -v "$DATA:/data" \
    "$IMAGE" \
    telegram-media-bot doctor --config /app/config.yaml --offline \
      --expected-version "$RELEASE_VERSION"
}

strong_doctor_readonly() {
  docker run --rm --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m,mode=1777 \
    -v "$DATA/config.yaml:/app/config.yaml:ro" \
    -v "$DATA:/data:ro" \
    "$IMAGE" \
    telegram-media-bot doctor --config /app/config.yaml --offline \
      --expected-version "$RELEASE_VERSION"
}

init_database() {
  docker run --rm -i -v "$DATA:/data" "$IMAGE" python - <<'PY'
from pathlib import Path
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
repo = SqliteAuditRepository(Path("/data/state/jobs.sqlite3"))
repo.initialize()
repo.reconcile_config((-1001234567890,))
snapshot = repo.health_snapshot()
print(snapshot.active_destinations)
PY
}

main() {
  ensure_image
  write_config true

  # Phase A: initialize a real WAL-mode SqliteAuditRepository with one ACTIVE
  # destination. On clean close sqlite checkpoints and removes -wal/-shm,
  # exactly reproducing the production preflight conditions.
  local initialized
  initialized="$(init_database)"
  [[ "$initialized" == "1" ]] || fail "expected one ACTIVE destination, got '$initialized'"
  if [[ -e "$DATA/state/jobs.sqlite3-wal" || -e "$DATA/state/jobs.sqlite3-shm" ]]; then
    fail "expected cleanly closed WAL DB without -wal/-shm sidecars"
  fi

  local before after output rc
  before="$(snapshot_state)"
  sha256sum "$DATA/config.yaml" >"$TEST_ROOT/config.before.sha"

  # Phase B: read-only preflight with logger enabled MUST pass and MUST NOT
  # touch SQLite or any durable bytes.
  output="$(preflight 2>&1)" || rc=$?
  rc="${rc:-0}"
  [[ "$rc" -eq 0 ]] || {
    echo "$output" >&2
    fail "read-only preflight exited $rc instead of 0"
  }
  echo "$output" | grep -q "OK   operator_logger: enabled;durable_state=deferred-readonly" \
    || fail "expected deferred-readonly diagnostic, got: $(echo "$output" | grep operator_logger || true)"

  after="$(snapshot_state)"
  [[ "$before" == "$after" ]] || fail "read-only preflight mutated durable state"
  [[ -e "$DATA/state/jobs.sqlite3-wal" || -e "$DATA/state/jobs.sqlite3-shm" ]] \
    && fail "read-only preflight created -wal/-shm sidecars"
  sha256sum -c "$TEST_ROOT/config.before.sha" >/dev/null || fail "config.yaml mutated"

  # Phase B2: negative control — the RC2 symptom. A strong doctor (no
  # --read-only-runtime) against the read-only mount must reproduce
  # `enabled;durable_state=unavailable`, proving the failure is real and the
  # read-only flag is what fixes it.
  set +e
  output="$(strong_doctor_readonly 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || fail "strong doctor against read-only mount unexpectedly passed"
  echo "$output" | grep -q "FAIL operator_logger: enabled;durable_state=unavailable" \
    || fail "expected RC2 unavailable symptom in strong doctor against ro mount"

  # Phase C: strong post-stop verification (writable /data) still performs the
  # full health snapshot and passes for the healthy database.
  output="$(full_doctor)"
  echo "$output" | grep -q "OK   operator_logger: enabled;.*effective=1;active=1" \
    || fail "strong post-stop doctor did not validate the healthy snapshot: $(echo "$output" | grep operator_logger || true)"

  # Phase D: corrupt database must fail the strong post-stop verification.
  docker run --rm -v "$DATA:/data" "$IMAGE" sh -c \
    "printf 'this is not a sqlite database\\0\\0\\0\\0' > /data/state/jobs.sqlite3"
  set +e
  output="$(full_doctor 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || fail "strong doctor accepted a corrupt logger database"
  echo "$output" | grep -q "FAIL operator_logger: enabled;durable_state=unavailable" \
    || fail "corrupt DB did not fail strong verification"

  # Phase E: logger disabled -> read-only preflight passes without touching DB.
  docker run --rm -v "$DATA:/data" "$IMAGE" \
    rm -f /data/state/jobs.sqlite3 /data/state/jobs.sqlite3-wal /data/state/jobs.sqlite3-shm
  write_config false
  output="$(preflight 2>&1)"
  echo "$output" | grep -q "OK   operator_logger: disabled" \
    || fail "logger disabled preflight did not report disabled"

  # Phase F: logger enabled + database missing -> preflight fails safely.
  write_config true
  set +e
  output="$(preflight 2>&1)"
  rc=$?
  set -e
  [[ "$rc" -ne 0 ]] || fail "missing logger database passed read-only preflight"
  echo "$output" | grep -q "FAIL operator_logger: enabled;durable_state=missing" \
    || fail "missing DB did not fail read-only preflight safely"

  echo "read-only logger preflight test passed."
}

main "$@"
