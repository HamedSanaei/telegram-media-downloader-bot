#!/usr/bin/env bash
# --------------------------------------------------------------------------- #
# Deterministic test suite for the tmb control plane (scripts/tmb.sh +
# scripts/lib/*). Uses a stateful fake docker so no real Docker daemon, root,
# or network is required. Covers dispatch, help/version, usage errors,
# no-TTY behavior, status, services, logs, storage scoping, backup manifest/
# checksum/permissions, archive attack rejection, transactional restore with
# rollback and interrupt recovery, exact service-state restoration, the
# management lock, uninstall, and secret redaction.
# --------------------------------------------------------------------------- #
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMB_SCRIPT="$(cd "$SCRIPT_DIR/.." && pwd)/tmb.sh"
TEST_ROOT="$(mktemp -d)"
FIXTURE="$TEST_ROOT/install"
FAKE_BIN="$TEST_ROOT/bin"
STATE_FILE="$TEST_ROOT/docker-state"
DOCKER_LOG="$TEST_ROOT/docker.log"
COUNTER_FILE="$TEST_ROOT/run-counter"
INT_MARKER="$TEST_ROOT/int-marker"
export TMB_FAKE_DOCKER_STATE="$STATE_FILE"
export TMB_FAKE_DOCKER_LOG="$DOCKER_LOG"
export TMB_FAKE_DOCKER_COUNTER="$COUNTER_FILE"
export TMB_FAKE_DOCKER_MARKER="$INT_MARKER"

PASS=0
FAIL=0

pass() {
  PASS=$((PASS + 1))
  printf 'ok   - %s\n' "$1"
}

fail() {
  FAIL=$((FAIL + 1))
  printf 'FAIL - %s\n' "$1"
}

check_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$label"
  else
    fail "$label (expected [$expected], got [$actual])"
  fi
}

check_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    pass "$label"
  else
    fail "$label (missing [$needle] in output)"
  fi
}

check_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ "$haystack" != *"$needle"* ]]; then
    pass "$label"
  else
    fail "$label (unexpected [$needle] in output)"
  fi
}

# --------------------------------------------------------------------------- #
# Fixture: fake install tree + stateful fake docker
# --------------------------------------------------------------------------- #
setup_fixture() {
  mkdir -p "$FIXTURE/data/state" "$FIXTURE/data/cookies" \
    "$FIXTURE/data/telegram-bot-api" "$FIXTURE/data/downloads" \
    "$FIXTURE/data/temp" "$FIXTURE/backups" "$FAKE_BIN"
  cat >"$FIXTURE/pyproject.toml" <<'EOF'
[project]
name = "telegram-media-downloader-bot"
version = "9.9.9-test"
EOF
  cat >"$FIXTURE/config.yaml" <<'EOF'
telegram:
  bot_token: "123456:TESTtoken"
  admin_ids: [111, 222]
  support_username: "@support"
  required_channels:
    enabled: true
    channels:
      - chat_id: -100123
        title: "My Channel"
  logger:
    enabled: false
  local_api_is_local: false
EOF
  cat >"$FIXTURE/.env" <<'EOF'
TMB_IMAGE=ghcr.io/hamedsanaei/telegram-media-downloader-bot:9.9.9-test
APP_UID=10001
APP_GID=10001
EOF
  printf 'bot\nworker\n' >"$STATE_FILE"
  printf 'not-a-real-sqlite-placeholder\n' >"$FIXTURE/data/state/jobs.sqlite3"
  printf 'cookie-value-placeholder\n' >"$FIXTURE/data/cookies/cookies.txt"
  printf 'download-artifact\n' >"$FIXTURE/data/downloads/video.mp4"
  printf 'temp-artifact\n' >"$FIXTURE/data/temp/tmp.work"
  : >"$DOCKER_LOG"
  : >"$COUNTER_FILE"
}

setup_fixture

cat >"$FAKE_BIN/docker" <<'FAKE_DOCKER'
#!/usr/bin/env python3
# Stateful fake docker for deterministic tmb tests. Implemented in Python
# (a native executable) because on msys/Git Bash spawning bash-script children
# in rapid succession deadlocks the fork emulation; native interpreters do not.
import os
import re
import signal
import sys

