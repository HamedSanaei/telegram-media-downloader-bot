param(
    [Parameter(Position = 0)]
    [string]$Command = "help",
    [Parameter(Position = 1)]
    [string]$Service = "",
    [Parameter(Position = 2)]
    [string]$ThirdArgument = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:APP_UID = if ($env:APP_UID) { $env:APP_UID } else { "10001" }
$env:APP_GID = if ($env:APP_GID) { $env:APP_GID } else { "10001" }


function Assert-LastExitCode {
    param([string]$Operation)
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE."
    }
}


function Require-Lock {
    if (-not (Test-Path "uv.lock")) {
        throw "uv.lock is missing; run '.\manage.ps1 lock', review it, and commit it first."
    }
}

function New-Lock {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv lock
        Assert-LastExitCode "uv lock"
    } else {
        docker run --rm --user "${env:APP_UID}:${env:APP_GID}" `
            -v "${PSScriptRoot}:/workspace" -w /workspace `
            ghcr.io/astral-sh/uv:0.11.31-python3.14-trixie-slim uv lock
        Assert-LastExitCode "Dockerized uv lock"
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
            Write-Host "Created .env with PYTHON_VERSION=3.14.5 for Docker builds."
        }
        New-Item -ItemType Directory -Force data/downloads, data/temp, data/state, data/cookies | Out-Null
    }
    "up" {
        Ensure-Config
        Require-Lock
        docker compose up -d --build
        Assert-LastExitCode "docker compose up"
    }
    "down" {
        docker compose down
        Assert-LastExitCode "docker compose down"
    }
    "restart" {
        Ensure-Config
        Require-Lock
        docker compose up -d --build --force-recreate
        Assert-LastExitCode "docker compose restart"
    }
    "logs" {
        if ($Service) { docker compose logs -f $Service } else { docker compose logs -f }
        Assert-LastExitCode "docker compose logs"
    }
    "status" {
        docker compose ps
        Assert-LastExitCode "docker compose ps"
    }
    "lock" { New-Lock }
    "check" {
        Require-Lock
        uv lock --check
        Assert-LastExitCode "uv lock --check"
        uv sync --frozen --group dev
        Assert-LastExitCode "uv sync"
        uv run python scripts/check_architecture.py
        Assert-LastExitCode "architecture check"
        uv run python scripts/check_text_integrity.py
        Assert-LastExitCode "text-integrity check"
        uv run python scripts/generate_file_manifest.py --check
        Assert-LastExitCode "source-manifest check"
        uv run pre-commit run detect-secrets --all-files
        Assert-LastExitCode "secret scan"
        uv run pip check
        Assert-LastExitCode "dependency check"
        uv run pip-audit --local --skip-editable --progress-spinner off
        Assert-LastExitCode "dependency vulnerability audit"
        uv run ruff check .
        Assert-LastExitCode "Ruff lint"
        uv run ruff format --check .
        Assert-LastExitCode "Ruff format"
        uv run mypy src tests
        Assert-LastExitCode "mypy"
        uv run pytest -m "not contract" --cov=telegram_media_bot --cov-report=term-missing
        Assert-LastExitCode "pytest"
        uv build
        Assert-LastExitCode "package build"
        Push-Location plugins/example_extractor
        try {
            uv lock --check
            Assert-LastExitCode "plugin uv lock --check"
            uv sync --frozen --group dev
            Assert-LastExitCode "plugin uv sync"
            uv run pytest -m "not contract"
            Assert-LastExitCode "plugin pytest"
        } finally {
            Pop-Location
        }
    }
    "config-check" {
        Ensure-Config
        uv run telegram-media-bot config-check --config config.yaml
        Assert-LastExitCode "configuration check"
    }
    "doctor" {
        Ensure-Config
        uv run telegram-media-bot doctor --config config.yaml
        Assert-LastExitCode "runtime doctor"
    }
    "upgrade-ytdlp" {
        Require-Lock
        uv run python scripts/upgrade_ytdlp.py
        Assert-LastExitCode "yt-dlp upgrade"
    }
    "canary-report" {
        if (-not $Service -or -not $ThirdArgument) {
            throw "Usage: .\manage.ps1 canary-report BASELINE.json CANARY.json"
        }
        uv run python scripts/compare_canary.py $Service $ThirdArgument
        Assert-LastExitCode "canary comparison"
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
          upgrade-ytdlp, canary-report, clean
"@
    }
}
