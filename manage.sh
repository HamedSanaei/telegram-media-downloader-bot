#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

UV_LOCK_IMAGE="ghcr.io/astral-sh/uv:0.11.31-python3.14-trixie-slim"

require_lock() {
  if [[ -f uv.lock ]]; then
    return
  fi
  echo "uv.lock is missing; run './manage.sh lock', review it, and commit it first." >&2
  exit 1
}

generate_lock() {
  if command -v uv >/dev/null 2>&1; then
    uv lock
  else
    require_command docker
    docker run --rm \
      --user "${APP_UID:-1000}:${APP_GID:-1000}" \
      -v "$ROOT_DIR:/workspace" \
      -w /workspace \
      "$UV_LOCK_IMAGE" uv lock
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

ensure_config() {
  if [[ ! -f config.yaml ]]; then
    echo "config.yaml does not exist. Run './manage.sh init' first." >&2
    exit 1
  fi
}

case "${1:-help}" in
  init)
    if [[ -f config.yaml ]]; then
      echo "config.yaml already exists; it was not overwritten."
    else
      cp config.example.yaml config.yaml
      chmod 600 config.yaml 2>/dev/null || true
      echo "Created config.yaml. Set telegram.bot_token before starting."
    fi
    if [[ ! -f .env ]]; then
      cp .env.example .env
      echo "Created .env with PYTHON_VERSION=3.14.5 for Docker builds."
    fi
    mkdir -p data/downloads data/temp data/state data/cookies
    ;;

  up)
    require_command docker
    ensure_config
    export APP_UID="${APP_UID:-$(id -u)}"
    export APP_GID="${APP_GID:-$(id -g)}"
    require_lock
    docker compose up -d --build
    ;;

  down)
    require_command docker
    docker compose down
    ;;

  restart)
    require_command docker
    ensure_config
    export APP_UID="${APP_UID:-$(id -u)}"
    export APP_GID="${APP_GID:-$(id -g)}"
    require_lock
    docker compose up -d --build --force-recreate
    ;;

  logs)
    require_command docker
    if [[ -n "${2:-}" ]]; then
      docker compose logs -f "$2"
    else
      docker compose logs -f
    fi
    ;;

  status)
    require_command docker
    docker compose ps
    ;;

  lock)
    export APP_UID="${APP_UID:-$(id -u)}"
    export APP_GID="${APP_GID:-$(id -g)}"
    generate_lock
    ;;

  check)
    require_command uv
    require_lock
    uv lock --check
    uv sync --frozen --group dev
    uv run python scripts/check_architecture.py
    uv run python scripts/check_text_integrity.py
    uv run python scripts/generate_file_manifest.py --check
    uv run pre-commit run detect-secrets --all-files
    uv run pip check
    uv run pip-audit --local --skip-editable --progress-spinner off
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src tests
    uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
    uv build
    (cd plugins/example_extractor && uv lock --check && uv sync --frozen --group dev && uv run pytest -m "not contract")
    ;;

  config-check)
    require_command uv
    ensure_config
    uv run telegram-media-bot config-check --config config.yaml
    ;;

  doctor)
    require_command uv
    ensure_config
    uv run telegram-media-bot doctor --config config.yaml
    ;;

  local-api)
    require_command uv
    ensure_config
    if [[ -z "${2:-}" ]]; then
      echo "Usage: ./manage.sh local-api status|start|stop|migrate-to-local|migrate-to-cloud [--yes]" >&2
      exit 2
    fi
    local_api_args=(run telegram-media-bot local-api --config config.yaml "$2")
    if [[ -n "${3:-}" ]]; then
      local_api_args+=("$3")
    fi
    uv "${local_api_args[@]}"
    ;;

  upgrade-ytdlp)
    require_command uv
    require_lock
    uv run python scripts/upgrade_ytdlp.py
    ;;

  canary-report)
    require_command uv
    if [[ -z "${2:-}" || -z "${3:-}" ]]; then
      echo "Usage: ./manage.sh canary-report BASELINE.json CANARY.json" >&2
      exit 2
    fi
    uv run python scripts/compare_canary.py "$2" "$3"
    ;;

  clean)
    mkdir -p data/downloads data/temp
    find data/downloads data/temp -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    echo "Runtime download and temporary directories cleaned."
    ;;

  help|*)
    cat <<'EOF'
Usage: ./manage.sh COMMAND

Commands:
  init             Create local config.yaml and runtime directories
  up               Build and start bot, worker, and Redis
  down             Stop the stack
  restart          Rebuild and recreate the stack
  logs [service]   Follow all logs or one service
  status           Show service status
  lock             Generate uv.lock if it is missing
  check            Run lock, lint, format, type, test, and coverage gates
  config-check     Validate local configuration
  doctor           Check configuration and local runtime dependencies
  local-api ACTION  Status, lifecycle, and explicit migration commands
  upgrade-ytdlp    Update only the yt-dlp lock entry and run adapter tests
  canary-report    Compare baseline and canary failure-rate snapshots
  clean            Delete local downloaded and temporary files
EOF
    ;;
esac
