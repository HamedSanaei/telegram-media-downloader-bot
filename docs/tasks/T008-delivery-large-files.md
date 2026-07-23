# T008 - Telegram delivery and runtime media dependencies

**Status:** complete (2026-07-23)

Delivery is behind a project port and selects audio, video, or document with document fallback.
Captions and filenames are sanitized, upload limits fail explicitly, and an optional local Bot API
base URL is supported. Video modes select a complete SDR source at the requested resolution
ceiling and transcode oversized results to H.264/AAC at that resolution beneath the final delivery
limit. Source transfers have a separate bounded ceiling and never treat one surviving stream as a
complete video. File uploads use a dedicated configurable request timeout instead of aiogram's
shorter general session default. Docker pins Deno 2.9.3 and installs ffmpeg; `doctor` reports runtime
versions.

## Deliverables

- Select `send_audio`, `send_video`, or `send_document` using normalized result data and fallback.
- Implement configurable caption and filename sanitization.
- Handle files above configured Telegram limits explicitly.
- Design and optionally support a local Telegram Bot API server behind a delivery port.
- Add and pin a verified JavaScript runtime strategy for yt-dlp where required.
- Verify ffmpeg and runtime versions in `doctor`.
- Add delivery fallback tests.
