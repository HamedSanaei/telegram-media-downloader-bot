# Personal Instagram connection activation

Personal connection is optional and disabled by default. The Telegram message that connection is
unavailable means the installation lacks one or more prerequisites; configuring administrator IDs
does not enable it. Public downloads continue using the existing shared operator cookie.

## Why an HTTPS hostname is needed

The bot issues an expiring, single-use owner-bound link to a page hosted by the operator. This is
not Instagram's own website or OAuth. Logging into instagram.com in a user's browser does not
give this separate service access to that browser's Instagram session. The page explicitly explains
the operator-owned login flow and asks for consent. Passwords and 2FA codes are transient; only
authenticated session cookies are encrypted in the existing vault. Never request these secrets in
Telegram, support chat, logs, URLs, or command-line arguments.

Use a hostname you control with a trusted certificate and correct DNS/ingress routing. A hostname
alone does not configure the origin server. Verify HTTPS has no redirect loop before inviting
users. Do not replace unrelated services listening on ports 80/443.

## Provisioning

Use a tested checkout/image containing `instagram-setup`; older published images may not have
this command. From the installation directory, stop any existing companion first. Back up the
installation using the normal management workflow before changing configuration.

For a checkout with its Python 3.14 environment:

```bash
uv run telegram-media-bot instagram-setup --config config.yaml \
  --public-base-url https://connect.example.com
uv run telegram-media-bot instagram-setup --config config.yaml --check
```

For Docker, use the exact tested image selected for the core services (the main configuration's
storage root must remain `/data`, matching the normal Docker installation):

```bash
docker run --rm --user 0 \
  --mount type=bind,src="$PWD",dst=/workspace \
  --mount type=bind,src="$PWD/data",dst=/data \
  "$TMB_IMAGE" telegram-media-bot instagram-setup --config /workspace/config.yaml \
  --public-base-url https://connect.example.com
docker run --rm --user 0 \
  --mount type=bind,src="$PWD",dst=/workspace \
  --mount type=bind,src="$PWD/data",dst=/data \
  "$TMB_IMAGE" telegram-media-bot instagram-setup --config /workspace/config.yaml --check
```

`TMB_IMAGE` above is a shell variable containing that exact image; a value only inside `.env` is
not automatically available to `docker run`. Do not print or source arbitrary secret configuration.
These one-shot provisioning containers intentionally access the authoritative configuration; the
long-running web service must not.

Keep the provisioning command's `/data` bind writable as shown. A SQLite `mode=ro` connection can
still require writable WAL/SHM coordination files when checking existing encrypted-session rows;
a read-only data bind may correctly fail closed with "Cannot verify existing credential storage".
This differs from the updater's explicitly read-only static preflight. Keep runtime ownership of
SQLite sidecars consistent with `APP_UID`/`APP_GID`. The long-running companion still receives only
the reduced read-only profile and writable state directory, never the full bot configuration.

Setup enables the existing settings, preserves valid encryption/signing keys, creates missing keys
only for a fresh installation, and writes private backups under `backups/instagram-setup-*`.
Files are atomic individually; the restricted profile is written first, enabling main config last,
and a failed main write restores/removes the newly written profile. Repeating unchanged setup is
idempotent. Mismatched handoff keys, lost vault keys with encrypted rows, invalid origins and
concurrent setup fail closed. Restore original keys rather than deleting encrypted sessions.
Keep backups private: they contain the original configuration and any existing keys.

`configuration_ready: true` checks local key/profile consistency only. It does not check DNS,
TLS, container liveness, ingress, or whether Instagram will accept an account's credentials.

## Runtime and ingress

The generated ignored `companion.local.yaml` contains only the verifier, vault and shared-state
settings. Do not edit it independently or mount the complete bot configuration into the web service.
`docker-compose.companion.yml` is a separate project with only this reduced read-only profile and
`data/state` mounted. It uses the same `TMB_IMAGE` selection as the bot, runs non-root/read-only,
drops capabilities, disables core dumps, and publishes only `127.0.0.1:8090`.