state_file = os.environ["TMB_FAKE_DOCKER_STATE"]
log_file = os.environ.get("TMB_FAKE_DOCKER_LOG", os.devnull)
counter_file = os.environ.get("TMB_FAKE_DOCKER_COUNTER") or ""
marker = os.environ.get("TMB_FAKE_DOCKER_MARKER") or ""

# newline="\n" keeps LF-only output on Windows (text mode would write CRLF).
with open(log_file, "a", encoding="utf-8", newline="\n") as handle:
    handle.write("docker %s\n" % " ".join(sys.argv[1:]))


def count_run() -> int:
    n = 0
    if counter_file and os.path.exists(counter_file):
        with open(counter_file, encoding="utf-8") as handle:
            n = int(handle.read().strip() or "0")
    n += 1
    if counter_file:
        with open(counter_file, "w", encoding="utf-8") as handle:
            handle.write(str(n))
    return n


def maybe_interrupt() -> None:
    if os.environ.get("TMB_TEST_FAKE_DOCKER_INT") == "1":
        n = count_run()
        if n == int(os.environ.get("TMB_TEST_FAKE_DOCKER_INT_AT", "4")):
            if marker:
                open(marker, "w").close()
            import time

            time.sleep(1)
            os.kill(os.getppid(), signal.SIGINT)
            sys.exit(0)


def add_running(services):
    existing = []
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8", newline="\n") as handle:
            existing = [line.strip() for line in handle if line.strip()]
    for service in services:
        if not service.startswith("-") and service not in existing:
            existing.append(service)
    with open(state_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join("%s\n" % s for s in existing))


def remove_running(services):
    remove = {s for s in services if not s.startswith("-")}
    if not remove:
        return
    if not os.path.exists(state_file):
        return
    with open(state_file, encoding="utf-8", newline="\n") as handle:
        remaining = [line for line in handle if line.strip() not in remove]
    with open(state_file, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(remaining))


args = sys.argv[1:]
if args and args[0] == "compose":
    args = args[1:]
    while args and args[0] in ("--project-directory", "--profile"):
        args = args[2:]
    while args and args[0].startswith("-"):
        args = args[1:]
    action = args[0] if args else ""
    rest = args[1:]
    if action == "ps":
        if "-q" in rest:
            print("container-%s" % rest[-1])
        elif "--filter" in rest and "status=running" in rest:
            # Real docker splits "--filter status=running" into two argv tokens.
            if os.path.exists(state_file):
                with open(state_file, encoding="utf-8") as handle:
                    sys.stdout.write(handle.read())
        else:
            print("NAME                IMAGE     STATUS")
    elif action == "stop":
        remove_running(rest)
    elif action == "up":
        maybe_interrupt()
        add_running(rest)
    elif action == "down":
        open(state_file, "w").close()
    elif action == "run":
        maybe_interrupt()
    elif action == "logs":
        print("INFO  safe diagnostic line")
        print("ERROR bot_token=123456:TESTtoken leaked")
    sys.exit(0)

if args and args[0] == "image":
    if "RepoDigests" in " ".join(args):
        print("ghcr.io/hamedsanaei/telegram-media-downloader-bot@sha256:abcdef0123456789")
    else:
        print("sha256:abcdef0123456789")
    sys.exit(0)

if args and args[0] == "inspect":
    fmt = args[2] if len(args) > 2 else ""
    if "State.Status" in fmt:
        print("running")
    elif "State.Health" in fmt:
        print("none")
    elif "RestartCount" in fmt:
        print("0")
    elif "State.StartedAt" in fmt:
        print("2026-01-01T00:00:00Z")
    sys.exit(0)

if args and args[0] == "run":
    maybe_interrupt()
    if os.environ.get("TMB_TEST_FAKE_DOCKER_RUN_FAIL") == "1":
        print("fake docker run failed (test)", file=sys.stderr)
        sys.exit(1)
    print("ok")
    sys.exit(0)

if args and args[0] == "exec":
    print("PONG")
    sys.exit(0)

if args and args[0] == "version":
    print("26.0.0")
    sys.exit(0)

sys.exit(0)
FAKE_DOCKER
chmod 755 "$FAKE_BIN/docker"

run_tmb() {
  TMB_ROOT_DIR="$FIXTURE" PATH="$FAKE_BIN:$PATH" bash "$TMB_SCRIPT" "$@" 2>&1
}

