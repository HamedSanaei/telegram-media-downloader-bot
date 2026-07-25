param(
    [Parameter(Position = 0)]
    [string]$Command = "",
    [Parameter(Position = 1)]
    [string]$Service = ""
)

$ErrorActionPreference = "Stop"
$RootDirectory = Split-Path -Parent $PSScriptRoot
$ReleaseRoot = "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases"
$ArchiveName = "telegram-media-downloader-bot.zip"
$ImageRepository = "ghcr.io/hamedsanaei/telegram-media-downloader-bot"
Set-Location $RootDirectory

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose --project-directory $RootDirectory --profile local-api @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose command failed." }
}

function New-TmbBackup {
    $BackupDirectory = Join-Path $RootDirectory "backups"
    New-Item -ItemType Directory -Force $BackupDirectory | Out-Null
    $Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $Archive = Join-Path $BackupDirectory "tmb-$Stamp.zip"
    $BackupItems = @("config.yaml", ".env")
    foreach ($Item in @("data/state", "data/cookies", "data/telegram-bot-api")) {
        if (Test-Path -LiteralPath $Item) { $BackupItems += $Item }
    }
    Compress-Archive -Path $BackupItems -DestinationPath $Archive -Force
    Write-Host "Backup created: $Archive"
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
        if (-not (Test-Path -LiteralPath (Join-Path $Payload "docker-compose.yml"))) {
            throw "Verified release is missing docker-compose.yml."
        }
        return [pscustomobject]@{
            TemporaryDirectory = $TemporaryDirectory
            Payload = $Payload
            Version = $VersionMatch.Matches[0].Groups[1].Value
        }
    }
    catch {
        Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force `
            -ErrorAction SilentlyContinue
        throw
    }
}

function Set-ConfiguredImage {
    param([string]$Image)
    $EnvironmentPath = Join-Path $RootDirectory ".env"
    $Content = Get-Content -Raw -Encoding utf8 $EnvironmentPath
    if ($Content -match "(?m)^TMB_IMAGE=") {
        $Content = $Content -replace "(?m)^TMB_IMAGE=.*$", "TMB_IMAGE=$Image"
    }
    else {
        $Content = $Content.TrimEnd() + [Environment]::NewLine + "TMB_IMAGE=$Image" +
            [Environment]::NewLine
    }
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($EnvironmentPath, $Content, $Utf8NoBom)
}

function Get-RunningApplicationServices {
    $Running = @(Invoke-Compose @("ps", "--services", "--filter", "status=running"))
    return @($Running | Where-Object { $_ -in @("bot", "worker", "local-api") })
}

function Start-TmbServices {
    param(
        [string[]]$Services,
        [switch]$ForceRecreate
    )
    if ($Services.Count -eq 0) { return }
    $Arguments = @("up", "-d", "--no-build")
    if ($ForceRecreate) { $Arguments += "--force-recreate" }
    $Arguments += $Services
    Invoke-Compose $Arguments
}

function Get-ConfiguredImage {
    if ($env:TMB_IMAGE) { return $env:TMB_IMAGE }
    $EnvironmentPath = Join-Path $RootDirectory ".env"
    if (Test-Path -LiteralPath $EnvironmentPath) {
        $Match = Select-String -Path $EnvironmentPath -Pattern '^TMB_IMAGE=(.+)$' |
            Select-Object -First 1
        if ($Match) { return $Match.Matches[0].Groups[1].Value }
    }
    return "ghcr.io/hamedsanaei/telegram-media-downloader-bot:latest"
}

if (-not $Command) {
    Write-Host "1 Start`n2 Stop`n3 Restart`n4 Status`n5 Logs`n6 Doctor`n7 Config`n8 Update`n9 Backup`n0 Exit"
    $Choice = Read-Host "Select"
    $Command = @{
        "1" = "start"; "2" = "stop"; "3" = "restart"; "4" = "status"
        "5" = "logs"; "6" = "doctor"; "7" = "config"; "8" = "update"
        "9" = "backup"; "0" = "exit"
    }[$Choice]
}

switch ($Command) {
    "start" { Invoke-Compose @("up", "-d", "--no-build") }
    "stop" { Invoke-Compose @("down") }
    "restart" { Invoke-Compose @("up", "-d", "--no-build", "--force-recreate") }
    "status" { Invoke-Compose @("ps") }
    "logs" {
        $Arguments = @("logs", "-f")
        if ($Service) { $Arguments += $Service }
        Invoke-Compose $Arguments
    }
    "doctor" {
        Invoke-Compose @("run", "--rm", "--no-deps", "worker", "telegram-media-bot", "doctor", "--config", "/app/config.yaml")
    }
    "config" {
        $Image = Get-ConfiguredImage
        & docker run --rm -it -v "${RootDirectory}:/workspace" -w /workspace $Image telegram-media-bot configure --config /workspace/config.yaml
        if ($LASTEXITCODE -ne 0) { throw "Configuration failed." }
    }
    "update" {
        $PreviousImage = Get-ConfiguredImage
        $PreviousServices = @(Get-RunningApplicationServices)
        if ($PreviousServices.Count -gt 0) {
            Invoke-Compose (@("stop", "-t", "45") + $PreviousServices)
        }
        $Release = $null
        try {
            New-TmbBackup
            $Release = Get-VerifiedRelease
            Set-ConfiguredImage "$ImageRepository`:$($Release.Version)"
            Invoke-Compose @("pull")
            Get-ChildItem -LiteralPath $Release.Payload -Force |
                Copy-Item -Destination $RootDirectory -Recurse -Force
            Start-TmbServices -Services $PreviousServices -ForceRecreate
        }
        catch {
            Set-ConfiguredImage $PreviousImage
            Write-Warning "Update failed; restoring the prior image and restarting the stack."
            Start-TmbServices -Services $PreviousServices
            throw
        }
        finally {
            if ($Release -and (Test-Path -LiteralPath $Release.TemporaryDirectory)) {
                Remove-Item -LiteralPath $Release.TemporaryDirectory `
                    -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    "backup" { New-TmbBackup }
    "uninstall" {
        Invoke-Compose @("down")
        if ((Read-Host "Type DELETE to remove config and data") -eq "DELETE") {
            Remove-Item -LiteralPath (Join-Path $RootDirectory "data") -Recurse -Force
            Remove-Item -LiteralPath (Join-Path $RootDirectory "config.yaml") -Force
        }
    }
    "exit" { exit 0 }
    default { throw "Usage: tmb start|stop|restart|status|logs [service]|doctor|config|update|backup|uninstall" }
}
