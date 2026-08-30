#!/usr/bin/env bash
set -euo pipefail

RELEASE_ROOT="https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
ARCHIVE_NAME="telegram-media-downloader-bot.tar.gz"
DEFAULT_INSTALL_DIR="/opt/telegram-media-downloader-bot"
IMAGE_REPOSITORY="ghcr.io/hamedsanaei/telegram-media-downloader-bot"
TMB_BIN_DIR="${TMB_BIN_DIR:-/usr/local/bin}"
# Standalone bootstrap snapshot; tests enforce parity with release-policy.json.
readonly -a BLOCKED_RELEASE_VERSIONS=("1.3.7")
INSTALL_TEMPORARY_DIRECTORY=""
INSTALL_STAGING_DIRECTORY=""
INSTALL_RELEASE_VERSION=""

assert_release_allowed() {
  local version="${1:-}" normalized blocked
  [[ -n "$version" ]] || return 0
  normalized="${version#v}"
  for blocked in "${BLOCKED_RELEASE_VERSIONS[@]}"; do
    if [[ "$normalized" == "$blocked" ]]; then
      echo "Release $normalized is blocked because it contains a critical Telegram durable-polling crash bug. Use v1.3.8 or newer instead." >&2
      return 1
    fi
  done
}

cleanup_prepared_install_release() {
  if [[ -n "$INSTALL_TEMPORARY_DIRECTORY" ]]; then
    rm -rf -- "$INSTALL_TEMPORARY_DIRECTORY"
  fi
}

release_url() {
  if [[ -n "${TMB_RELEASE_TAG:-}" ]]; then
    printf '%s/download/%s/%s' "$RELEASE_ROOT" "$TMB_RELEASE_TAG" "$1"
  else
    printf '%s/latest/download/%s' "$RELEASE_ROOT" "$1"
  fi
}

prepare_verified_release() {
  INSTALL_TEMPORARY_DIRECTORY="$(mktemp -d)"
  INSTALL_STAGING_DIRECTORY="$INSTALL_TEMPORARY_DIRECTORY/extracted"
  trap cleanup_prepared_install_release EXIT
  mkdir -p "$INSTALL_STAGING_DIRECTORY"
  curl -fsSL "$(release_url "$ARCHIVE_NAME")" \
    -o "$INSTALL_TEMPORARY_DIRECTORY/$ARCHIVE_NAME"
  curl -fsSL "$(release_url "$ARCHIVE_NAME.sha256")" \
    -o "$INSTALL_TEMPORARY_DIRECTORY/$ARCHIVE_NAME.sha256"
  (
    cd "$INSTALL_TEMPORARY_DIRECTORY"
    sha256sum --check --status "$ARCHIVE_NAME.sha256"
  )
  tar -xzf "$INSTALL_TEMPORARY_DIRECTORY/$ARCHIVE_NAME" \
    -C "$INSTALL_STAGING_DIRECTORY" --strip-components=1
  [[ -f "$INSTALL_STAGING_DIRECTORY/pyproject.toml" ]]
  INSTALL_RELEASE_VERSION="$(
    sed -n 's/^version = "\([^"]*\)"/\1/p' \
      "$INSTALL_STAGING_DIRECTORY/pyproject.toml" | head -n 1
  )"
  [[ -n "$INSTALL_RELEASE_VERSION" ]] || {
    echo "Unable to determine the verified release version." >&2
    return 1
  }
  assert_release_allowed "$INSTALL_RELEASE_VERSION"
  [[ -f "$INSTALL_STAGING_DIRECTORY/docker-compose.yml" ]]
  local script
  for script in \
    install.sh \
    manage.sh \
    scripts/tmb.sh \
    scripts/build_release_archives.sh \
    scripts/tests/test_tmb_update.sh \
    scripts/tests/test_tmb_upgrade_integration.sh \
    scripts/tests/test_local_api_readiness.sh; do
    bash -n "$INSTALL_STAGING_DIRECTORY/$script"
  done
  chmod 755 \
    "$INSTALL_STAGING_DIRECTORY/install.sh" \
    "$INSTALL_STAGING_DIRECTORY/manage.sh" \
    "$INSTALL_STAGING_DIRECTORY/scripts/tmb.sh" \
    "$INSTALL_STAGING_DIRECTORY/scripts/build_release_archives.sh" \
    "$INSTALL_STAGING_DIRECTORY/scripts/tests/test_tmb_update.sh" \
    "$INSTALL_STAGING_DIRECTORY/scripts/tests/test_tmb_upgrade_integration.sh" \
    "$INSTALL_STAGING_DIRECTORY/scripts/tests/test_local_api_readiness.sh"
}

