# T003 - yt-dlp adapter boundary

**Status:** complete (Twitter HLS audio-only metadata compatibility added 2026-08-01)

Implement project-owned engine models and protocol, a single direct yt-dlp integration package,
semantic format mapping, metadata normalization, output isolation, and error translation.

Codex must expand edge-case coverage without weakening the boundary.

YouTube inspection and download receive canonical URLs and set `noplaylist=true` whenever a valid
video ID is present. Real `/playlist?list=...` URLs retain bounded collection behavior.

Twitter HLS audio formats may be treated as AAC only for the extractor's explicit, narrow
`hls-audio-*-Audio` + `m3u8` + audio-only MP4 metadata contract. Unknown MP4 audio remains
incompatible. Inspection-selected IDs are authoritative at download time and a two-stream MP4 plan
is a lossless remux, never a transcode.
