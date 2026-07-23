# T005 - Inspection and format selection

**Status:** complete (2026-07-23)

Inspection runs as an ARQ worker job. Normalized metadata is stored in SQLite behind an opaque,
owner-bound, expiring selection token. Callback data contains only the token and a configured
semantic mode; ownership, expiry, mode membership, playlist count, and duration are validated before
a durable download job is enqueued. A generic upstream size estimate is displayed as advisory
metadata because it may describe a different format; selected-format and final-file limits remain
mandatory.

## Deliverables

- Queue or safely execute metadata inspection without blocking polling.
- Persist short-lived normalized metadata keyed by an opaque token.
- Display source, title, duration, size estimates when available, and playlist count.
- Generate inline buttons from configured semantic modes only.
- Validate callback ownership and expiration.
- Enqueue the selected immutable download request.
- Handle multi-entry content according to playlist policy.
- Add unit and integration tests for callback tampering and expired selections.
