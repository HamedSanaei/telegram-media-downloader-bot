# T008 - Telegram delivery and runtime media dependencies

**Status:** pending

## Deliverables

- Select `send_audio`, `send_video`, or `send_document` using normalized result data and fallback.
- Implement configurable caption and filename sanitization.
- Handle files above configured Telegram limits explicitly.
- Design and optionally support a local Telegram Bot API server behind a delivery port.
- Add and pin a verified JavaScript runtime strategy for yt-dlp where required.
- Verify ffmpeg and runtime versions in `doctor`.
- Add delivery fallback tests.
