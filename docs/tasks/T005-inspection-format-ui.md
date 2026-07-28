# T005 - Inspection and format selection

**Status:** complete (native MP4 codec-first regression fix 2026-07-28)

Inspection runs as an ARQ worker job. Normalized metadata is stored in SQLite behind an opaque,
owner-bound, expiring selection token. Callback data contains only the token and a configured
semantic mode; ownership, expiry, mode membership, playlist count, and duration are validated before
a durable download job is enqueued. The adapter evaluates the same bounded semantic selector used
for download, so exact-height modes are shown only when present and cannot silently fall back.
Each option records resolution, FPS, HDR/SDR, and exact/estimated/unknown component-summed size.
Legacy selections without this JSON field remain readable. Ordinary videos now use an opaque
two-step Container -> semantic-quality callback. MP4/WebM candidates state whether they are native
or require guaranteed conversion. Instagram video URLs bypass the choice UI and enqueue
native-only `best_original`; optional MP4 forcing selects native MP4 video plus M4A and never turns
VP9 into an implicit H.264 transcode.

Fast MP4 now filters native H.264/AVC + AAC before ranking, optionally falls back to the highest
lower compatible resolution, and reports the actual height. Native WebM remains VP9 + Opus.
CPU-heavy MP4 conversion is a separate backward-compatible callback policy and is hidden unless
explicitly enabled.

## Deliverables

- Queue or safely execute metadata inspection without blocking polling.
- Persist short-lived normalized metadata keyed by an opaque token.
- Display source, title, duration, size estimates when available, and playlist count.
- Generate inline buttons from configured semantic modes only.
- Validate callback ownership and expiration.
- Enqueue the selected immutable download request.
- Handle multi-entry content according to playlist policy.
- Add unit and integration tests for callback tampering and expired selections.
