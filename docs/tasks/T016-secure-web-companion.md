# T016 - Secure companion web and callback boundary

**Status:** planned

## Goal

Establish the proposed architecture and minimal least-privilege HTTP boundary shared by future
Instagram account linking and payment callbacks while keeping their security domains separate.

## Why

Telegram messages are not a safe password/2FA channel, and gateways require server callbacks.
Neither concern belongs in polling handlers or media workers.

## Dependencies

- ADR-035 must be accepted during this task after threat-model review.
- Existing `aiohttp` dependency and SQLite/WAL control plane.
- T015 for real payment callback application handling; the boundary can be scaffolded first.

## Scope

- Implement a separate optional `aiohttp.web` companion process and composition root.
- Give it least-privilege settings containing only database path, web listener/security material,
  handoff public key, vault-key access, and future provider credentials.
- Ensure the process cannot read or receive the Telegram bot token.
- Separate `/instagram/connect/...` browser routes from `/payment/callback/{provider}` machine
  routes with independent middleware and request models.
- Define bot-signed Ed25519 handoff claims: purpose, Telegram owner, nonce, issue time, and five-
  minute expiry. Deliver the token in the URL fragment, POST-exchange it, and consume a stored nonce
  hash exactly once.
- Add secure/HttpOnly/SameSite session cookies, synchronizer CSRF tokens, CSP, no-referrer policy,
  body/time/rate limits, sanitized access logging, health/readiness, and HTTPS deployment contract.
- Keep interactive flow state in bounded memory for at most ten minutes.

## Non-goals

- No Instagram login implementation, payment provider, production exposure, DNS/TLS automation,
  password storage, or Docker service rollout in this task unless ADR-035 acceptance explicitly
  includes the minimal disabled service.

## Architecture

```text
Telegram bot --signed handoff--> companion browser boundary --> account-link application port
Payment provider --signed webhook--> provider adapter --> billing application port
```

Browser handlers never call media engines. Callback handlers never trust browser state. The route
layer parses bounded input; cryptographic/provider adapters verify it; application services receive
project-owned commands. The service shares SQLite/WAL but not bot-process secrets.

## Data and persistence changes

| Entity/field group | Purpose and sensitivity | Constraints/indexes | Retention/compatibility |
|---|---|---|---|
| handoff nonce hash/purpose/owner/expiry/consumed time | Replay prevention; owner is personal data | unique nonce hash; expiry index | purge within 24 hours after expiry |
| callback receipt idempotency reference | Safe handoff to T015 | provider/reference uniqueness belongs to billing | retained under payment audit policy |

Passwords, 2FA codes, CSRF tokens, cookies, signatures, authorization headers, and full callback
payloads are never durable.

## Security requirements

- HTTPS at the ingress; reject forwarded scheme/host unless the proxy is explicitly trusted.
- Constant-time signature checks, strict claim purpose/audience, bounded clock skew, single use.
- CSRF on every browser mutation; payment webhooks use provider signature/replay validation instead.
- No secret/query logging, directory listing, debug page, traceback response, or permissive CORS.
- Rate limits cannot use user/IP values as Prometheus labels.

## Failure semantics

Expired, replayed, wrong-purpose, malformed, or wrong-owner handoffs fail generically. Restarted
interactive sessions require the user to reopen the Telegram link. Invalid webhooks never reach the
billing service. Health failure never changes subscription or credential state.

## Migration and backward compatibility

The companion is disabled by default. Existing Compose/configuration remains valid. Any future
service-specific ignored YAML and mounts must be additive and documented as an ADR-004 refinement.

## Telegram UX

T018/T023 own messages and buttons. This task defines only the short-lived link contract and safe
expired/replayed result codes they may present.

## Acceptance gates

- The service demonstrably starts without the Telegram bot token.
- Handoff tokens do not reach HTTP access logs or Referer headers.
- Browser redirect/status cannot confirm a payment.
- Instagram and payment middleware/configuration are isolated despite sharing a process.

## Tests

- Signature, purpose, owner, issue/expiry, skew, replay, and concurrent-consumption tests.
- CSRF, SameSite/Secure cookie, CSP/referrer, CORS, method, body-size, timeout, and rate-limit tests.
- Secret-redaction tests for URLs, form bodies, headers, exceptions, and callback payloads.
- Service-settings test proving absence of the bot token.

## Operational considerations

Document reverse-proxy/TLS trust, readiness, bounded in-memory sessions, process restart behavior,
sanitized metrics, and a disabled-by-default Compose rollout for T025.

## Risks

Web compromise, CSRF, token replay, callback forgery, and accidental bot-token exposure require an
explicit threat-model review before ADR-035 moves from proposed to accepted.

## Definition of done

ADR-035 is resolved, the disabled least-privilege boundary and security tests exist as justified,
and no login, provider, subscription activation, or public endpoint is enabled.
