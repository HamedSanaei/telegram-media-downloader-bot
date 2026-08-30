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
# Standalone bootstrap snapshot; tests enforce parity with release-policy.json.
$BlockedReleaseVersions = @("1.3.7")
Set-Location $RootDirectory

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

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose --project-directory $RootDirectory --profile local-api @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose command failed." }
}

function Invoke-Docker {
    param([string[]]$Arguments)
    $Output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed." }
    return $Output
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

function Wait-TmbServicesHealthy {
    param([string[]]$Services)
    $Deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $Deadline) {
        $AllReady = $true
        foreach ($CurrentService in $Services) {
            $Container = @(Invoke-Compose @("ps", "-q", $CurrentService) |
                Where-Object { $_ }) | Select-Object -First 1
            if (-not $Container) { $AllReady = $false; continue }
            $State = @(Invoke-Docker @(
                    "inspect", "--format", "{{.State.Status}}", $Container
                )) | Select-Object -Last 1
            $Health = @(Invoke-Docker @(
                    "inspect", "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                    $Container
                )) | Select-Object -Last 1
            if ($State -in @("exited", "dead", "restarting")) {
                throw "Service $CurrentService entered a crash/restart state."
            }
            if ($State -ne "running" -or $Health -notin @("healthy", "none")) {
                $AllReady = $false
            }
        }
        if ($AllReady) { return }
        Start-Sleep -Seconds 5
    }
    throw "Updated services did not become healthy before the timeout."
}

