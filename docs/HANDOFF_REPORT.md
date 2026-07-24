# Handoff verification report

Generated: 2026-07-24

## Release scope

Tasks T001 through T012 are implemented for release `1.0.0`. The delivered flow is public URL
validation, queued inspection, normalized metadata, owner-bound semantic selection, durable
download, throttled progress and cancellation, typed Telegram delivery, terminal persistence, and
safe cleanup. The application imports `yt_dlp` only inside `infrastructure/ytdlp/`; the example
external extractor remains an independent distribution under `plugins/`.

This release also includes managed/external Telegram Local Bot API lifecycle, config-only
credentials, durable explicit migration and rollback, shared Bot/Worker endpoint leases, and a
1900 MB practical direct-delivery ceiling without size-triggered transcoding below that ceiling.
Semantic 1440p, 2160p, and non-transcoding original-quality modes are available. Files above the
direct ceiling are sent as stored 1850 MB ZIP volumes with a manifest through the 4096 MB media
ceiling.

Inspection now exposes only real selector candidates. Every option carries owned resolution, FPS,
HDR/SDR, and exact/estimated/unknown selected-output size. Fixed heights cannot silently fall back.
Tracked direct/multipart delivery reports byte progress to Telegram and structured logs, followed
by elapsed-only heartbeats during opaque Telegram processing. Successful volume receipts are
durable before the next volume begins.

## Verification completed on this host

- Runtime: CPython 3.14.5; pytest 9.1.1; locked yt-dlp 2026.07.04; uv 0.11.28 locally.
- `uv lock --check` and `uv sync --frozen --group dev`: passed.
- Architecture import boundaries, manifest integrity, and UTF-8/text integrity checks: passed.
- Ruff lint and format checks: passed for 98 Python files.
- Strict mypy: passed for 89 source/test files.
- Root tests: 167 passed, 1 destructive large-file case skipped, and 6 opt-in source contracts
  deselected.
- Measured core branch coverage: 82.91%, above the enforced 80% floor.
- The explicitly enabled contract suite selected all 6 cases; all were skipped because no
  operator-approved `CONTRACT_*_URL` fixtures were configured.
- Real inspection of `GEXtERfkOHQ` exposed 1440p, 1080p, 720p, and 480p candidates and correctly
  omitted 2160p. It reported exact selected video+audio sizes, including 2,146,444,183 bytes for
  1440p, 744,420,917 bytes for 1080p, and 2,703,504,315 bytes for `best_original`.
- `doctor` passed locally for Python 3.14.5, yt-dlp 2026.07.04, ffmpeg 8.1.2, ffprobe 8.1.2,
  Deno 2.9.3, the managed Local Bot API, and the configured portable 7-Zip executable.
- Plugin SDK: independent lock/sync passed; 1 test passed and 1 contract case was deselected.
- Secret scan through the committed pre-commit baseline: passed. A second scan including current
  tracked and untracked source files reported no findings.
- Dependency integrity: `pip check` passed.
- Dependency vulnerability audit: `pip-audit 2.10.1` reported no known vulnerabilities after pytest
  was upgraded to the fixed 9.x line and both lockfiles were regenerated.
- The locked runtime no longer contains Telethon, cryptg, ruamel.yaml, or their transitive
  authentication dependencies.
- Configuration validation, JSON-schema generation, Compose YAML parsing, Dockerfile static checks,
  PowerShell syntax parsing, and `git diff --check`: passed.
- Python source distribution and wheel build: passed for version 1.0.0.
- Unit/integration coverage includes exact 1440p/2160p/original selection, HDR/FPS behavior,
  exact direct/multipart threshold routing, ambiguous-delivery quarantine, per-item persistence,
  multipart ordering, and SHA-256 manifests.
- A native portable 7-Zip smoke test created a stored split ZIP volume successfully.
- Telegram upload calls were verified to receive the configured 14400-second per-part timeout and
  1024 KiB tracked chunk size. Tests cover measured transfer percentage, opaque-finalization
  heartbeats, no network-error fallback, and immediate prior-volume persistence.
- Local lifecycle tests verified managed child credentials are absent from command lines, cloud
  `logOut` is called at most once, uncertain migration is quarantined, mixed endpoint leases are
  rejected, cloud rollback waits 10 minutes, and a declared 201 MB file reaches delivery unchanged.
- `config-check`, safe `local-api status`, and `doctor` passed against the local operator
  configuration without printing credentials.

## Checks not executable on this host

- Docker/Podman/Buildah/nerdctl are not installed, so an actual container image build and Compose
  startup could not be run locally. CI contains a required `docker build` job, while the Dockerfile
  and Compose document were statically validated here.
- Bash is not installed, so `bash -n manage.sh` could not be rerun on this Windows host. The
  PowerShell management script parsed successfully; its component quality/security/build commands
  were run directly because the complete script would also reach the unavailable Docker tooling.
- The YouTube, SoundCloud, Instagram, Twitter/X, Pinterest, and TikTok network contracts were not
  executed because their operator-approved fixture URLs were not configured. They remain excluded
  from the default suite.
- The real 201 MB tracked Telegram upload test was explicitly enabled, but safely skipped because
  local `telegram.admin_ids` does not contain a private test chat ID. It remains opt-in and is
  documented in `docs/LOCAL_BOT_API.md`.
- The destructive real multipart reconstruction test above 3900 MB requires an explicit
  environment opt-in, so it was not run on this workstation.

## Operational limitations

- The supported v1 topology is one worker container with bounded in-process concurrency. Multi-host
  workers require a leased/shared durable database adapter instead of the local SQLite/WAL store.
- Telegram has no upload idempotency key. A crash during upload is quarantined as
  `delivery_uncertain` for operator review and is never resent automatically.
- Multi-volume recipients need 7-Zip and must start extraction from `.zip.001`.
- URL and extracted-media validation narrows SSRF exposure, but DNS rebinding between validation and
  an upstream socket connect requires infrastructure egress filtering for complete defense in depth.
- The official Local Bot API executable is operator-supplied and is not bundled. No user account,
  phone login, MTProto session, or staging channel is used. Castbox, Spotify, DRM circumvention,
  and user-controlled yt-dlp settings remain intentionally outside v1 scope.

## Release commands

Run `./manage.sh check` (or `manage.ps1 check`) and a real `docker build` on a Docker-capable release
host. Enable contract tests only with reviewed public fixtures, deploy a staging canary, apply the
documented comparison threshold, and retain the prior immutable image plus Git/lockfile revision for
rollback.
