# Security model

Untrusted inputs include Telegram messages and documents, URLs, DNS answers, redirects, remote
metadata, titles, thumbnails, extensions, playlist entries, and upstream error strings.

## Controls

- Only absolute HTTP(S) URLs without credentials or invalid ports are accepted.
- Local/internal names and every non-global resolved address (loopback, private, link-local,
  reserved, multicast, unspecified, and metadata-service addresses) are rejected. Mixed public and
  private DNS answer sets are rejected. The adapter revalidates extracted page/media/playlist URLs.
- Only operator-defined semantic modes/selectors, output roots, postprocessors, cookies, proxy, and
  headers exist. Users cannot supply commands, output templates, destinations, or yt-dlp options.
- Static allow/block policy, durable admin blocks, all-channel membership, and a fail-closed Redis
  per-user rate limit are applied before inspection. Telegram membership errors deny access.
- Output and temporary paths resolve under fixed roots. Containers run non-root, read-only, with all
  Linux capabilities dropped and `no-new-privileges`; writable state is limited to `/data` and tmpfs.
- Tokens, cookies, authorization, passwords, proxy values, Local API hashes, and URL credentials
  are recursively redacted. Managed `api_id`/`api_hash` are read only from YAML and passed only in
  the child process environment, never its command line. Arbitrary user URLs and file paths are not
  logged.
- The optional proxy is a `SecretStr` scoped to the yt-dlp adapter. It never affects Telegram,
  Redis, Local API, or installer traffic and is not included in logs or CLI health output.
- Interactive configuration uses atomic replacement and removes its secret-bearing temporary file
  on success or validation failure; `config.yaml.tmp` is ignored as defense in depth.
- Cookie administration requires a current administrator in private chat, ignores upload filenames,
  applies an in-memory 2 MiB stream bound, validates strict Netscape records, and atomically updates
  the existing canonical path only after creating a restricted backup. Configuration rejects
  divergent consumer paths, so stale source-specific cookie state cannot bypass an admin update.
  Logs and replies expose no cookie names, values, domains, contents, filenames, or paths.
- User persistence stores only Bot API profile fields and aggregate usage. No phone, email, contact,
  user-session, SMS, or two-factor credential is collected.
- Delivery limits are checked before Telegram; ambiguous uploads enter `delivery_uncertain` rather
  than risking an automatic duplicate.
- Dependencies are locked and checked against current vulnerability advisories with `pip-audit`;
  secrets, architecture, and text integrity are checked; yt-dlp upgrades are never automatic or
  auto-merged.

## Residual risks and operations

DNS rebinding between validation and the upstream connection cannot be eliminated without owning the
yt-dlp transport; multiple validation points reduce the window. Run the worker on an egress-filtered
network that denies private/metadata ranges as defense in depth. Protect `config.yaml`, cookies,
SQLite backups, and Redis volumes with restrictive permissions and encryption where applicable.

The project does not implement DRM circumvention. Operators remain responsible for platform terms,
copyright, source allowlists, abuse response, and lawful use.

Local Bot API support is bot-only. Phone numbers, SMS/login codes, two-step passwords, MTProto user
sessions, and Userbot accounts are explicitly outside the design. Migration is interactive,
stateful, fail-closed, and blocked while live Bot/Worker endpoint leases exist.
