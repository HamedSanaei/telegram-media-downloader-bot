# Docker installation and `tmb` management

## Linux

Supported installer targets are Ubuntu 22.04+ and Debian 12+. Other distributions work when Docker
Engine, Compose v2, curl, `tar`, and `sha256sum` are already installed.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.sh)
```

The installer downloads the latest release archive and its SHA-256 file, rejects any mismatch,
pins the GHCR image to the verified application version, obtains Bot token/API ID/API hash through
hidden interactive prompts, optionally collects admins, required channels, yt-dlp proxy, and
Instagram cookies, then writes only `config.yaml` with restrictive permissions. Set
`TMB_RELEASE_TAG=vX.Y.Z` before running the installer to select an explicit release. Local API
migration requires typing `MIGRATE`; normal startup never calls `logOut`.

Release tags publish both `tar.gz` and ZIP source assets plus their SHA-256 files. CI runs
ShellCheck for the Linux installer/manager and PSScriptAnalyzer for the Windows equivalents. A tag
is accepted only when `vX.Y.Z` exactly matches the package version in `pyproject.toml`, keeping the
source archive and immutable GHCR tag aligned.

## Windows

Windows 10/11 requires WSL2 and Docker Desktop:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; irm https://raw.githubusercontent.com/HamedSanaei/telegram-media-downloader-bot/main/install.ps1 | iex
```

When Docker Desktop installation requires restart, the installer writes a non-secret resume marker.
Reboot/start Docker Desktop and run the same command again. Installation defaults to
`%LOCALAPPDATA%\TelegramMediaDownloaderBot`; a `tmb.cmd` shim is added to `%USERPROFILE%\bin` and
the user PATH.

## Management

Run `tmb` without arguments for the menu, or:

```text
tmb start
tmb stop
tmb restart
tmb status
tmb logs [bot|worker|local-api|redis]
tmb doctor
tmb config
tmb update
tmb backup
tmb uninstall
```

`update` records which application services are running, gracefully stops only those
Bot/Worker/Local API writers, and leaves Redis/its queue running. It then creates a consistent
SQLite/WAL backup, downloads and verifies the release in an isolated staging directory, pulls the
matching pinned image, installs the verified source, and recreates only the services that were
previously running. A download, checksum, archive-validation, or image-pull failure leaves the
installed source untouched, restores the prior image pin, and restarts exactly the prior service
set. `config.yaml`, SQLite, cookies, Redis, and downloaded state are preserved. `uninstall` stops
the stack and deletes config/data only after the literal `DELETE` confirmation.

Backups contain `config.yaml`, `.env`, SQLite/state, cookies, and Local API state. Large
`data/downloads` and disposable `data/temp` content are preserved in place during update but
excluded from archives to avoid duplicating multi-gigabyte media.

For a release rollback, select the previous tag and run the same verified updater:

```bash
TMB_RELEASE_TAG=v1.0.0 tmb update
```

```powershell
$env:TMB_RELEASE_TAG = "v1.0.0"; tmb update
```

Restore the timestamped archive under `backups/` only when state rollback is also required, and
keep the current backup until the older release has passed `tmb doctor`.

The Local API image is built from pinned official `tdlib/telegram-bot-api` source. API credentials
are read from mounted YAML and passed to the official process only through its child environment;
they do not appear in Compose environment, Docker command, installer logs, shell history, or status
output. Bot and Worker use `http://local-api:8081`.

For private Instagram Stories/Highlights, export a Netscape cookies file from an authorized account,
place it below `data/cookies`, configure its container path, and keep it read-only. No phone number,
SMS, 2FA, Userbot, or Telegram user session is used.
