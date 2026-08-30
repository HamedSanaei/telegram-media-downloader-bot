# T023 - VIP purchasing and account Telegram UX

**Status:** planned

## Goal

Provide the Persian `/vip` account dashboard, plan/order presentation, subscription status, and
Instagram connection actions over the provider-neutral services.

## Why

Users need one coherent product surface, while authorization and economic state must remain in
application services rather than button text, FSM state, or provider-specific handlers.

## Dependencies

- T014 entitlement/subscription views and authorization.
- T015 payment orders/gateway registry.
- T018 Instagram connection lifecycle.
- T016 secure web handoff/callback boundary.

## Scope

- Add an all-user `/vip` owner-bound inline dashboard without introducing a persistent regular-user
  keyboard or changing the shared URL-download entry point.
- Show Free/active/expired status, UTC-derived localized expiry, available enabled plans, buy/renew,
  current pending order, and connected/expired/challenged/disconnected Instagram state.
- Provide connect/reconnect/disconnect actions to Free and VIP users.
- Create checkout only when an enabled plan and registered gateway exist; otherwise show a stable
  unavailable message and create no unusable order.
- Add reusable VIP-required, connection-required, and relogin buttons/messages to private/public
  policy failures.
- Add role-authorized admin views/actions for aggregate VIP/payment counts, sanitized credential
  health, operational subscription disable, and compromised-session revoke.
- Audit every admin economic/credential mutation.

## Non-goals

- No real provider adapter, price invention, direct SQL administration, Telegram payment secret,
  private media logic, or new admin-specific download path.

## Architecture

Telegram handlers call entitlement, plan-catalog, billing, and credential-account application ports.
Callback payloads are versioned, opaque, owner-bound, expiring, and below Telegram's 64-byte limit.
Every callback reauthorizes ownership/role and reloads durable state; displayed keyboard/state is
never trusted. Provider checkout URLs are returned by a registered gateway adapter.

## Data and persistence changes

Use T014/T015/T017 records. If a short-lived dashboard token is needed, store only opaque owner,
purpose, creation, and expiry using the existing selection-style contract and purge on expiry.
Admin audit contains actor, action, target internal ID, stable reason, and UTC time—no cookie,
transaction reference, provider payload, username, or payment secret.

## Security requirements

- Reauthorize every callback and admin action against current settings/state.
- Never display raw provider references, callback data, credential values, upstream errors, or
  Instagram usernames.
- Browser redirects display status only; payment confirmation remains server verified.
- Disconnect/revoke confirmations are owner/role bound and clear stale FSM state.

## Failure semantics

Backend unavailable, no plans, no provider, order pending, order expired/failed, entitlement expired,
session expired/challenged, and access denied have distinct stable Persian presentation. Telegram
edit failure may send a replacement message; it cannot repeat an economic mutation.

## Migration and backward compatibility

`/vip` is additive. Existing `/start`, `/menu`, admin management, URL handling, and callbacks retain
their behavior. With no plans/provider, the dashboard remains informational and connection remains
available.

## Telegram UX

Required screens/actions:

- `⭐ VIP` status and expiration date;
- available plans and buy/renew;
- pending/failed/expired payment status;
- Instagram connected/expired/challenged/disconnected state;
- connect, reconnect, and confirmed disconnect;
- Free private-content offer with buy and connect actions;
- active VIP/no Instagram connection prompt;
- expired Instagram session relogin prompt.

Follow existing Persian text organization and reusable keyboard/rendering conventions.

## Acceptance gates

- Free users can connect without purchasing and cannot access private media.
- No provider/plan creates no payment order and presents a stable unavailable state.
- Duplicate/replayed callbacks cannot duplicate orders, grants, revokes, or admin actions.
- Admin views are aggregate/sanitized and every mutation is audited.

## Tests

- Free/active/expired/renewed/stacked status rendering and localized expiry.
- Empty/disabled plan catalog, no gateway, pending/paid/failed/expired/refunded orders.
- Connect/reconnect/disconnect for Free and VIP.
- Owner tampering, callback expiry/replay, forged button text, and role changes.
- Telegram edit failure/idempotent resend around order creation and admin mutations.
- Exact Persian gating actions for the complete public/private matrix.

## Operational considerations

Document support flows for pending payments and session challenges. Aggregate reports must exclude
high-cardinality identities and must not mutate audit records.

## Risks

Telegram retries and stale callbacks can repeat economic actions. Opaque tokens, durable idempotency,
and service-level reauthorization are mandatory.

## Definition of done

Complete Persian `/vip`/admin UX, owner/role/idempotency tests, provider-unavailable behavior,
documentation, and gates pass; production purchase remains unavailable until T024.