# --------------------------------------------------------------------------- #
# 1. Help / version / usage errors / no-TTY menu
# --------------------------------------------------------------------------- #

out="$(run_tmb --help)" && code=0 || code=$?
check_eq "tmb --help exits 0" "0" "$code"
check_contains "tmb --help shows usage" "Usage: tmb COMMAND" "$out"

out="$(run_tmb help)" && code=0 || code=$?
check_eq "tmb help exits 0" "0" "$code"
check_contains "tmb help lists backup" "backup [create|list|inspect FILE|verify FILE|delete FILE]" "$out"

out="$(run_tmb help backup)" && code=0 || code=$?
check_eq "tmb help backup exits 0" "0" "$code"
check_contains "tmb help backup shows usage" "Usage: tmb backup" "$out"

out="$(run_tmb help restore)" && code=0 || code=$?
check_eq "tmb help restore exits 0" "0" "$code"
check_contains "tmb help restore shows usage" "Usage: tmb restore" "$out"

out="$(run_tmb help logs)" && code=0 || code=$?
check_eq "tmb help logs exits 0" "0" "$code"
check_contains "tmb help logs shows usage" "Usage: tmb logs" "$out"

out="$(run_tmb version)" && code=0 || code=$?
check_eq "tmb version exits 0" "0" "$code"
check_contains "tmb version shows app version" "Application version: 9.9.9-test" "$out"
check_not_contains "tmb version never shows the token" "TESTtoken" "$out"

out="$(run_tmb --version)" && code=0 || code=$?
check_eq "tmb --version exits 0" "0" "$code"

out="$(run_tmb bogus-command)" && code=0 || code=$?
check_eq "unknown command exits 2" "2" "$code"
check_contains "unknown command reports error" "Unknown command: bogus-command" "$out"

out="$(run_tmb logs --help)" && code=0 || code=$?
check_eq "tmb logs --help exits 0 (nested help)" "0" "$code"
check_contains "tmb logs --help shows usage" "Usage: tmb logs" "$out"

out="$(printf '0\n' | run_tmb)" && code=0 || code=$?
check_eq "no-TTY menu with 0 exits 0" "0" "$code"
check_contains "no-TTY menu prints banner" "Telegram Media Downloader Bot Manager" "$out"

# --------------------------------------------------------------------------- #
# 2. Status
# --------------------------------------------------------------------------- #
out="$(run_tmb status)" && code=0 || code=$?
check_eq "tmb status exits 0" "0" "$code"
check_contains "status shows version" "Application version: 9.9.9-test" "$out"
check_contains "status shows bot running" "bot" "$out"
check_contains "status shows telegram configured" "Telegram: token configured" "$out"
check_not_contains "status hides the token" "TESTtoken" "$out"
check_contains "status shows logger state" "Operator Logger:" "$out"
check_contains "status shows required channels state" "Required channels policy:" "$out"

# --------------------------------------------------------------------------- #
# 3. Services
# --------------------------------------------------------------------------- #
out="$(run_tmb services ps)" && code=0 || code=$?
check_eq "tmb services ps exits 0" "0" "$code"
# On msys the fixture path is converted to a Windows path when passed to the
# native python fake; normalize with cygpath so the needle matches everywhere.
normalized_fixture="$(cygpath -m "$FIXTURE" 2>/dev/null || printf '%s' "$FIXTURE")"
check_contains "services ps invokes compose" "compose --project-directory $normalized_fixture --profile local-api ps" "$(cat "$DOCKER_LOG")"

out="$(run_tmb services stop-one bot)" && code=0 || code=$?
check_eq "stop-one bot exits 0" "0" "$code"
check_eq "stop-one removes bot from running state" "worker" "$(cat "$STATE_FILE")"

out="$(run_tmb services start-one bot)" && code=0 || code=$?
check_eq "start-one bot exits 0" "0" "$code"
check_eq "start-one restores bot to running state" "worker
bot" "$(cat "$STATE_FILE")"

out="$(run_tmb services bogus)" && code=0 || code=$?
check_eq "invalid services action exits 2" "2" "$code"

