# Security model

Untrusted inputs include Telegram messages, URLs, DNS answers, redirects, remote metadata, titles,
thumbnails, extensions, playlist entries, and upstream error strings.

## Controls

- Only absolute HTTP(S) URLs without credentials or invalid ports are accepted.
- Local/internal names and every non-global resolved address (loopback, private, link-local,
  reserved, multicast, unspecified, and metadata-service addresses) are rejected. Mixed public and
  private DNS answer sets are rejected. The adapter revalidates extracted page/media/playlist URLs.
- Only operator-defined semantic modes/selectors, output roots, postprocessors, cookies, proxy, and
  headers exist. Users cannot supply commands, output templates, destinations, or yt-dlp options.
- Static allow/block policy, durable admin blocks, and a fail-closed Redis per-user rate limit are
  applied before inspection.
- Output and temporary paths resolve under fixed roots. Containers run non-root, read-only, with all
  Linux capabilities dropped and `no-new-privileges`; writable state is limited to `/data` and tmpfs.
- Tokens, cookies, authorization, passwords, proxy values, and URL credentials are recursively
  redacted. Arbitrary user URLs and file paths are not logged.
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
