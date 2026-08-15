# Docker installation and `tmb` management

The v1.3 runtime image installs the exact locked `gallery-dl==1.32.8` package and preserves its
GPL-2.0 notice. Upgrading from v1.0.x does not rewrite `config.yaml`, `.env`, SQLite/WAL, Redis,
cookies, downloads, backups, or Telegram Local Bot API state. A null `gallery_dl.cookies.instagram`
automatically reuses the deployed `yt_dlp.cookies_file`, as do TikTok, Twitter/X, and Pinterest.
Legacy non-null gallery cookie entries must resolve to that same canonical combined file.

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

The Linux `update` runs from an isolated copy and completes release download, checksum, staged
Bash/Compose/config validation, and candidate image pulls before downtime. It records all project
services, stops only the running filesystem writers, keeps Redis/ARQ online, creates a private
atomic durable-state backup, and replaces top-level application entries through a rollback
snapshot. Runtime permissions, same-UID filesystem and SQLite WAL writes, package version, and
doctor are verified before candidate writers start. Health and the exact original service set are
then mandatory. Any post-stop failure restores the prior transaction and service state;
intentionally stopped services remain stopped. `config.yaml`, `.env`, SQLite, cookies, Redis,
Local API state, downloads, and temp content are preserved. `uninstall` removes local state only
after the literal `DELETE` confirmation.

### Upgrade from v1.2.2 to v1.3.0

After the `v1.3.0` release and image are published, a normal version-pinned update is sufficient:

```bash
cd /path/to/telegram-media-downloader-bot
TMB_RELEASE_TAG=v1.3.0 tmb update
tmb doctor
tmb status
```

No cookie migration is needed when v1.2.2 defines only
`yt_dlp.cookies_file: /data/cookies/cookies.txt`, as confirmed for the current production
deployment. If any legacy `gallery_dl.cookies.*` value points elsewhere, first merge all records
from those files into the canonical combined file, then set every alias to null or to the same
canonical path. The v1.3.0 configuration check deliberately fails divergent paths instead of
leaving a runtime consumer on stale credentials.

### Upgrade from v1.3.0 to v1.3.1

The Linux v1.3.0 updater takes its backup before stopping the Local Bot API writer, and it cannot
replace that running updater with corrected code before the vulnerable step. After v1.3.1 and its
assets are published, use the checksummed standalone updater exactly once:

```bash
cd /path/to/telegram-media-downloader-bot
release_tag="v1.3.1"
project_root="$(pwd -P)"
bootstrap_dir="$(mktemp -d)"
chmod 0700 "$bootstrap_dir"
curl -fsSL \
  "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases/download/${release_tag}/tmb-updater.sh" \
  -o "$bootstrap_dir/tmb-updater.sh"
curl -fsSL \
  "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases/download/${release_tag}/tmb-updater.sh.sha256" \
  -o "$bootstrap_dir/tmb-updater.sh.sha256"
(cd "$bootstrap_dir" && sha256sum --check tmb-updater.sh.sha256)
sudo env TMB_ROOT_DIR="$project_root" TMB_RELEASE_TAG="$release_tag" \
  bash "$bootstrap_dir/tmb-updater.sh" update
rm -rf -- "$bootstrap_dir"
tmb doctor
tmb status
```

The checksum is mandatory. Do not substitute the installed v1.3.0 `tmb update` for this one-time
Linux bootstrap. The v1.3.1 transaction records the running services itself, so do not manually
stop them first. Windows v1.3.0 already stops its application writers before `Compress-Archive` and
may use the ordinary version-pinned PowerShell update. After v1.3.1 is installed, ordinary pinned
updates are sufficient again.

Before restart, the Linux updater resolves `APP_UID`/`APP_GID` from the Compose environment or
`.env` with fallback `10001:10001`. It repairs `data/`, SQLite/WAL/SHM, downloads, temp, cookies,
Local API state, and backups to the configured runtime owner with directories at `0700` and files
at `0600`. This keeps cookie material unavailable to group/other while allowing the runtime's
upstream cookie jar to read and persist required updates. The application root remains traversable
at `0755` (its owner depends on the historical updater path); installer-managed `.env` and
`config.yaml` retain their installer owner and `0600` mode. A container running as the exact
configured runtime identity must create/remove a state probe and enable SQLite WAL. The updater
then guarantees
`scripts/tmb.sh` is executable, repairs `$TMB_BIN_DIR/tmb` (default `/usr/local/bin/tmb`), resolves
its target, and runs `tmb status`.

For a damaged v1.0.2 command, restore the executable link once before selecting the hotfix:

```bash
chmod 0755 ./scripts/tmb.sh
sudo ln -sfn "$(pwd)/scripts/tmb.sh" /usr/local/bin/tmb
TMB_RELEASE_TAG=v1.0.3 tmb update
```

The published v1.2.1 updater validates with its old container mounts before it can install a newer
script. To move an affected v1.2.1 installation to v1.2.2 without editing `config.yaml`, verify and
run the standalone release updater once from the project root:

```bash
release_tag="v1.2.2"
project_root="$(pwd -P)"
bootstrap_dir="$(mktemp -d)"
curl -fsSL \
  "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases/download/${release_tag}/tmb-updater.sh" \
  -o "$bootstrap_dir/tmb-updater.sh"
curl -fsSL \
  "https://github.com/HamedSanaei/telegram-media-downloader-bot/releases/download/${release_tag}/tmb-updater.sh.sha256" \
  -o "$bootstrap_dir/tmb-updater.sh.sha256"
(cd "$bootstrap_dir" && sha256sum --check tmb-updater.sh.sha256)
sudo env TMB_ROOT_DIR="$project_root" TMB_RELEASE_TAG="$release_tag" \
  bash "$bootstrap_dir/tmb-updater.sh" update
rm -rf -- "$bootstrap_dir"
```

Run this only after v1.2.2 assets are published and from the directory containing the installation's
`config.yaml`. The checksum is mandatory. This changes neither the configuration nor cookie bytes;
ordinary `tmb update` is sufficient again after v1.2.2 is installed.

Backups contain `config.yaml`, `.env`, SQLite/state including present WAL/SHM files, cookies, and
durable Local API state. Large `data/downloads` and disposable `data/temp` content are preserved in
place during update but excluded from archives to avoid duplicating multi-gigabyte media. The exact
volatile `data/telegram-bot-api/telegram-bot-api.log` path is also excluded; no broad log wildcard is
used. Linux archives are mode `0600`, published by atomic rename, and incomplete files are removed.

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
place it below `data/cookies`, configure its container path, and restrict it to the runtime owner
with mode `0600`. The canonical file and its parent directory must be owner-writable when Telegram
cookie management is used. No phone number, SMS, 2FA, Userbot, or Telegram user session is used.

Configured administrators can subsequently open **Cookie Management** in the bot's private chat.
Uploading a Netscape `cookies.txt` updates only records for services detected from its domains;
other service records in `yt_dlp.cookies_file` remain unchanged. The upload filename is ignored,
files above 2 MiB are rejected, and every successful change leaves a mode-preserving backup under
`data/cookies/.cookie-backups/`. The complete current canonical file can be downloaded from the
same private admin section. Treat that document and all backups as secrets. No restart is required;
yt-dlp and every gallery-dl source reopen the same effective path for subsequent jobs. Existing
deployments with different per-source gallery cookie files must first combine their records into
`yt_dlp.cookies_file` and set those legacy entries to null or the same canonical path.
