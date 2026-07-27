#!/usr/bin/env bash
set -euo pipefail

COMMIT="${1:?usage: build_release_archives.sh COMMIT OUTPUT_DIRECTORY}"
OUTPUT_DIRECTORY="${2:?usage: build_release_archives.sh COMMIT OUTPUT_DIRECTORY}"
PREFIX="telegram-media-downloader-bot"
TEMPORARY_DIRECTORY="$(mktemp -d)"
trap 'rm -rf -- "$TEMPORARY_DIRECTORY"' EXIT

mkdir -p "$OUTPUT_DIRECTORY" "$TEMPORARY_DIRECTORY/tree"
git archive --format=tar --prefix="$PREFIX/" "$COMMIT" \
  | tar -xf - -C "$TEMPORARY_DIRECTORY/tree"

# The v1.0.2 updater applies runtime ownership before `cp -a`. Omitting tracked data placeholders
# prevents that final copy from resetting the repaired ownership/modes of persistent state.
rm -rf -- "${TEMPORARY_DIRECTORY:?}/tree/$PREFIX/data"

# v1.0.2 replaces its own script with `cp -a` while Bash is still reading it. Shipping the Linux
# command as a symlink makes cp unlink the pathname instead of truncating the executing inode.
mv \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tmb.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tmb-current.sh"
ln -s tmb-current.sh "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tmb.sh"
chmod 755 \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/install.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/manage.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tmb-current.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_tmb_update.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_tmb_upgrade_integration.sh"

COMMIT_EPOCH="$(git show -s --format=%ct "$COMMIT")"
tar \
  --sort=name \
  --mtime="@$COMMIT_EPOCH" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --format=posix \
  --pax-option=delete=atime,delete=ctime \
  -cf - \
  -C "$TEMPORARY_DIRECTORY/tree" \
  "$PREFIX" \
  | gzip -n -9 >"$OUTPUT_DIRECTORY/telegram-media-downloader-bot.tar.gz"

# Keep the Windows ZIP as a regular-file git archive; Windows uses scripts/tmb.ps1.
git archive \
  --format=zip \
  --prefix="$PREFIX/" \
  --output="$OUTPUT_DIRECTORY/telegram-media-downloader-bot.zip" \
  "$COMMIT"

(
  cd "$OUTPUT_DIRECTORY"
  sha256sum telegram-media-downloader-bot.tar.gz \
    >telegram-media-downloader-bot.tar.gz.sha256
  sha256sum telegram-media-downloader-bot.zip \
    >telegram-media-downloader-bot.zip.sha256
)
