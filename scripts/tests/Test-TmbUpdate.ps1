$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tmb-tests-" + [guid]::NewGuid())

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) { throw "tmb update test failed: $Message" }
}

function Invoke-UpdateCase {
    param(
        [string]$Name,
        [bool]$FailChecksum,
        [bool]$FailDownload = $false
    )
    $CaseRoot = Join-Path $TestRoot $Name
    $ScriptDirectory = Join-Path $CaseRoot "scripts"
    $LogPath = Join-Path $CaseRoot "operations.log"
    New-Item -ItemType Directory -Force `
        $ScriptDirectory, `
        (Join-Path $CaseRoot "data/state"), `
        (Join-Path $CaseRoot "data/cookies"), `
        (Join-Path $CaseRoot "data/telegram-bot-api"), `
        (Join-Path $CaseRoot "data/downloads"), `
        (Join-Path $CaseRoot "data/temp") | Out-Null
    Copy-Item (Join-Path $SourceRoot "scripts/tmb.ps1") (Join-Path $ScriptDirectory "tmb.ps1")
    $Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText(
        (Join-Path $CaseRoot "config.yaml"),
        "telegram:`n  bot_token: V1_CONFIG_SENTINEL",
        $Utf8NoBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $CaseRoot ".env"),
        "TMB_IMAGE=example.invalid/tmb:1.0.2`nCOMPOSE_PROFILES=local-api`nAPP_UID=10001`nAPP_GID=10001`nTMB_WORKER_CPUS=1.5`n",
        $Utf8NoBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $CaseRoot "pyproject.toml"),
        'version = "1.0.2"',
        $Utf8NoBom
    )
    "sqlite-v1-state" | Set-Content -Encoding ascii `
        (Join-Path $CaseRoot "data/state/jobs.sqlite3")
    "cookies-v1-state" | Set-Content -Encoding ascii `
        (Join-Path $CaseRoot "data/cookies/cookies.txt")
    "local-api-v1-state" | Set-Content -Encoding ascii `
        (Join-Path $CaseRoot "data/telegram-bot-api/state.bin")
    "runtime-media-v1" | Set-Content -Encoding ascii `
        (Join-Path $CaseRoot "data/downloads/large.mp4")

    $PreviousLocation = Get-Location
    try {
        & {
            param($ScriptPath, $OperationLog, $ShouldFailChecksum, $ShouldFailDownload)

            function docker {
                $Joined = $args -join " "
                Add-Content -Encoding utf8 $OperationLog ("docker " + $Joined)
                if ($Joined -match " ps --services --filter status=running$") {
                    Write-Output "bot"
                    Write-Output "worker"
                    Write-Output "local-api"
                    Write-Output "redis"
                }
                elseif ($Joined -match " ps -a -q$") {
                    Write-Output "stopped-project-container"
                }
                elseif ($Joined -match " ps -q (bot|worker|local-api)$") {
                    Write-Output "$($Matches[1])-container"
                }
                elseif ($Joined -eq "inspect --format {{.State.Status}} stopped-project-container") {
                    Write-Output "exited"
                }
                elseif ($Joined -eq "inspect --format {{.Image}} stopped-project-container") {
                    Write-Output "sha256:old-unused"
                }
                elseif ($Joined -match "^inspect --format \{\{\.State\.Status\}\}") {
                    Write-Output "running"
                }
                elseif ($Joined -match "^inspect --format \{\{if \.State\.Health\}\}") {
                    Write-Output "healthy"
                }
                elseif ($Joined -match "run --rm --no-deps worker python -c") {
                    Write-Output "1.0.3"
                }
                elseif ($Joined -match "operations\.update\.prune_old_project_images") {
                    Write-Output "true"
                }
                elseif ($Joined -match "^image inspect --format \{\{\.Id\}\}") {
                    Write-Output "sha256:current"
                }
                elseif ($Joined -eq "ps -aq") {
                    Write-Output "referenced-container"
                }
                elseif ($Joined -eq "inspect --format {{.Image}} referenced-container") {
                    Write-Output "sha256:old-used"
                }
                elseif ($Joined -match "^image ls .*Repository.*ID") {
                    Write-Output "ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:current"
                    Write-Output "ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:old-unused"
                    Write-Output "ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:old-used"
                    Write-Output "ghcr.io/hamedsanaei/telegram-media-downloader-bot|sha256:shared-tag"
                    Write-Output "example/another-project|sha256:shared-tag"
                    Write-Output "redis|sha256:redis"
                }
                elseif ($Joined -eq "image inspect --format {{.Size}} sha256:old-unused") {
                    Write-Output "12345"
                }
                $global:LASTEXITCODE = 0
            }

            function Invoke-WebRequest {
                param(
                    [switch]$UseBasicParsing,
                    [Parameter(Position = 0)]
                    [string]$Uri,
                    [string]$OutFile
                )
                $null = $UseBasicParsing
                Add-Content -Encoding utf8 $OperationLog "download $Uri"
                if ($ShouldFailDownload) { throw "Release download failed." }
                if ($OutFile -like "*.sha256") {
                    "abc  telegram-media-downloader-bot.zip" |
                        Set-Content -Encoding ascii $OutFile
                }
                else {
                    "fixture" | Set-Content -Encoding ascii $OutFile
                }
            }

            function Get-FileHash {
                param(
                    [string]$Algorithm,
                    [Parameter(Position = 0)]
                    [string]$LiteralPath
                )
                $null = $Algorithm, $LiteralPath
                [pscustomobject]@{ Hash = if ($ShouldFailChecksum) { "bad" } else { "abc" } }
            }

            function Expand-Archive {
                param(
                    [string]$LiteralPath,
                    [string]$DestinationPath,
                    [switch]$Force
                )
                $null = $LiteralPath, $Force
                $Payload = Join-Path $DestinationPath "telegram-media-downloader-bot"
                New-Item -ItemType Directory -Force $Payload | Out-Null
                'version = "1.0.3"' | Set-Content -Encoding utf8 `
                    (Join-Path $Payload "pyproject.toml")
                "services: {}" | Set-Content -Encoding utf8 `
                    (Join-Path $Payload "docker-compose.yml")
                foreach ($DataDirectory in @("state", "cookies", "downloads")) {
                    New-Item -ItemType Directory -Force `
                        (Join-Path $Payload "data/$DataDirectory") | Out-Null
                }
                "release-placeholder" | Set-Content -Encoding ascii `
                    (Join-Path $Payload "data/state/.gitkeep")
                "release-placeholder" | Set-Content -Encoding ascii `
                    (Join-Path $Payload "data/cookies/README.md")
                "release-placeholder" | Set-Content -Encoding ascii `
                    (Join-Path $Payload "data/downloads/.gitkeep")
            }

            function Compress-Archive {
                param(
                    [string[]]$Path,
                    [string]$DestinationPath,
                    [switch]$Force
                )
                $null = $Force
                Add-Content -Encoding utf8 $OperationLog ("backup " + ($Path -join " "))
                "backup" | Set-Content -Encoding ascii $DestinationPath
            }

            . $ScriptPath update
        } (Join-Path $ScriptDirectory "tmb.ps1") $LogPath $FailChecksum $FailDownload
        if ($FailChecksum -or $FailDownload) {
            throw "Expected update failure unexpectedly succeeded."
        }
    }
    catch {
        if (-not ($FailChecksum -or $FailDownload)) { throw }
    }
    finally {
        Set-Location $PreviousLocation
    }

    $EnvironmentText = Get-Content -Raw -Encoding utf8 (Join-Path $CaseRoot ".env")
    $VersionText = Get-Content -Raw -Encoding utf8 (Join-Path $CaseRoot "pyproject.toml")
    $Log = @(Get-Content -Encoding utf8 $LogPath)
    if ($FailChecksum -or $FailDownload) {
        Assert-True ($EnvironmentText -match "example\.invalid/tmb:1\.0\.2") `
            "checksum failure did not restore the previous image"
        Assert-True ($VersionText -match 'version = "1.0.2"') `
            "unverified release content was installed"
        Assert-True (-not ($Log -match " pull$")) "pull ran after checksum failure"
        Assert-True ([bool]($Log -match " up -d --no-build bot worker local-api$")) `
            "previous stack was not restarted"
        return
    }
    Assert-True ($EnvironmentText -match "telegram-media-downloader-bot:1\.0\.3") `
        "successful update did not pin the verified version"
    $NormalizedEnvironment = $EnvironmentText.Replace("`r`n", "`n").TrimEnd()
    Assert-True (
        $NormalizedEnvironment -eq
        "TMB_IMAGE=ghcr.io/hamedsanaei/telegram-media-downloader-bot:1.0.3`nCOMPOSE_PROFILES=local-api`nAPP_UID=10001`nAPP_GID=10001`nTMB_WORKER_CPUS=1.5"
    ) "update changed .env beyond TMB_IMAGE"
    Assert-True (
        (Get-Content -Raw -Encoding utf8 (Join-Path $CaseRoot "config.yaml")) -match
        "V1_CONFIG_SENTINEL"
    ) "successful update overwrote config.yaml"
    Assert-True (
        (Get-Content -Raw -Encoding ascii (Join-Path $CaseRoot "data/state/jobs.sqlite3")) -match
        "sqlite-v1-state"
    ) "successful update overwrote SQLite state"
    Assert-True (
        (Get-Content -Raw -Encoding ascii (Join-Path $CaseRoot "data/cookies/cookies.txt")) -match
        "cookies-v1-state"
    ) "successful update overwrote cookies"
    Assert-True (
        (Get-Content -Raw -Encoding ascii (
            Join-Path $CaseRoot "data/telegram-bot-api/state.bin"
        )) -match "local-api-v1-state"
    ) "successful update overwrote Local Bot API state"
    Assert-True (
        (Get-Content -Raw -Encoding ascii (Join-Path $CaseRoot "data/downloads/large.mp4")) -match
        "runtime-media-v1"
    ) "successful update overwrote existing downloads"
    Assert-True ([bool]($Log -match " stop -t 45 bot worker local-api$")) `
        "application writers were not stopped"
    Assert-True (-not ($Log -match "stop .*redis")) "Redis was stopped before backup"
    Assert-True ([bool]($Log -match "backup .*data/state.*data/cookies.*data/telegram-bot-api")) `
        "durable state was not backed up"
    Assert-True (-not ($Log -match "backup .*data/downloads")) `
        "runtime downloads were copied into the backup"
    $StopIndex = [array]::IndexOf($Log, ($Log -match " stop bot worker local-api$")[0])
    $BackupIndex = [array]::IndexOf($Log, ($Log -match "^backup ")[0])
    $DownloadIndex = [array]::IndexOf($Log, ($Log -match "^download ")[0])
    Assert-True ($StopIndex -lt $BackupIndex -and $BackupIndex -lt $DownloadIndex) `
        "stop, consistent backup, and download ordering is wrong"
    Assert-True ([bool]($Log -match " up -d --no-build --force-recreate bot worker local-api$")) `
        "successful update did not recreate the stack"
    Assert-True ([bool]($Log -match "run --rm --no-deps worker python -c")) `
        "successful update did not verify runtime version"
    Assert-True ([bool]($Log -match "run --rm --no-deps worker telegram-media-bot doctor")) `
        "successful update did not run doctor"
    Assert-True ([bool]($Log -match "image rm sha256:old-unused")) `
        "unused old project image was not removed"
    Assert-True ([bool]($Log -match "docker rm stopped-project-container")) `
        "stopped old Compose project container was not removed"
    Assert-True (-not ($Log -match "image rm sha256:old-used")) `
        "container-referenced image was removed"
    Assert-True (-not ($Log -match "image rm sha256:redis")) `
        "image from another repository was removed"
    Assert-True (-not ($Log -match "image rm sha256:shared-tag")) `
        "image ID with a foreign repository tag was removed"
    Assert-True (-not ($Log -match "(image|system|volume) prune")) `
        "unsafe global prune command was used"
}

try {
    Invoke-UpdateCase -Name "success" -FailChecksum $false
    Invoke-UpdateCase -Name "checksum-failure" -FailChecksum $true
    Invoke-UpdateCase -Name "download-failure" -FailChecksum $false -FailDownload $true
    Write-Output "Windows tmb update recovery tests passed."
}
finally {
    if (Test-Path -LiteralPath $TestRoot) {
        Remove-Item -LiteralPath $TestRoot -Recurse -Force
    }
}
