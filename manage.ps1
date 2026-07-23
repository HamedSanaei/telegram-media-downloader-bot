param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [Parameter(Position = 1)]
    [string]$Service = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:APP_UID = if ($env:APP_UID) { $env:APP_UID } else { "10001" }
$env:APP_GID = if ($env:APP_GID) { $env:APP_GID } else { "10001" }


function Ensure-Lock {
    if (Test-Path "uv.lock") { return }
    Write-Host "uv.lock is missing; generating it once before the build..."
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv lock
    } else {
        docker run --rm --user "${env:APP_UID}:${env:APP_GID}" `
            -v "${PSScriptRoot}:/workspace" -w /workspace `
            ghcr.io/astral-sh/uv:0.11.31-python3.14-trixie-slim uv lock
    }
}

function Ensure-Config {
    if (-not (Test-Path "config.yaml")) {
        throw "config.yaml does not exist. Run '.\manage.ps1 init' first."
    }
}

switch ($Command) {
    "init" {
        if (Test-Path "config.yaml") {
            Write-Host "config.yaml already exists; it was not overwritten."
        } else {
            Copy-Item "config.example.yaml" "config.yaml"
            Write-Host "Created config.yaml. Set telegram.bot_token before starting."
        }
        if (-not (Test-Path ".env")) {
            Copy-Item ".env.example" ".env"
            Write-Host "Created .env with PYTHON_VERSION=3.14 for Docker builds."
        }
        New-Item -ItemType Directory -Force data/downloads, data/temp, data/state, data/cookies | Out-Null
    }
    "up" {
        Ensure-Config
        Ensure-Lock
        docker compose up -d --build
    }
    "down" { docker compose down }
    "restart" {
        Ensure-Config
        Ensure-Lock
        docker compose up -d --build --force-recreate
    }
    "logs" {
        if ($Service) { docker compose logs -f $Service } else { docker compose logs -f }
    }
    "status" { docker compose ps }
    "lock" { Ensure-Lock }
    "check" {
        Ensure-Lock
        uv lock --check
        uv sync --frozen --group dev
        uv run python scripts/check_architecture.py
        uv run python scripts/check_text_integrity.py
        uv run ruff check .
        uv run ruff format --check .
        uv run mypy src tests
        uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
    }
    "config-check" {
        Ensure-Config
        uv run telegram-media-bot config-check --config config.yaml
    }
    "doctor" {
        Ensure-Config
        uv run telegram-media-bot doctor --config config.yaml
    }
    "upgrade-ytdlp" {
        Ensure-Lock
        uv lock --upgrade-package yt-dlp
        uv sync --frozen --group dev
        uv run pytest tests/unit/infrastructure/ytdlp -m "not contract"
        Write-Host "yt-dlp lock entry updated and adapter unit tests passed. Review the diff, run '.\manage.ps1 check', then rebuild."
    }
    "clean" {
        New-Item -ItemType Directory -Force data/downloads, data/temp | Out-Null
        Remove-Item -Recurse -Force data/downloads/*, data/temp/* -ErrorAction SilentlyContinue
        Write-Host "Runtime download and temporary directories cleaned."
    }
    default {
        Write-Host @"
Usage: .\manage.ps1 COMMAND

Commands: init, lock, up, down, restart, logs, status, check, config-check, doctor,
          upgrade-ytdlp, clean
"@
    }
}
