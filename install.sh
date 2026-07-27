#!/usr/bin/env bash
set -euo pipefail

RELEASE_ROOT="https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
ARCHIVE_NAME="telegram-media-downloader-bot.tar.gz"
DEFAULT_INSTALL_DIR="/opt/telegram-media-downloader-bot"
IMAGE_REPOSITORY="ghcr.io/hamedsanaei/telegram-media-downloader-bot"
TMB_BIN_DIR="${TMB_BIN_DIR:-/usr/local/bin}"

release_url() {
  if [[ -n "${TMB_RELEASE_TAG:-}" ]]; then
    printf '%s/download/%s/%s' "$RELEASE_ROOT" "$TMB_RELEASE_TAG" "$1"
  else
    printf '%s/latest/download/%s' "$RELEASE_ROOT" "$1"
  fi
}

install_verified_release() (
  local destination="$1"
  local temporary_directory staging_directory
  temporary_directory="$(mktemp -d)"
  staging_directory="$temporary_directory/extracted"
  trap 'rm -rf -- "$temporary_directory"' EXIT
  mkdir -p "$staging_directory"
  curl -fsSL "$(release_url "$ARCHIVE_NAME")" \
    -o "$temporary_directory/$ARCHIVE_NAME"
  curl -fsSL "$(release_url "$ARCHIVE_NAME.sha256")" \
    -o "$temporary_directory/$ARCHIVE_NAME.sha256"
  (
    cd "$temporary_directory"
    sha256sum --check --status "$ARCHIVE_NAME.sha256"
  )
  tar -xzf "$temporary_directory/$ARCHIVE_NAME" \
    -C "$staging_directory" --strip-components=1
  [[ -f "$staging_directory/pyproject.toml" ]]
  [[ -f "$staging_directory/docker-compose.yml" ]]
  local script
  for script in \
    install.sh \
    manage.sh \
    scripts/tmb.sh \
    scripts/build_release_archives.sh \
    scripts/tests/test_tmb_update.sh \
    scripts/tests/test_tmb_upgrade_integration.sh; do
    bash -n "$staging_directory/$script"
  done
  chmod 755 \
    "$staging_directory/install.sh" \
    "$staging_directory/manage.sh" \
    "$staging_directory/scripts/tmb.sh" \
    "$staging_directory/scripts/build_release_archives.sh" \
    "$staging_directory/scripts/tests/test_tmb_update.sh" \
    "$staging_directory/scripts/tests/test_tmb_upgrade_integration.sh"
  mkdir -p "$destination"
  cp -a "$staging_directory/." "$destination/"
  chmod 755 \
    "$destination/install.sh" \
    "$destination/manage.sh" \
    "$destination/scripts/tmb.sh" \
    "$destination/scripts/build_release_archives.sh" \
    "$destination/scripts/tests/test_tmb_update.sh" \
    "$destination/scripts/tests/test_tmb_upgrade_integration.sh"
)

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer supports Linux only." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  if ! command -v curl >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y curl
  fi
  echo "Installing Docker Engine from the official Docker installer..."
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker was installed. Sign out and back in, then run the installer again."
  exit 0
fi
docker compose version >/dev/null

read -r -p "Installation directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
sudo mkdir -p "$INSTALL_DIR"
sudo chown "$USER":"$(id -gn)" "$INSTALL_DIR"
install_verified_release "$INSTALL_DIR"
cd "$INSTALL_DIR"

cp -n config.example.yaml config.yaml
chmod 600 config.yaml
cp -n .env.example .env
RELEASE_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -n 1)"
if [[ -z "$RELEASE_VERSION" ]]; then
  echo "Unable to determine the verified release version." >&2
  exit 1
fi
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
