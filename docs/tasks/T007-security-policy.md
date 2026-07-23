# T007 - URL security, source policy, rate limits, and admin controls

**Status:** pending

## Deliverables

- Resolve and reject loopback, private, link-local, reserved, multicast, and metadata-service hosts.
- Revalidate redirects as far as the adapter permits.
- Enforce allowed and blocked users.
- Implement Redis-backed per-user rate limits.
- Enforce enabled-source policy after normalized inspection.
- Add admin commands for health, queue depth, failed jobs, and user blocks without exposing secrets.
- Add abuse-oriented tests.
