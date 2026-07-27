# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.14.5
ARG TELEGRAM_BOT_API_REF=adfd7f6a8e990272851777eeb3ae0def4216f161

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv
FROM denoland/deno:bin-2.9.3 AS deno

FROM debian:bookworm-slim AS telegram-bot-api-build
ARG TELEGRAM_BOT_API_REF
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates cmake g++ git gperf libssl-dev make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --filter=blob:none --no-checkout https://github.com/tdlib/telegram-bot-api.git \
    && cd telegram-bot-api \
    && git checkout --detach "${TELEGRAM_BOT_API_REF}" \
    && git submodule update --init --recursive \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/telegram-bot-api \
    && cmake --build build --target install --parallel 2

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
COPY --from=telegram-bot-api-build \
    /opt/telegram-bot-api/bin/telegram-bot-api \
    /usr/local/bin/telegram-bot-api
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
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