```bash
docker compose -f docker-compose.companion.yml config --quiet
docker compose -f docker-compose.companion.yml up -d companion
curl --fail http://127.0.0.1:8090/ready
docker compose up -d --force-recreate bot worker
```

Recreating bot/worker ensures their file bind mounts use the newly atomically replaced config;
schedule this explicit runtime change with the normal operator rollout procedure.
Route the selected hostname through the existing HTTPS ingress to loopback port 8090. The
companion must not be exposed directly on a public plaintext port. Secure cookies require HTTPS;
if using a CDN, configure its origin HTTPS/TLS/redirect behavior consistently. Do not disable
certificate validation, record credential bodies, add analytics, or log fragment handoffs.
For a separate reverse-proxy container, loopback is not that container's network: provide an
explicit private routing design instead of publishing 8090 globally.

### Explicit CDN HTTP-origin exception

HTTPS to the origin is preferred. If the operator explicitly chooses CDN-terminated TLS with an
HTTP origin, the edge-to-origin link carries credentials unencrypted. An edge-IP allowlist and a
random port reduce exposure but do not encrypt or authenticate that network leg. Record this
choice separately from application readiness; do not describe it as end-to-end TLS.

Keep the companion on loopback 8090 and use a dedicated reverse-proxy listener on a verified-free
operator-selected port, without changing unrelated 80/443 listeners. Restrict that listener to
loopback and the CDN's current official egress ranges, reject unexpected Host headers, and require
the trusted edge's `X-Forwarded-Proto: https` indication. Browser access remains HTTPS-only.
Disable edge caching for the entire connection hostname, retain no-store responses, and avoid
request-body buffering/access logs for credential requests. Maintain the IP allowlist explicitly
when the provider changes its ranges; do not trust client-supplied forwarded IP chains.

An optional `/origin-health` probe can report ingress reachability without pretending the app is
ready. The actual `/ready` and connection routes must continue returning an unavailable status
until the separately deployed companion is healthy. Check both through the CDN after the panel's
origin protocol, port and Host settings have been applied.

Check the public `/ready` endpoint and the connection page without credentials. Then open a new
link from the bot, consent, submit credentials and handle 2FA when requested. Wrong passwords can
be retried; expired or interrupted sessions need a fresh bot link. Check connection status in the
bot afterward, and verify disconnect changes the owner state. Instagram may require confirmation
in its app or reject this login protocol; the implementation never substitutes a fake successful
session for a denied/checkpoint response. Fixture tests are not a live account-login guarantee.

Connection alone grants no VIP. Existing public personal-first/shared-fallback routing and private
owner/VIP policy are unchanged. Session failures still notify only the affected owner.

## Backup, update, restore and rollback

The companion writes the shared SQLite/WAL state and must be stopped before core management
writes. Both Bash and PowerShell management fail closed when the installed companion is running
or its Docker state cannot be verified. Keep `companion.local.yaml` in place; do not bypass the
guard, delete the profile, or start the companion during a management transaction.

```bash
docker compose -f docker-compose.companion.yml stop companion
# Run the normal tmb backup/update/restore operation now.
# After successful core upgrade and image selection:
docker compose -f docker-compose.companion.yml up -d --force-recreate companion
```

The updater still owns its original four services; it does not silently restart this separate
project. Recreate the companion with the matching core image, including after rollback. The main
private configuration and SQLite state remain in normal backups. After restoring a configuration
or explicitly rotating keys, rerun setup with the restored origin to regenerate its reduced
profile, then restart/recreate affected processes. Never generate a replacement vault key for
existing ciphertext. To disable new connection links, stop the companion, explicitly set
`web_companion.enabled: false` in main configuration and restart the bot/worker; retain keys and
state for safe recovery.
