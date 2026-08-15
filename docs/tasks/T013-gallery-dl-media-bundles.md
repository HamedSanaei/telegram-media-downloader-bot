# T013 - gallery-dl image and mixed-media bundles

**Status:** implemented; local Docker gate pending because Docker is unavailable on the review host
(2026-08-01)

Add a replaceable subprocess adapter pinned to gallery-dl 1.32.8 for bounded single-post image
and mixed-media inspection/download on Instagram, TikTok, Twitter/X, and Pinterest. Preserve the
yt-dlp video/audio path, add YouTube thumbnails and SoundCloud artwork, persist only normalized
stable asset identities, and deliver ordered assets through photo, media-group, document, or the
existing multipart archive path.

## Acceptance gates

- No `gallery_dl` import outside the isolated adapter and no raw vendor JSON or signed CDN URL in
  durable state.
- Single-item URL validation, one canonical cookie file shared with yt-dlp (superseding the original
  source-specific design), bounded subprocess output/time/concurrency, process-group cancellation,
  safe workspace confinement, and Pillow signature validation.
- Sanitized 1.32.8 fixtures cover image, mixed, video-fallback, authentication, schema-change, and
  limit behavior without network access.
- Existing config and durable jobs remain readable; gallery-dl settings are defaulted.
- Full repository, package, and Docker quality gates pass before the version moves to 1.1.0.