function Test-ProjectImageCleanupEnabled {
    $Image = Get-ConfiguredImage
    $Output = @(& docker run --rm `
            -v "${RootDirectory}/config.yaml:/app/config.yaml:ro" `
            $Image python -c `
            "from telegram_media_bot.bootstrap.config import load_settings; print(str(load_settings('/app/config.yaml').operations.update.prune_old_project_images_after_success).lower())")
    if ($LASTEXITCODE -ne 0 -or $Output.Count -eq 0) { return $true }
    return $Output[-1].Trim() -ne "false"
}

function Invoke-TmbCleanup {
    param([switch]$DryRun)
    $WorkspaceArguments = @(
        "run", "--rm", "--no-deps", "worker",
        "telegram-media-bot", "cleanup-workspaces", "--config", "/app/config.yaml"
    )
    if ($DryRun) { $WorkspaceArguments += "--dry-run" }
    Invoke-Compose $WorkspaceArguments | Out-Host

    $CurrentImage = Get-ConfiguredImage
    $CurrentId = @(Invoke-Docker @(
            "image", "inspect", "--format", "{{.Id}}", $CurrentImage
        )) | Select-Object -Last 1
    $ProjectContainers = @(Invoke-Compose @("ps", "-a", "-q") | Where-Object { $_ })
    foreach ($Container in $ProjectContainers) {
        $State = @(Invoke-Docker @(
                "inspect", "--format", "{{.State.Status}}", $Container
            )) | Select-Object -Last 1
        $ImageId = @(Invoke-Docker @(
                "inspect", "--format", "{{.Image}}", $Container
            )) | Select-Object -Last 1
        if ($State -ne "running" -and $ImageId -and $ImageId -ne $CurrentId) {
            if ($DryRun) {
                Write-Host "Would remove stopped project container: $Container"
            }
            else {
                Invoke-Docker @("rm", $Container) | Out-Null
            }
        }
    }

    $Referenced = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($Container in @(Invoke-Docker @("ps", "-aq") | Where-Object { $_ })) {
        $ImageId = @(Invoke-Docker @(
                "inspect", "--format", "{{.Image}}", $Container
            )) | Select-Object -Last 1
        if ($ImageId) { [void]$Referenced.Add($ImageId) }
    }
    $ProjectImages = [System.Collections.Generic.HashSet[string]]::new()
    $ForeignImages = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($Line in @(Invoke-Docker @(
                "image", "ls", "--no-trunc", "--format", "{{.Repository}}|{{.ID}}"
            ))) {
        $Parts = $Line -split "\|", 2
        if ($Parts.Count -ne 2 -or -not $Parts[1]) { continue }
        if ($Parts[0] -eq $ImageRepository) {
            [void]$ProjectImages.Add($Parts[1])
        }
        else {
            [void]$ForeignImages.Add($Parts[1])
        }
    }
    $Candidates = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($ImageId in $ProjectImages) {
        if ($ImageId -ne $CurrentId -and -not $Referenced.Contains($ImageId) -and
            -not $ForeignImages.Contains($ImageId)) {
            [void]$Candidates.Add($ImageId)
        }
    }
    [long]$Reclaimed = 0
    foreach ($ImageId in $Candidates) {
        $SizeText = @(Invoke-Docker @(
                "image", "inspect", "--format", "{{.Size}}", $ImageId
            )) | Select-Object -Last 1
        [long]$Size = 0
        [void][long]::TryParse($SizeText, [ref]$Size)
        $Reclaimed += $Size
        if ($DryRun) {
            Write-Host "Would remove old project image: $ImageId"
        }
        else {
            Invoke-Docker @("image", "rm", $ImageId) | Out-Null
        }
    }
    Write-Host "Project cleanup candidates: $($Candidates.Count) image(s); approximate bytes: $Reclaimed"
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
    Write-Host "1 Start`n2 Stop`n3 Restart`n4 Status`n5 Logs`n6 Doctor`n7 Config`n8 Update`n9 Backup`n10 Cleanup`n0 Exit"
    $Choice = Read-Host "Select"
    $Command = @{
        "1" = "start"; "2" = "stop"; "3" = "restart"; "4" = "status"
        "5" = "logs"; "6" = "doctor"; "7" = "config"; "8" = "update"
        "9" = "backup"; "10" = "cleanup"; "0" = "exit"
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
        Assert-ReleaseAllowed $env:TMB_RELEASE_TAG
        $Release = Get-VerifiedRelease
        $PreviousImage = $null
        $PreviousServices = @()
        $ServiceStateTouched = $false
        $EnvironmentMutationStarted = $false
        try {
            $PreviousImage = Get-ConfiguredImage
            $PreviousServices = @(Get-RunningApplicationServices)
            if ($PreviousServices.Count -gt 0) {
                $ServiceStateTouched = $true
                Invoke-Compose (@("stop", "-t", "45") + $PreviousServices)
            }
            New-TmbBackup
            $EnvironmentMutationStarted = $true
            Set-ConfiguredImage "$ImageRepository`:$($Release.Version)"
            Invoke-Compose @("pull")
            Get-ChildItem -LiteralPath $Release.Payload -Force |
                Copy-Item -Destination $RootDirectory -Recurse -Force
            Start-TmbServices -Services $PreviousServices -ForceRecreate
            Wait-TmbServicesHealthy -Services $PreviousServices
            $RuntimeVersion = @(Invoke-Compose @(
                    "run", "--rm", "--no-deps", "worker", "python", "-c",
                    "import telegram_media_bot; print(telegram_media_bot.__version__)"
                )) | Select-Object -Last 1
            if ($RuntimeVersion.Trim() -ne $Release.Version) {
                throw "Runtime version does not match release $($Release.Version)."
            }
            Invoke-Compose @(
                "run", "--rm", "--no-deps", "worker",
                "telegram-media-bot", "doctor", "--config", "/app/config.yaml"
            ) | Out-Null
            Invoke-Compose @("ps") | Out-Null
            if (Test-ProjectImageCleanupEnabled) {
                try { Invoke-TmbCleanup } catch {
                    Write-Warning "Update succeeded, but old project image cleanup failed."
                }
            }
        }
        catch {
            if ($EnvironmentMutationStarted) {
                Set-ConfiguredImage $PreviousImage
            }
            if ($ServiceStateTouched) {
                Write-Warning "Update failed; restoring the prior image and restarting the stack."
                Start-TmbServices -Services $PreviousServices
            }
            throw
        }
        finally {
            if ($Release -and (Test-Path -LiteralPath $Release.TemporaryDirectory)) {
                Remove-Item -LiteralPath $Release.TemporaryDirectory `
                    -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    "cleanup" {
        if ($Service -and $Service -ne "--dry-run") {
            throw "Usage: tmb cleanup [--dry-run]"
        }
        Invoke-TmbCleanup -DryRun:($Service -eq "--dry-run")
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
    default { throw "Usage: tmb start|stop|restart|status|logs [service]|doctor|config|update|backup|cleanup [--dry-run]|uninstall" }
}
