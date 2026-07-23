# Project status

Last updated: 2026-07-23

## Complete foundation

- Repository metadata, uv project, Dockerfile, Compose, Linux/Windows management scripts. The first `up` generates `uv.lock` if the archive was distributed without one.
- Strict YAML config model and validation command.
- Domain models and download-engine protocol.
- Isolated yt-dlp adapter with semantic format mapping and error translation.
- aiogram polling bot that queues URL jobs.
- ARQ worker that downloads and uploads a document.
- Unit tests, opt-in contract-test scaffold, CI, pre-commit, and security guidance.

## Deliberately incomplete product features

- Metadata preview and inline quality keyboard.
- Progress editing and cancellation.
- Durable job database and restart reconciliation.
- Per-user rate limiter implementation.
- Private-network URL resolution checks beyond current syntax validation.
- Large-file/local Bot API strategy.
- Full playlist UX.
- Admin commands and production metrics.

## Known starter limitations

- The first handler enqueues the configured default mode immediately.
- Worker-side Telegram upload couples the initial delivery path to aiogram; this is documented as a
  provisional decision.
- Contract fixtures require operator-provided safe URLs through environment variables.
- Docker includes ffmpeg but not a JavaScript runtime; add and pin Deno/Node in T008 or T011 after
  choosing a verified runtime image strategy.

## Starter verification

- 36 non-contract tests pass with 93.21% measured core coverage in the artifact environment.
- Architecture, UTF-8/text integrity, compilation, shell syntax, and configuration parsing checks pass.
- `uv.lock` must be generated on the first machine with package-registry access and committed before
  release; management scripts and CI already enforce that workflow.
