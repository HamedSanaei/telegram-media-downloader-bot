# Security model

## Threat boundaries

Untrusted inputs include Telegram messages, URLs, redirects, remote metadata, file extensions,
titles, thumbnails, and upstream error strings.

## Baseline controls

- only `http` and `https` URL schemes;
- local config excluded from Git;
- container runs as non-root;
- no shell commands built from user input;
- no user-controlled yt-dlp option dictionaries or output templates;
- fixed output root and per-job directories;
- configurable source allowlist and file-size limits;
- sanitized user-facing errors;
- cookies stored outside source and mounted at runtime;
- dependency lockfile and CI gates.

## Required next controls

T007 must resolve hostnames and reject loopback, link-local, private, reserved, and metadata-service
addresses before passing URLs to the engine, including after redirects where practical. It must add
real per-user rate limiting and allow/block policy enforcement.

## Secret handling

`telegram.bot_token`, proxy credentials, future API keys, and cookies must never be logged. Use a
local config file with restrictive permissions. Production backups containing the config or cookies
must be encrypted and access-controlled.

## Abuse and legal boundaries

Do not implement DRM circumvention. Operators are responsible for source policies, applicable laws,
platform terms, and responding to abuse reports. The bot should communicate failures honestly and
must not claim that alternate-source resolution is a direct download from another service.
