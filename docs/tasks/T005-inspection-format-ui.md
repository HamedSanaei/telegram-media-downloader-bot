# T005 - Inspection and format selection

**Status:** complete (real-format/size expansion 2026-07-24)

Inspection runs as an ARQ worker job. Normalized metadata is stored in SQLite behind an opaque,
owner-bound, expiring selection token. Callback data contains only the token and a configured
semantic mode; ownership, expiry, mode membership, playlist count, and duration are validated before
a durable download job is enqueued. The adapter evaluates the same bounded semantic selector used
for download, so exact-height modes are shown only when present and cannot silently fall back.
Each option records resolution, FPS, HDR/SDR, and exact/estimated/unknown component-summed size.
Legacy selections without this JSON field remain readable.

## Deliverables

- Queue or safely execute metadata inspection without blocking polling.
- Persist short-lived normalized metadata keyed by an opaque token.
- Display source, title, duration, size estimates when available, and playlist count.
- Generate inline buttons from configured semantic modes only.
- Validate callback ownership and expiration.
- Enqueue the selected immutable download request.
- Handle multi-entry content according to playlist policy.
- Add unit and integration tests for callback tampering and expired selections.
