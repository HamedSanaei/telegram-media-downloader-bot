# Multipart large-file delivery

## Routing policy

The worker uses one deterministic rule:

- results at or below `telegram.max_upload_size_mb` are sent directly;
- every larger result at or below `multipart.max_total_size_mb` is packaged as stored ZIP volumes;
- results above `media.max_file_size_mb` or `multipart.max_total_size_mb` are rejected before
  delivery.

The production Local Bot API values are a 1900 MB direct ceiling, 1850 MB ZIP volumes, and a
4096 MB final-file ceiling. The smaller volume size preserves headroom for Telegram metadata and
implementation differences.

No Premium account, phone number, login code, 2FA password, user session, MTProto client, private
staging channel, or `copyMessage` route is involved.

## Configuration

```yaml
telegram:
  max_upload_size_mb: 1900
  local_api_is_local: true

media:
  max_file_size_mb: 4096
  max_source_size_mb: 4096

multipart:
  enabled: true
  seven_zip_executable: 7zz
  part_size_mb: 1850
  max_total_size_mb: 4096
  compression_level: 0
```

`part_size_mb` must not exceed the configured Telegram direct upload ceiling when multipart delivery
can be reached. `compression_level` is fixed at zero so media is not wastefully recompressed.

On Windows, `seven_zip_executable` may be an absolute path such as
`C:/Tools/7-Zip/7zz.exe`. Relative paths containing a directory are resolved from the directory that
contains `config.yaml`. On Linux and in the provided image, use `7zz` from `PATH`.

Validate the installation without displaying configuration secrets:

```powershell
uv run telegram-media-bot config-check --config .\config.yaml
uv run telegram-media-bot doctor --config .\config.yaml
```

## Files sent to the user

For image/mixed bundles at or below direct limits, the Telegram adapter first chooses a
deterministic inline plan. JPEG/PNG/WebP images use `send_photo`; streamable videos may share a
mixed media group; compatible collections are split into 2–10 item groups without changing source
order. A format, dimension, or Telegram rejection selects document delivery without converting or
overwriting the original. Captions appear only on the first item allowed by each group.

The semantic image-ZIP action creates one store-only archive in source order with numeric names.
If that archive exceeds the direct ceiling, the existing 7-Zip multipart subsystem splits it and
persists/deletes each confirmed part exactly like an oversized video. No separate retention,
retry, or archive lifecycle exists for gallery media.

For an oversized result, the bot sends:

1. `.zip.001`, `.zip.002`, and any later volumes in ordinal order;
2. one JSON manifest containing the original filename, size, and SHA-256 plus the size and SHA-256
   of every volume;
3. a Persian extraction instruction.

Each Telegram response is stored as a separate `delivery_items` receipt with a unique ordinal.
Ambiguous Telegram delivery still enters `delivery_uncertain` and is not resent automatically.

## Extraction

Put every `.zip.NNN` file in the same directory. Open `.zip.001` with 7-Zip and choose Extract.
Do not rename one volume independently. Verify the volume hashes against the JSON manifest before
extracting, and optionally verify the reconstructed original-file hash afterward.

Example:

```bash
7zz x media.mp4.zip.001
```

## Cleanup and capacity

ZIP volumes and manifests are created inside the unique job directory. After each successful
Telegram response and durable delivery receipt, that part is deleted immediately. The final worker
cleanup removes the source, any unsent parts, the manifest, and the job directory on every terminal
outcome. Cancellation terminates the job-scoped 7-Zip process group before cleanup. Capacity
planning must temporarily allow the source file plus approximately one additional
source-file-sized set of stored volumes.

The recipient requires 7-Zip. The multipart route intentionally favors operational simplicity over
single-message delivery for files above the direct Local Bot API ceiling.