# --------------------------------------------------------------------------- #
# 4. Logs
# --------------------------------------------------------------------------- #
out="$(run_tmb logs)" && code=0 || code=$?
check_eq "tmb logs exits 0" "0" "$code"
check_contains "logs --tail 100 passed to compose" "--tail 100" "$(cat "$DOCKER_LOG")"
check_not_contains "logs redact bot tokens" "TESTtoken" "$out"
check_contains "logs redaction placeholder shown" "redacted sensitive line" "$out"

out="$(run_tmb logs worker --tail 500)" && code=0 || code=$?
check_eq "tmb logs worker --tail 500 exits 0" "0" "$code"
check_contains "logs service passed through" "worker" "$(cat "$DOCKER_LOG")"
check_contains "logs --tail 500 passed through" "--tail 500" "$(cat "$DOCKER_LOG")"

out="$(run_tmb logs --since 2h)" && code=0 || code=$?
check_eq "tmb logs --since 2h exits 0" "0" "$code"
check_contains "logs --since 2h passed through" "--since 2h" "$(cat "$DOCKER_LOG")"

out="$(run_tmb logs worker --since 24h --tail 200)" && code=0 || code=$?
check_eq "tmb logs worker --since 24h --tail 200 exits 0" "0" "$code"

out="$(run_tmb logs --tail abc)" && code=0 || code=$?
check_eq "invalid --tail exits 2" "2" "$code"

out="$(run_tmb logs unknown-service)" && code=0 || code=$?
check_eq "unknown log service exits 2" "2" "$code"

# --------------------------------------------------------------------------- #
# 5. Storage overview + project-scoped cleanup
# --------------------------------------------------------------------------- #
out="$(run_tmb storage)" && code=0 || code=$?
check_eq "tmb storage exits 0" "0" "$code"
check_contains "storage shows downloads" "downloads" "$out"

out="$(run_tmb storage cleanup-downloads --yes)" && code=0 || code=$?
check_eq "cleanup-downloads exits 0" "0" "$code"
check_eq "downloads cleaned" "0" "$(find "$FIXTURE/data/downloads" -mindepth 1 | wc -l)"
[[ -f "$FIXTURE/data/state/jobs.sqlite3" ]]
pass "cleanup-downloads preserves state directory"
[[ -f "$FIXTURE/data/cookies/cookies.txt" ]]
pass "cleanup-downloads preserves cookies"

out="$(run_tmb storage cleanup-temp --yes)" && code=0 || code=$?
check_eq "cleanup-temp exits 0" "0" "$code"
check_eq "temp cleaned" "0" "$(find "$FIXTURE/data/temp" -mindepth 1 | wc -l)"

out="$(run_tmb storage bogus)" && code=0 || code=$?
check_eq "invalid storage action exits 2" "2" "$code"

# --------------------------------------------------------------------------- #
# 6. Backup: create / list / inspect / verify / permissions / manifest
# --------------------------------------------------------------------------- #
out="$(run_tmb backup create)" && code=0 || code=$?
check_eq "backup create exits 0" "0" "$code"
archive="$(printf '%s\n' "$out" | sed -n 's/^Backup created: //p' | head -n 1)"
archive_abs="$FIXTURE/$archive"
[[ -f "$archive_abs" ]]
pass "backup archive exists"
[[ -f "$archive_abs.sha256" ]]
pass "backup checksum file exists"

if [[ "$(uname -s)" == "Linux" ]]; then
  check_eq "backup archive mode is 0600" "600" "$(stat -c '%a' "$archive_abs")"
  check_eq "backup checksum mode is 0600" "600" "$(stat -c '%a' "$archive_abs.sha256")"
else
  pass "backup permissions checked on Linux only (host: $(uname -s))"
fi

manifest="$(tar -xzOf "$archive_abs" manifest.json)"
check_contains "backup manifest has schema version" '"schema_version": 1' "$manifest"
check_contains "backup manifest has kind operational" '"kind": "operational"' "$manifest"
check_contains "backup manifest has app version" '"app_version": "9.9.9-test"' "$manifest"
check_contains "backup manifest lists config.yaml" '"config.yaml"' "$manifest"
check_contains "backup manifest lists state" '"data/state"' "$manifest"
check_not_contains "backup manifest excludes downloads" '"data/downloads"' "$manifest"

