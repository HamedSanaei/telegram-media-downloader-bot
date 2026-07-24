# Acceptance criteria for the complete first release

A release is acceptable when all of the following are true:

1. A user can submit one supported URL and receive normalized metadata.
2. The bot offers configured semantic formats without exposing raw format IDs.
3. A selected job is queued and processed without blocking polling.
4. Progress updates are throttled and do not trigger Telegram edit floods.
5. Cancellation stops work and cleans temporary files.
6. Retries do not create uncontrolled duplicate uploads.
7. Disabled sources, blocked users, size limits, duration limits, and playlist limits are enforced.
8. Private-network and local-file URLs are rejected.
9. All secrets are loaded from ignored local config and are redacted from logs.
10. `yt_dlp` imports exist only in the adapter package.
11. An upstream yt-dlp update can be tested by updating `uv.lock`; unchanged project contracts keep
    the rest of the code untouched.
12. `./manage.sh up` starts the complete stack after config creation.
13. The required lint, format, type, test, coverage, and build gates pass.
14. Restart and cleanup integration tests pass.
15. Documentation matches actual behavior and lists any remaining limitations.
16. Local Bot API migration is explicit, durable, and never repeats an uncertain `logOut`.
17. Bot and Worker cannot run against mixed cloud/local endpoints, and no Local API credential is
    exposed through command lines, logs, health, CLI status, Compose, or image layers.
18. A migrated local runtime accepts configured files above 200 MB and up to the 1900 MB
    application ceiling without size-triggered transcoding below that ceiling.
