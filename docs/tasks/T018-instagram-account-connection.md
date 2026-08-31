# T018 - Instagram account connection and recovery UX

**Status:** implemented

## Goal

Let every Free or VIP Telegram user connect, reconnect, inspect the sanitized state of, and
disconnect their own Instagram account through the secure web companion.

## Why

Account connection is independent of purchasing VIP, but Telegram messages must never carry an
Instagram password or 2FA code. The connection lifecycle must produce an encrypted session that
later media policy can use without retaining login secrets.

## Dependencies

- T016 companion web boundary and signed handoff.
- T017 encrypted credential vault, states, generations, and leases.

## Scope

- Add owner-bound Telegram connect/reconnect/disconnect entry points for Free and VIP users.
- Generate a five-minute, single-use signed handoff and open the HTTPS connection page.
- Implement transient web login/session acquisition. Password and 2FA/checkpoint codes remain only
  in bounded in-memory flow state for at most ten minutes and are discarded after each use.
- Verify a newly acquired session through the user-initiated connection operation, normalize only
  the required Instagram cookie records, encrypt immediately, and increment generation.
- Persist only `CONNECTED`, `EXPIRED`, `CHALLENGE_REQUIRED`, `REVOKED`, or `DISCONNECTED` plus safe
  timestamps/failure category.
- Let users revoke/disconnect themselves and let a currently authorized administrator revoke a
  compromised session without seeing it.
- Expose reconnect/re-login behavior for expired/challenged sessions.
- Perform a provider-terms/privacy review and require explicit user consent that the credential is
  used only for content their account can already view.

## Non-goals

- Connecting does not grant VIP, download private media, change public credential preference, keep
  sessions alive through polling, or guarantee Instagram will not challenge an account.

## Architecture

Telegram owns presentation and signs the handoff. The companion owns HTTPS forms and transient
challenge state. An Instagram login/session acquisition adapter is infrastructure behind an
application port; it returns a normalized secret directly to the T017 vault. Domain/application
code never accepts password strings as durable commands or events.

## Data and persistence changes

No new secret table beyond T017. Add only:

- single-use handoff nonce records from T016;
- sanitized connection event kinds, timestamps, credential generation, and actor role;
- no password, 2FA code, upstream response, Instagram username, or raw cookie.

Connection events inherit T017's 90-day retention. Revocation audit identifies only the credential,
Telegram owner, actor role, and stable reason category.

## Security requirements

- Never request credentials in Telegram or place secrets/session material in a URL.
- Disable request/form-body logging and traceback/debug responses.
- Bind the flow to signed owner/purpose/nonce and reauthorize disconnect/admin revoke at execution.
- Rate-limit login, 2FA, reconnect, and handoff creation without personal metric labels.
- Never persist a password or code even when session acquisition fails.

## Failure semantics

Wrong credentials return a generic retryable login result. Checkpoint/2FA produces
`CHALLENGE_REQUIRED`; rejected/expired sessions produce `EXPIRED`; explicit actions produce
`REVOKED`/`DISCONNECTED`. Restarted or expired browser state requires a fresh Telegram handoff.
Raw upstream text is reduced to stable project categories.

## Migration and backward compatibility

Account connection is disabled by default. Free users with no credential require no record. Existing
operator cookies, jobs, Telegram `/start`, and public download behavior remain unchanged.

## Telegram UX

Provide reusable Persian views/actions for:

- `اتصال حساب اینستاگرام`, connection status, and last verified time;
- reconnect/re-login after expiry/challenge;
- disconnect with confirmation;
- clear notice that connection is available to Free users but private downloads require VIP.

Do not reveal Instagram usernames, cookies, upstream errors, or account existence to another user.

## Acceptance gates

- Free and VIP users use the same owner-bound connection service.
- Successful connection stores only encrypted session material; password/code searches of DB/logs
  and error captures return no match.
- Revoke/disconnect invalidates queued materialization immediately.
- No scheduled session-health traffic is introduced.

## Tests

- Free/VIP connect, connected, reconnect, expired, challenge, revoked, and disconnected flows.
- Wrong-owner, expired, replayed, wrong-purpose, and concurrent handoff use.
- Password/2FA absence from SQLite, Redis, logs, metrics, URLs, errors, and Telegram.
- Process restart/timeout during password, 2FA, encryption, and final commit.
- User/admin revoke authorization and cross-user isolation.

## Operational considerations

Document expected checkpoints, session invalidation, rate limiting, upstream changes, datacenter-IP
suspicion, incident revocation, and user support without promising permanent session validity.

## Risks

Instagram may change login flows or prohibit an automation pattern. The adapter must fail closed,
remain replaceable, and never add follower-approval bypass, account takeover, or DRM behavior.

## Definition of done

Secure connection/reconnection/revocation flows, consent and Persian UX, transient-secret tests,
sanitized lifecycle handling, documentation, and gates pass with no media-policy change.

## Implementation notes

Built on the accepted ADR-033/035 foundations. The secure connection surface and its
owner-bound lifecycle are implemented without any media-policy change:

- `domain/instagram_connection.py` — `InstagramLoginResult` and safe `LoginFailureCategory` for
  the transient flow; secrets are never durable.
- `application/ports/instagram_login.py` — `InstagramSessionAcquirer` port that returns a
  normalized login result (a real upstream adapter is operator-supplied and must fail closed;
  no provider client is bundled).
- `application/services/instagram_connection.py` — `InstagramConnectionService`: mints an Ed25519
  signed single-use connection link with the handoff token in the URL fragment, runs the
  transient login, stores a successful session encrypted in the vault (T017), and exposes
  sanitized status/disconnect.
- `infrastructure/instagram_login/fake.py` — deterministic `FakeInstagramSessionAcquirer` for
  tests/operator use (correct password, 2FA checkpoint, reject modes).
- `infrastructure/web_companion/flow.py` — `CompanionInstagramConnectionFlow` drives the browser
  flow with bounded in-memory phase markers; passwords/2FA codes are forwarded once and never
  retained; a successful login stores ciphertext in the vault.
- `bootstrap/instagram.py` — `build_instagram_connection_service` composes the service from
  settings only when vault keys and the handoff signing key are present (None otherwise).
- `telegram/instagram_ux.py` + Persian texts + `/instagram connect|status|disconnect` command on
  `build_router` (owner-bound, injected optional connection); `bot_app` passes the composed
  service. Free and VIP users use the same owner-bound entry points; connecting grants no VIP.
- `bootstrap/config.py`/`config.example.yaml` gain `web_companion.public_base_url` (https only)
  used in generated links.

Tests cover connect-link signing/fragment, password/2FA login, challenge-then-2FA, deny,
password/2FA absence from the vault/logs/database, disconnect erase, cross-user isolation, and the
sanitized Persian rendering. The fully interactive account-status surface (in-dashboard connect/
reconnect/disconnect actions) is delivered with the `/vip` dashboard in T023.
