# T008 - Telegram delivery and runtime media dependencies

**Status:** complete (bounded transcode and 7-Zip image expansion 2026-07-27)

Delivery is behind a project port and selects audio, video, or document with document fallback.
Captions and filenames are sanitized, upload limits fail explicitly, and an optional local Bot API
base URL is supported. Video modes select a complete SDR source at the requested resolution
ceiling and transcode oversized results to H.264/AAC at that resolution beneath the final delivery
limit. Source transfers have a separate bounded ceiling and never treat one surviving stream as a
complete video. File uploads use a dedicated configurable four-hour per-part timeout, 1024 KiB
tracked chunks, and 30-second opaque-finalization heartbeats instead of aiogram's shorter general
session default. Docker pins Deno 2.9.3 and installs ffmpeg; `doctor` reports runtime versions.

The expanded implementation supports a 1900 MB practical ceiling with a shared config-derived
Bot/Worker client, managed/external Local Bot API lifecycle, explicit idempotent cloud/local
migration, safe CLI/doctor/readiness reporting, cross-process endpoint leases, and an opt-in real
upload test above 200 MB. Files below `telegram.max_upload_size_mb` are never transcoded solely due
to the independent media policy.

The second expansion adds semantic `1440p`, `2160p`, and `best_original`, plus lossless stored ZIP
volumes for every result above the direct upload ceiling and up to 4096 MB. Each delivery item is
persisted separately for restart safety. No Premium account, Userbot, MTProto session, private
staging channel, or copy step is used.

Fixed resolution modes are exact rather than fallback ceilings, selected video+audio size is
calculated during inspection, and direct/multipart upload byte progress appears in both the status
message and structured console log. Telegram post-body processing is reported as elapsed time only.

The official Local Bot API is compiled from a pinned upstream commit in the multi-stage image and
can run as a separate Compose service. Ordinary non-original video guarantees MP4 H.264/AAC or
WebM VP9/Opus; WebM is delivered as a document. Original-quality media is native-only, and a native
VP9 MP4 is delivered as a document instead of being re-encoded for inline playback. Ordered
Instagram video artifacts are sent separately.

Heavy FFmpeg work is bounded by configurable encoder threads, one conservative concurrent encode,
a timeout, an operator disable switch, process-tree cleanup, and structured progress. The runtime
image guarantees compatible `7zz` and `7z` names, and CI/release smoke tests create and verify a real
multi-volume archive.
Each multipart item is deleted immediately after its durable delivery receipt. A later failure
leaves only the not-yet-confirmed items for the worker's terminal workspace cleanup.

## Deliverables

- Select `send_audio`, `send_video`, or `send_document` using normalized result data and fallback.
- Implement configurable caption and filename sanitization.
- Handle files above configured Telegram limits explicitly.
- Design and optionally support a local Telegram Bot API server behind a delivery port.
- Add and pin a verified JavaScript runtime strategy for yt-dlp where required.
- Verify ffmpeg and runtime versions in `doctor`.
- Add delivery fallback tests.
