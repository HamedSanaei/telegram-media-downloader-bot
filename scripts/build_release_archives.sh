#!/usr/bin/env bash
set -euo pipefail

COMMIT="${1:?usage: build_release_archives.sh COMMIT OUTPUT_DIRECTORY}"
OUTPUT_DIRECTORY="${2:?usage: build_release_archives.sh COMMIT OUTPUT_DIRECTORY}"
PREFIX="telegram-media-downloader-bot"
TEMPORARY_DIRECTORY="$(mktemp -d)"
trap 'rm -rf -- "$TEMPORARY_DIRECTORY"' EXIT

PROJECT_VERSION="$(
  git show "$COMMIT:pyproject.toml" |
    sed -n 's/^version = "\([^"]*\)"/\1/p' | head -n 1
)"
[[ -n "$PROJECT_VERSION" ]] || {
  echo "Unable to determine release version from $COMMIT." >&2
  exit 2
}
python scripts/check_release_policy.py --version "$PROJECT_VERSION"

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
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_tmb_upgrade_integration.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_local_api_readiness.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_tmb.sh" \
  "$TEMPORARY_DIRECTORY/tree/$PREFIX/scripts/tests/test_readonly_logger_preflight.sh"

COMMIT_EPOCH="${TMB_RELEASE_ARCHIVE_EPOCH:-$(git show -s --format=%ct "$COMMIT")}"
[[ "$COMMIT_EPOCH" =~ ^[0-9]+$ ]] || {
  echo "Release archive epoch must be an integer Unix timestamp." >&2
  exit 2
}
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

# Publish the prepared updater as a self-extracting, checksummed standalone asset. Older
# releases used it as a one-time bootstrap when the installed updater could not handle a
# transition; the manager now lives in scripts/tmb.sh plus scripts/lib/, so the standalone
# asset embeds that whole scripts tree and runs the entrypoint from its own extraction. The
# payload is deterministic (fixed mtime/owner/order, gzip -n) so two builds of one commit
# compare byte-identical, as the publish workflow asserts.
UPDATER_PAYLOAD="$(mktemp)"
tar \
  --sort=name \
  --mtime="@$COMMIT_EPOCH" \
  --owner=0 \
  --group=0 \
  --numeric-owner \
  --format=posix \
  --pax-option=delete=atime,delete=ctime \
  -cf - \
  -C "$TEMPORARY_DIRECTORY/tree/$PREFIX" \
  scripts \
  | gzip -n -9 >"$UPDATER_PAYLOAD"
{
  cat <<'HEADER'
#!/usr/bin/env bash
# Self-contained tmb updater. The embedded payload is the full scripts tree
# (entrypoint, lib/, and tests), extracted to a private directory before the
# update transaction runs, so this asset works without the repository layout.
set -euo pipefail
updater_directory="$(mktemp -d)" || exit 1
trap 'rm -rf -- "${updater_directory:?}"' EXIT
payload_line="$(awk '/^#__TMB_UPDATER_PAYLOAD__$/{ print NR; exit }' "$0")"
[[ "$payload_line" =~ ^[0-9]+$ ]] || {
  echo "tmb-updater.sh is corrupt: payload marker not found." >&2
  exit 2
}
tail -n +"$((payload_line + 1))" "$0" | tar -xz -C "$updater_directory" || {
  echo "tmb-updater.sh payload extraction failed." >&2
  exit 2
}
exec bash "$updater_directory/scripts/tmb.sh" "${@:-update}"
#__TMB_UPDATER_PAYLOAD__
HEADER
  cat "$UPDATER_PAYLOAD"
} >"$OUTPUT_DIRECTORY/tmb-updater.sh"
rm -f -- "$UPDATER_PAYLOAD"
chmod 755 "$OUTPUT_DIRECTORY/tmb-updater.sh"
(
  cd "$OUTPUT_DIRECTORY"
  sha256sum tmb-updater.sh >tmb-updater.sh.sha256
)
