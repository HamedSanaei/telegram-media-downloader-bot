# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.14.5
# Immutable Telegram Local Bot API artifact, built once by the manual-only
# Dockerfile.telegram-bot-api workflow and published to GHCR. The normal
# application build never compiles Telegram Bot API or TDLib from source.
ARG TELEGRAM_BOT_API_IMAGE=ghcr.io/hamedsanaei/telegram-bot-api@sha256:36f4813c3feeb09a09918caa8617d8e217784019065298c6ad1bca2ca2dea826

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv
FROM denoland/deno:bin-2.9.3 AS deno
FROM ${TELEGRAM_BOT_API_IMAGE} AS telegram-bot-api

FROM python:${PYTHON_VERSION}-slim AS runtime
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DENO_DIR=/tmp/deno-cache \
    XDG_CACHE_HOME=/tmp/cache \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg 7zip tini \
    && rm -rf /var/lib/apt/lists/* \
    && if command -v 7zz >/dev/null 2>&1; then \
         ln -sfn "$(command -v 7zz)" /usr/local/bin/7z; \
       elif command -v 7z >/dev/null 2>&1; then \
         ln -sfn "$(command -v 7z)" /usr/local/bin/7zz; \
       else \
         echo "7-Zip executable is missing" >&2; exit 1; \
       fi \
    && command -v 7zz >/dev/null \
    && command -v 7z >/dev/null

COPY --from=uv /uv /uvx /bin/
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=telegram-bot-api \
    /telegram-bot-api \
    /usr/local/bin/telegram-bot-api
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY docs/THIRD_PARTY_NOTICES.md ./docs/THIRD_PARTY_NOTICES.md
COPY docs/licenses ./docs/licenses
COPY plugins ./plugins
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

RUN groupadd --gid "${APP_GID}" appuser \
    && useradd --create-home --uid "${APP_UID}" --gid "${APP_GID}" appuser \
    && mkdir -p /data/downloads /data/temp /data/state /data/cookies \
    && chown -R appuser:appuser /app /data

USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["telegram-media-bot", "bot", "--config", "/app/config.yaml"]
