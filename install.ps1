$ErrorActionPreference = "Stop"
$ReleaseRoot = "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
$ArchiveName = "telegram-media-downloader-bot.zip"
$ImageRepository = "ghcr.io/hamedsanaei/telegram-media-downloader-bot"
$InstallDirectory = Join-Path $env:LOCALAPPDATA "TelegramMediaDownloaderBot"
$ResumeState = Join-Path $env:LOCALAPPDATA "TelegramMediaDownloaderBot.install-state"
# Standalone bootstrap snapshot; tests enforce parity with release-policy.json.
$BlockedReleaseVersions = @("1.3.7")

function Assert-ReleaseAllowed {
    param([string]$Version)
    if (-not $Version) { return }
    $Normalized = if ($Version.StartsWith("v", [System.StringComparison]::Ordinal)) {
        $Version.Substring(1)
    }
    else {
        $Version
    }
    foreach ($Blocked in $BlockedReleaseVersions) {
        if ([string]::Equals($Normalized, $Blocked, [System.StringComparison]::Ordinal)) {
            throw "Release $Normalized is blocked because it contains a critical Telegram durable-polling crash bug. Use v1.3.8 or newer instead."
        }
    }
}

function Get-ReleaseUrl {
    param([string]$AssetName)
    if ($env:TMB_RELEASE_TAG) {
        return "$ReleaseRoot/download/$($env:TMB_RELEASE_TAG)/$AssetName"
    }
    return "$ReleaseRoot/latest/download/$AssetName"
}

function Get-VerifiedRelease {
    $TemporaryDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("tmb-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force $TemporaryDirectory | Out-Null
    try {
        $ArchivePath = Join-Path $TemporaryDirectory $ArchiveName
        $ChecksumPath = "$ArchivePath.sha256"
        Invoke-WebRequest -UseBasicParsing (Get-ReleaseUrl $ArchiveName) -OutFile $ArchivePath
        Invoke-WebRequest -UseBasicParsing (Get-ReleaseUrl "$ArchiveName.sha256") -OutFile $ChecksumPath
        $ExpectedHash = ((Get-Content -Raw -Encoding utf8 $ChecksumPath).Trim() -split "\s+")[0]
        $ActualHash = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash
        if ($ActualHash -ine $ExpectedHash) {
            throw "Release checksum verification failed."
        }
        $ExtractedDirectory = Join-Path $TemporaryDirectory "extracted"
        Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractedDirectory -Force
        $Payload = Join-Path $ExtractedDirectory "telegram-media-downloader-bot"
        $VersionMatch = Select-String -Path (Join-Path $Payload "pyproject.toml") `
            -Pattern '^version = "([^"]+)"$'
        if (-not $VersionMatch) { throw "Unable to determine verified release version." }
        $Version = $VersionMatch.Matches[0].Groups[1].Value
        Assert-ReleaseAllowed $Version
        if (-not (Test-Path -LiteralPath (Join-Path $Payload "docker-compose.yml"))) {
            throw "Verified release is missing docker-compose.yml."
        }
        return [pscustomobject]@{
            TemporaryDirectory = $TemporaryDirectory
            Payload = $Payload
            Version = $Version
        }
    }
    catch {
        Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Install-PreparedRelease {
    param(
        [string]$Destination,
        [string]$Payload
    )
    New-Item -ItemType Directory -Force $Destination | Out-Null
    Get-ChildItem -LiteralPath $Payload -Force |
        Copy-Item -Destination $Destination -Recurse -Force
}

Assert-ReleaseAllowed $env:TMB_RELEASE_TAG
$Release = Get-VerifiedRelease
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "Docker Desktop is required. Install Docker Desktop with WSL2 and rerun this command."
        }
        winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements
        New-Item -ItemType File -Force $ResumeState | Out-Null
        Write-Host "Docker Desktop was installed. Reboot if requested, start Docker Desktop, then rerun the same command."
        exit 0
    }
    docker compose version | Out-Null
    docker info | Out-Null

    New-Item -ItemType Directory -Force (Split-Path -Parent $InstallDirectory) | Out-Null
    Install-PreparedRelease -Destination $InstallDirectory -Payload $Release.Payload
    Set-Location $InstallDirectory
    if (-not (Test-Path "config.yaml")) { Copy-Item "config.example.yaml" "config.yaml" }
    if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
    $Image = "$ImageRepository`:$($Release.Version)"
    $EnvironmentValues = @{
        "TMB_IMAGE" = $Image
        "COMPOSE_PROFILES" = "local-api"
        "APP_UID" = "10001"
        "APP_GID" = "10001"
    }
    foreach ($Entry in $EnvironmentValues.GetEnumerator()) {
        if (-not (Select-String -Path ".env" -Pattern "^$($Entry.Key)=" -Quiet)) {
            Add-Content -Encoding utf8 ".env" "$($Entry.Key)=$($Entry.Value)"
        }
    }
    New-Item -ItemType Directory -Force data/downloads, data/temp, data/state, data/cookies, data/telegram-bot-api | Out-Null

    docker pull $Image
    docker run --rm -it -v "${InstallDirectory}:/workspace" -w /workspace $Image telegram-media-bot configure --config /workspace/config.yaml
    if ($LASTEXITCODE -ne 0) { throw "Interactive configuration failed." }

    docker compose --profile local-api up -d local-api
    if ((Read-Host "Type MIGRATE to move the bot from Cloud API to Local API") -eq "MIGRATE") {
        docker compose --profile local-api run --rm --no-deps bot telegram-media-bot local-api --config /app/config.yaml migrate-to-local --yes
    }
    docker compose --profile local-api up -d --no-build

    $BinDirectory = Join-Path $env:USERPROFILE "bin"
    New-Item -ItemType Directory -Force $BinDirectory | Out-Null
    $CommandFile = Join-Path $BinDirectory "tmb.cmd"
    "@powershell -NoProfile -ExecutionPolicy Bypass -File `"$InstallDirectory\scripts\tmb.ps1`" %*" | Set-Content -Encoding ascii $CommandFile
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($UserPath -split ";") -notcontains $BinDirectory) {
        [Environment]::SetEnvironmentVariable("Path", "$UserPath;$BinDirectory", "User")
    }
    Remove-Item $ResumeState -Force -ErrorAction SilentlyContinue
    Write-Host "Installation completed. Open a new terminal and run: tmb status"
}
finally {
    if ($Release -and (Test-Path -LiteralPath $Release.TemporaryDirectory)) {
        Remove-Item -LiteralPath $Release.TemporaryDirectory `
            -Recurse -Force -ErrorAction SilentlyContinue
    }
}
