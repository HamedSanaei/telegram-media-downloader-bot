# T005 - Inspection and format selection

**Status:** complete (native-only truthful UI and Back navigation 2026-07-29)

Inspection runs as an ARQ worker job. Normalized metadata is stored in SQLite behind an opaque,
owner-bound, expiring selection token. Versioned callback data contains only the token and a short
opaque option identity; ownership, expiry, option membership, playlist count, and duration are validated before
a durable download job is enqueued. The adapter evaluates the same bounded semantic selector used
for download, so exact-height modes are shown only when present and cannot silently fall back.
Each option records selected stream identity/codecs, resolution, FPS, HDR/SDR, component sizes, and
exact/estimated/unknown total size. The application planner hides transcode-required/wrong-codec
plans and deduplicates requested modes by their actual output. Ordinary videos use an opaque
two-step Native container -> actual-quality callback. Instagram video URLs bypass the choice UI and enqueue
native-only `best_original`; optional MP4 forcing selects native MP4 video plus M4A and never turns
VP9 into an implicit H.264 transcode.

MP4 Native filters H.264/AVC + AAC before ranking and may fall back to the highest lower compatible
resolution, but repeated fallbacks render only once under their actual height. WebM Native remains
VP9 + Opus. CPU-heavy conversion is never public; legacy callbacks safely return to the Native menu.
Every pre-enqueue page has Back and reuses this selection without another inspection or job.

## Deliverables

- Queue or safely execute metadata inspection without blocking polling.
- Persist short-lived normalized metadata keyed by an opaque token.
- Display source, title, duration, size estimates when available, and playlist count.
- Generate inline buttons from configured semantic modes only.
- Validate callback ownership and expiration.
- Enqueue the selected immutable download request.
- Handle multi-entry content according to playlist policy.
- Add unit and integration tests for callback tampering and expired selections.
