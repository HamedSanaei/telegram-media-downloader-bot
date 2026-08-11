# Docker installation and `tmb` management

The v1.1 runtime image installs the exact locked `gallery-dl==1.32.8` package and preserves its
GPL-2.0 notice. Upgrading from v1.0.x does not rewrite `config.yaml`, `.env`, SQLite/WAL, Redis,
cookies, downloads, backups, or Telegram Local Bot API state. A null `gallery_dl.cookies.instagram`
automatically reuses the deployed `yt_dlp.cookies_file`; other sources use their own optional
read-only cookie paths.

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

`update` runs from an isolated copy, validates every staged Bash script plus Compose and config
before stopping writers, keeps Redis/ARQ online, backs up durable state, and replaces top-level
application entries through a rollback snapshot. It repairs runtime permissions, requires actual
same-UID filesystem and SQLite WAL writes, pulls/starts the candidate, and verifies container
health before repairing and executing the global command. Any post-stop failure restores the prior
source, image, usable permissions, command link, and exact previous service set. `config.yaml`,
`.env`, SQLite, cookies, Redis, Local API state, and downloads are preserved. `uninstall` removes
local state only after the literal `DELETE` confirmation.

Before restart, the Linux updater resolves `APP_UID`/`APP_GID` from the Compose environment or
`.env` with fallback `10001:10001`. It repairs `data/`, SQLite/WAL/SHM, downloads, temp, cookies,
Local API state, and backups to private runtime-owned modes. A container running as that exact
identity must create/remove a state probe and enable SQLite WAL. The updater then guarantees
`scripts/tmb.sh` is executable, repairs `$TMB_BIN_DIR/tmb` (default `/usr/local/bin/tmb`), resolves
its target, and runs `tmb status`.

For a damaged v1.0.2 command, restore the executable link once before selecting the hotfix:

```bash
chmod 0755 ./scripts/tmb.sh
sudo ln -sfn "$(pwd)/scripts/tmb.sh" /usr/local/bin/tmb
TMB_RELEASE_TAG=v1.0.3 tmb update
```

Backups contain `config.yaml`, `.env`, SQLite/state, cookies, and Local API state. Large
`data/downloads` and disposable `data/temp` content are preserved in place during update but
excluded from archives to avoid duplicating multi-gigabyte media.

For a release rollback, select the previous tag and run the same verified updater:

```bash
TMB_RELEASE_TAG=v1.0.1 tmb update
```

```powershell
$env:TMB_RELEASE_TAG = "v1.0.1"; tmb update
```

Restore the timestamped archive under `backups/` only when state rollback is also required, and
keep the current backup until the older release has passed `tmb doctor`.

The Local API image is built from pinned official `tdlib/telegram-bot-api` source. API credentials
are read from mounted YAML and passed to the official process only through its child environment;
they do not appear in Compose environment, Docker command, installer logs, shell history, or status
output. Bot and Worker use `http://local-api:8081`.

The first image build may still spend substantial time compiling Telegram Local Bot API. CI and
release builds share the `telegram-media-downloader-bot-amd64` BuildKit cache, so later builds should
restore that stage. Changing `TELEGRAM_BOT_API_REF`, its stage/toolchain/base image, or the relevant
Dockerfile instructions intentionally invalidates it; application source and Python changes do not.

For private Instagram Stories/Highlights, export a Netscape cookies file from an authorized account,
place it below `data/cookies`, configure its container path, and keep it read-only. No phone number,
SMS, 2FA, Userbot, or Telegram user session is used.
