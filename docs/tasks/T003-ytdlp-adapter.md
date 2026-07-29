# T003 - yt-dlp adapter boundary

**Status:** complete (single-video YouTube intent defense added 2026-07-29)

Implement project-owned engine models and protocol, a single direct yt-dlp integration package,
semantic format mapping, metadata normalization, output isolation, and error translation.

Codex must expand edge-case coverage without weakening the boundary.

YouTube inspection and download receive canonical URLs and set `noplaylist=true` whenever a valid
video ID is present. Real `/playlist?list=...` URLs retain bounded collection behavior.
