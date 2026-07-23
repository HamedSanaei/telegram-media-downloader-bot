# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.31 AS uv

ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    APP_CONFIG_PATH=/app/config.yaml

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/downloads /data/temp /data/state /data/cookies \
    && chown -R appuser:appuser /app /data

USER appuser
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["telegram-media-bot", "bot"]
