# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.14.5

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv
FROM denoland/deno:bin-2.9.3 AS deno

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DENO_DIR=/tmp/deno-cache \
    XDG_CACHE_HOME=/tmp/cache \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY plugins ./plugins
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/downloads /data/temp /data/state /data/cookies \
    && chown -R appuser:appuser /app /data

USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=3)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["telegram-media-bot", "bot", "--config", "/app/config.yaml"]
