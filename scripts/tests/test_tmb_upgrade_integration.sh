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
TEST_ROOT="$(mktemp -d)"
INSTALL_ROOT="$TEST_ROOT/installation"
ASSET_ROOT="$TEST_ROOT/releases/download/v${RELEASE_VERSION}"
BIN_ROOT="$TEST_ROOT/bin"
REGISTRY_NAME="tmb-upgrade-registry-$$"
REGISTRY_PORT="${TMB_TEST_REGISTRY_PORT:-5055}"
IMAGE_REPOSITORY="localhost:${REGISTRY_PORT}/telegram-media-downloader-bot"

cleanup() {
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api down \
    --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  sudo rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

for command in bash curl docker git python sha256sum tar; do
  command -v "$command" >/dev/null
done

mkdir -p "$INSTALL_ROOT" "$ASSET_ROOT" "$BIN_ROOT"
bash "$SOURCE_ROOT/scripts/build_release_archives.sh" HEAD "$ASSET_ROOT"
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
COMPOSE_PROFILES=local-api
APP_UID=10001
APP_GID=10001
TMB_WORKER_CPUS=1.5
EOF
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
printf 'local-api-sentinel' >"$INSTALL_ROOT/data/telegram-bot-api/state.bin"
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

# Reproduce unusable production ownership/modes. The release tar deliberately carries a symlinked
# executable updater so v1.0.2 cannot truncate its own executing inode during `cp -a`.
sudo chown -R 0:0 "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups"
sudo find "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups" -type d -exec chmod 500 {} +
sudo find "$INSTALL_ROOT/data" "$INSTALL_ROOT/backups" -type f -exec chmod 400 {} +
if [[ "$USE_RELEASE_UPDATER_ASSET" == "1" ]]; then
  sudo chown 10001:10001 \
    "$INSTALL_ROOT/data" \
    "$INSTALL_ROOT/data/cookies" \
    "$INSTALL_ROOT/data/cookies/cookies.txt"
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

sudo env \
  "PATH=$BIN_ROOT:$PATH" \
  "TMB_BIN_DIR=$BIN_ROOT" \
  "TMB_RELEASE_TAG=v${RELEASE_VERSION}" \
  "TMB_IMAGE_REPOSITORY=$IMAGE_REPOSITORY" \
  "TMB_TEST_ASSET_ROOT=$ASSET_ROOT" \
  "TMB_ROOT_DIR=$INSTALL_ROOT" \
  bash "$UPDATER_PATH" update

PATH="$BIN_ROOT:$PATH" command -v tmb >/dev/null
test -x "$(readlink -f "$(PATH="$BIN_ROOT:$PATH" command -v tmb)")"
bash -n "$INSTALL_ROOT/scripts/tmb.sh"
sudo env "PATH=$BIN_ROOT:$PATH" "TMB_ROOT_DIR=$INSTALL_ROOT" tmb status >/dev/null
sudo grep -q "^TMB_IMAGE=.*:${RELEASE_VERSION}$" "$INSTALL_ROOT/.env"
sudo grep -q 'CHANGE_ME' "$INSTALL_ROOT/config.yaml"
! sudo grep -q '^gallery_dl:' "$INSTALL_ROOT/config.yaml"
sudo grep -q '^cookie-sentinel$' "$INSTALL_ROOT/data/cookies/cookies.txt"
sudo grep -q '^download-sentinel$' "$INSTALL_ROOT/data/downloads/existing.bin"
sudo grep -q '^local-api-sentinel$' "$INSTALL_ROOT/data/telegram-bot-api/state.bin"
test "$(sudo sha256sum "$INSTALL_ROOT/config.yaml" | cut -d' ' -f1)" = "$CONFIG_HASH_BEFORE"
test "$(sudo sha256sum "$INSTALL_ROOT/data/cookies/cookies.txt" | cut -d' ' -f1)" = \
  "$COOKIE_HASH_BEFORE"
grep -q "^version = \"${RELEASE_VERSION}\"$" "$INSTALL_ROOT/pyproject.toml"

DOCTOR_OUTPUT="$(
  docker compose --project-directory "$INSTALL_ROOT" --profile local-api run --rm --no-deps \
    worker telegram-media-bot doctor --config /app/config.yaml
)"
grep -Eq '^OK +gallery_dl_cookie_instagram$' <<<"$DOCTOR_OUTPUT"

docker run --rm --user 10001:10001 --entrypoint python \
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