out="$(run_tmb backup list)" && code=0 || code=$?
check_eq "backup list exits 0" "0" "$code"
check_contains "backup list shows archive" "$(basename "$archive")" "$out"

out="$(run_tmb backup verify "$archive")" && code=0 || code=$?
check_eq "backup verify exits 0" "0" "$code"
check_contains "backup verify reports OK" "Backup is valid" "$out"
check_contains "backup verify reports checksum" "Checksum: OK" "$out"

out="$(run_tmb backup inspect "$archive")" && code=0 || code=$?
check_eq "backup inspect exits 0" "0" "$code"
check_contains "backup inspect shows entries" "config.yaml" "$out"
check_contains "backup inspect shows kind" "kind: operational" "$out"

# --------------------------------------------------------------------------- #
# 7. Backup rejection: corrupted archive, checksum mismatch, traversal, symlink
# --------------------------------------------------------------------------- #
corrupt="$TEST_ROOT/corrupt.tar.gz"
cp "$archive_abs" "$corrupt"
printf 'X' | dd of="$corrupt" bs=1 seek=200 conv=notrunc status=none
out="$(run_tmb backup verify "$corrupt")" && code=0 || code=$?
check_eq "corrupted archive rejected" "1" "$code"
check_contains "corrupted archive reports invalid gzip" "not a valid gzip archive" "$out"

tampered="$TEST_ROOT/tampered.tar.gz"
cp "$archive_abs" "$tampered"
printf '0' >"$tampered.sha256"
out="$(run_tmb backup verify "$tampered")" && code=0 || code=$?
check_eq "checksum mismatch rejected" "1" "$code"
check_contains "checksum mismatch reported" "checksum verification failed" "$out"

# Build attack archives with python tarfile so no real symlink (Windows needs
# privileges) and no host filesystem quirks are involved.
python3 - "$TEST_ROOT" <<'PY'
import gzip
import io
import os
import sys
import tarfile

root = sys.argv[1]

# Absolute-path member: rejected as unsafe by validate_backup_archive.
with tarfile.open(os.path.join(root, "traversal.tar.gz"), "w:gz") as archive:
    info = tarfile.TarInfo("/etc/passwd")
    data = b"evil\n"
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))

