# T007 - URL security, source policy, rate limits, and admin controls

**Status:** complete (2026-07-23)

Public URL validation rejects credentials, invalid ports, local/internal names, and every non-global
resolved address. The adapter revalidates extracted and selected media URLs. Static/durable user
blocks, allowlists, Redis fixed-window rate limiting, and admin health/queue/failure/block commands
are implemented with abuse-oriented tests.

## Deliverables

- Resolve and reject loopback, private, link-local, reserved, multicast, and metadata-service hosts.
- Revalidate redirects as far as the adapter permits.
- Enforce allowed and blocked users.
- Implement Redis-backed per-user rate limits.
- Enforce enabled-source policy after normalized inspection.
- Add admin commands for health, queue depth, failed jobs, and user blocks without exposing secrets.
- Add abuse-oriented tests.