install_prepared_release() {
  local destination="$1"
  sudo mkdir -p "$destination"
  sudo chown "$USER":"$(id -gn)" "$destination"
  cp -a "$INSTALL_STAGING_DIRECTORY/." "$destination/"
  chmod 755 \
    "$destination/install.sh" \
    "$destination/manage.sh" \
    "$destination/scripts/tmb.sh" \
    "$destination/scripts/build_release_archives.sh" \
    "$destination/scripts/tests/test_tmb_update.sh" \
    "$destination/scripts/tests/test_tmb_upgrade_integration.sh" \
    "$destination/scripts/tests/test_local_api_readiness.sh"
}

assert_release_allowed "${TMB_RELEASE_TAG:-}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer supports Linux only." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y curl
fi
prepare_verified_release

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker Engine from the official Docker installer..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker was installed. Sign out and back in, then run the installer again."
  exit 0
fi
docker compose version >/dev/null

read -r -p "Installation directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
install_prepared_release "$INSTALL_DIR"
cd "$INSTALL_DIR"

cp -n config.example.yaml config.yaml
chmod 600 config.yaml
cp -n .env.example .env
RELEASE_VERSION="$INSTALL_RELEASE_VERSION"
DEFAULT_IMAGE="$IMAGE_REPOSITORY:$RELEASE_VERSION"
if grep -q '^TMB_IMAGE=' .env; then
  sed -i "s|^TMB_IMAGE=.*|TMB_IMAGE=$DEFAULT_IMAGE|" .env
else
  echo "TMB_IMAGE=$DEFAULT_IMAGE" >> .env
fi
grep -q '^COMPOSE_PROFILES=' .env || echo "COMPOSE_PROFILES=local-api" >> .env
grep -q '^APP_UID=' .env || echo "APP_UID=$(id -u)" >> .env
grep -q '^APP_GID=' .env || echo "APP_GID=$(id -g)" >> .env
mkdir -p data/downloads data/temp data/state data/cookies data/telegram-bot-api

docker pull "$DEFAULT_IMAGE"
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  -v "$INSTALL_DIR:/workspace" -w /workspace \
  "$DEFAULT_IMAGE" telegram-media-bot configure --config /workspace/config.yaml

docker compose --profile local-api up -d local-api
read -r -p "Migrate this bot from Cloud Bot API to Local Bot API now? Type MIGRATE: " answer
if [[ "$answer" == "MIGRATE" ]]; then
  docker compose --profile local-api run --rm --no-deps bot \
    telegram-media-bot local-api --config /app/config.yaml migrate-to-local --yes
fi
docker compose --profile local-api up -d --no-build

chmod 755 scripts/tmb.sh
sudo mkdir -p "$TMB_BIN_DIR"
sudo ln -sfn "$INSTALL_DIR/scripts/tmb.sh" "$TMB_BIN_DIR/tmb"
command -v tmb >/dev/null 2>&1 || {
  echo "tmb was installed at $TMB_BIN_DIR/tmb; add that directory to PATH." >&2
}
test -x "$(readlink -f "$(command -v tmb)")"
tmb status >/dev/null
echo "Installation completed. Use: tmb status, tmb logs, tmb doctor"