# Symlink member: rejected as a symlink/special file.
with tarfile.open(os.path.join(root, "symlink.tar.gz"), "w:gz") as archive:
    info = tarfile.TarInfo("evil-link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    archive.addfile(info)
PY
out="$(run_tmb backup verify "$TEST_ROOT/traversal.tar.gz")" && code=0 || code=$?
check_eq "path traversal archive rejected" "1" "$code"
check_contains "path traversal reported" "unsafe path entry" "$out"

out="$(run_tmb backup verify "$TEST_ROOT/symlink.tar.gz")" && code=0 || code=$?
check_eq "symlink archive rejected" "1" "$code"
check_contains "symlink reported" "symlink or special file" "$out"

# --------------------------------------------------------------------------- #
# 8. Restore: dry-run, transactional success, exact state restoration
# --------------------------------------------------------------------------- #
out="$(run_tmb restore --dry-run "$archive")" && code=0 || code=$?
check_eq "restore --dry-run exits 0" "0" "$code"
check_contains "restore dry-run changes nothing" "Dry run: archive validation passed; nothing was changed" "$out"
check_contains "restore dry-run shows backup kind" "kind: operational" "$out"

state_before_restore="$(cat "$STATE_FILE")"
printf '# modified-after-backup\n' >>"$FIXTURE/config.yaml"
out="$(run_tmb restore "$archive")" && code=0 || code=$?
check_eq "restore exits 0" "0" "$code"
check_contains "restore reports success" "Restore completed successfully" "$out"
check_not_contains "restore restores config from backup" "modified-after-backup" "$(cat "$FIXTURE/config.yaml")"
check_contains "restore keeps original config content" "TESTtoken" "$(cat "$FIXTURE/config.yaml")"
check_eq "restore restores exact service state" "$state_before_restore" "$(cat "$STATE_FILE")"
restore_dirs="$(find "$FIXTURE" -maxdepth 1 -name '.tmb-restore.*' | wc -l)"
check_eq "restore cleans transaction directories" "0" "$restore_dirs"
check_eq "pre-restore safety backup retained" "2" "$(find "$FIXTURE/backups" -name 'tmb-*.tar.gz' | wc -l)"
check_eq "support bundle never created by restore" "0" "$(find "$FIXTURE/backups" -name 'support-bundle-*' | wc -l)"

# --------------------------------------------------------------------------- #
# 9. Restore validation failure: nothing modified, services restored
# --------------------------------------------------------------------------- #
state_before_failure="$(cat "$STATE_FILE")"
printf '# modified-after-backup-2\n' >>"$FIXTURE/config.yaml"
out="$(TMB_TEST_FAKE_DOCKER_RUN_FAIL=1 run_tmb restore "$archive")" && code=0 || code=$?
check_eq "restore validation failure exits non-zero" "1" "$code"
check_contains "restore validation failure reported" "failed validation" "$out"
check_contains "restore failure leaves config untouched" "modified-after-backup-2" "$(cat "$FIXTURE/config.yaml")"
check_eq "restore failure restores service state" "$state_before_failure" "$(cat "$STATE_FILE")"

# --------------------------------------------------------------------------- #
# 10. Interrupted restore: SIGINT triggers automatic rollback
# --------------------------------------------------------------------------- #
state_before_interrupt="$(cat "$STATE_FILE")"
printf '# modified-before-interrupt\n' >>"$FIXTURE/config.yaml"
rm -f "$INT_MARKER" "$COUNTER_FILE"
: >"$COUNTER_FILE"
TMB_TEST_FAKE_DOCKER_INT=1 TMB_TEST_FAKE_DOCKER_INT_AT=4 \
  run_tmb restore "$archive" >"$TEST_ROOT/int-restore.out" 2>&1 &
restore_pid=$!
waited=0
while [[ ! -f "$INT_MARKER" && $waited -lt 20 ]]; do
  sleep 0.5
  waited=$((waited + 1))
done
wait "$restore_pid" && int_code=0 || int_code=$?
# On Linux the fake delivers a real SIGINT to the transaction subshell and the
# restore_interrupt trap runs (exit 130 + explicit message). On msys/Windows
# python cannot signal the parent bash, so the probe fails and the ordinary
# rollback path restores the pre-restore state; both must leave the system
# consistent and roll the config back to its pre-restore content.
if [[ "$(uname -s)" == "Linux" ]]; then
  check_eq "interrupted restore exits 130" "130" "$int_code"
  check_contains "interrupted restore reports interrupt" "Restore interrupted by operator" "$(cat "$TEST_ROOT/int-restore.out")"
else
  check_eq "interrupted restore exits non-zero" "1" "$int_code"
  check_contains "interrupted restore reports rollback" "rolling back" "$(cat "$TEST_ROOT/int-restore.out")"
fi
check_contains "interrupted restore rolls config back" "modified-before-interrupt" "$(cat "$FIXTURE/config.yaml")"
check_eq "interrupted restore restores services" "$state_before_interrupt" "$(cat "$STATE_FILE")"
restore_dirs="$(find "$FIXTURE" -maxdepth 1 -name '.tmb-restore.*' | wc -l)"
check_eq "interrupted restore cleans transaction dirs" "0" "$restore_dirs"

# --------------------------------------------------------------------------- #
# 11. Management lock: concurrent operations rejected; stale lock recovered
# --------------------------------------------------------------------------- #
if command -v flock >/dev/null 2>&1; then
  (
    exec 9>"$FIXTURE/.tmb.lock"
    flock -n 9 || exit 1
    sleep 30
  ) &
  holder=$!
  sleep 0.3
  out="$(run_tmb backup create)" && code=0 || code=$?
  check_eq "locked backup create rejected" "1" "$code"
  check_contains "locked backup create reports lock" "already running" "$out"
  kill "$holder" 2>/dev/null || true
  wait "$holder" 2>/dev/null || true
else
  mkdir -p "$FIXTURE/.tmb-lock"
  sleep 60 &
  live_pid=$!
  printf '%s\n' "$live_pid" >"$FIXTURE/.tmb-lock/pid"
  out="$(run_tmb backup create)" && code=0 || code=$?
  check_eq "locked backup create rejected (mkdir lock)" "1" "$code"
  check_contains "locked backup create reports lock (mkdir lock)" "already running" "$out"
  kill "$live_pid" 2>/dev/null || true
  wait "$live_pid" 2>/dev/null || true
  rm -rf "$FIXTURE/.tmb-lock"
  mkdir -p "$FIXTURE/.tmb-lock"
  printf '999999\n' >"$FIXTURE/.tmb-lock/pid"
  out="$(run_tmb backup create)" && code=0 || code=$?
  check_eq "stale lock recovered, backup create succeeds" "0" "$code"
fi

# --------------------------------------------------------------------------- #
# 12. Migration export / import surface
# --------------------------------------------------------------------------- #
out="$(run_tmb migration export)" && code=0 || code=$?
check_eq "migration export exits 0" "0" "$code"
migration_archive="$(printf '%s\n' "$out" | sed -n 's/^Backup created: //p' | head -n 1)"
migration_manifest="$(tar -xzOf "$FIXTURE/$migration_archive" manifest.json)"
check_contains "migration manifest kind is migration" '"kind": "migration"' "$migration_manifest"

out="$(run_tmb migration import "$migration_archive")" && code=0 || code=$?
check_eq "migration import exits 0" "0" "$code"
check_contains "migration import accepts only migration archives" "Restore completed successfully" "$out"

out="$(run_tmb migration import "$archive")" && code=0 || code=$?
check_eq "migration import rejects operational archive" "1" "$code"
check_contains "migration import explains kind requirement" "migration import requires a migration export" "$out"

# --------------------------------------------------------------------------- #
# 13. Support bundle: sanitized, excludes secrets
# --------------------------------------------------------------------------- #
out="$(run_tmb bundle)" && code=0 || code=$?
check_eq "bundle exits 0" "0" "$code"
bundle="$(printf '%s\n' "$out" | sed -n 's/^Support bundle created: //p' | head -n 1)"
[[ -f "$FIXTURE/$bundle" ]]
pass "support bundle archive exists"
bundle_listing="$(tar -tzf "$FIXTURE/$bundle")"
check_contains "bundle contains version info" "bundle/version.txt" "$bundle_listing"
check_contains "bundle contains services state" "bundle/services.txt" "$bundle_listing"
check_not_contains "bundle excludes config.yaml" "config.yaml" "$bundle_listing"
check_not_contains "bundle excludes cookies" "cookies.txt" "$bundle_listing"
bundle_doctor="$(tar -xzOf "$FIXTURE/$bundle" bundle/doctor.txt 2>/dev/null || true)"
check_not_contains "bundle doctor output has no token" "TESTtoken" "$bundle_doctor"

# --------------------------------------------------------------------------- #
# 14. Secret redaction filter
# --------------------------------------------------------------------------- #
# shellcheck source=scripts/lib/common.sh
ROOT_DIR="$FIXTURE" source "$SCRIPT_DIR/../lib/common.sh"
redacted="$(printf 'INFO everything fine\nERROR bot_token=123456:TESTtoken\napi_hash=deadbeef\nhttps://user:pass@example.com/x\n' | sanitize_stream)"
check_contains "redaction keeps safe lines" "everything fine" "$redacted"
check_not_contains "redaction hides bot token" "TESTtoken" "$redacted"
check_not_contains "redaction hides api hash" "deadbeef" "$redacted"
check_contains "redaction masks URL credentials" "https://[redacted]@example.com/x" "$redacted"

# --------------------------------------------------------------------------- #
# 15. Uninstall: safe stages only
# --------------------------------------------------------------------------- #
out="$(run_tmb uninstall --yes)" && code=0 || code=$?
check_eq "uninstall (remove containers) exits 0" "0" "$code"
check_contains "uninstall reports containers removed" "Containers removed" "$out"
[[ -f "$FIXTURE/config.yaml" && -f "$FIXTURE/data/state/jobs.sqlite3" ]]
pass "uninstall preserves data and config"

# --------------------------------------------------------------------------- #
# 16. Config surface
# --------------------------------------------------------------------------- #
out="$(run_tmb config check)" && code=0 || code=$?
check_eq "config check exits 0" "0" "$code"
out="$(run_tmb config show)" && code=0 || code=$?
check_eq "config show exits 0" "0" "$code"

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
rm -rf -- "$TEST_ROOT"
[[ "$FAIL" -eq 0 ]]