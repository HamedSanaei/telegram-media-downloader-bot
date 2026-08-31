# T015 - Provider-neutral billing and payment foundation

**Status:** implemented

## Goal

Add a provider-neutral payment order/attempt model whose confirmed transaction creates exactly one
entitlement grant, without selecting or integrating a real gateway.

## Why

Gateways retry callbacks and browser redirects are untrusted. Economic state must be durable,
auditable, and idempotent before provider-specific code or Telegram purchasing UX exists.

## Dependencies

- T014 subscription plans, grants, entitlement recomputation, and SQLite repositories.

## Scope

- Add `PaymentOrder`, `PaymentAttempt`, `PaymentStatus`, `PaymentProviderId`,
  `ProviderTransactionReference`, verified callback result, and checkout result models.
- Define `PaymentGateway.create_payment`, `verify_callback`, and `query_payment` ports using only
  project-owned request/result types.
- Resolve gateway adapters through composition, never provider-name `if/elif` chains.
- Snapshot plan ID, duration, capability set, amount, and currency on each order.
- Implement atomic confirmation: transaction uniqueness, order validation, status transition, and
  one entitlement grant in the same `BEGIN IMMEDIATE` SQLite transaction.
- Implement typed cancellation, expiry, refund, and reversal accounting.
- Supply a fake/in-memory gateway only for deterministic tests.

## Non-goals

- No real provider credentials, HTTP calls, callback route, checkout button, pricing, or enabled
  production plan.
- No card, CVV, banking credential, or provider secret storage.

## Architecture

Gateway adapters authenticate and normalize provider messages. The billing application service
accepts only a verified project result and checks order identity, expected amount/currency, allowed
state transition, and provider transaction uniqueness. A browser redirect may display status but
can never confirm payment.

Statuses are constrained to `CREATED`, `PENDING`, `PAID`, `FAILED`, `CANCELLED`, `EXPIRED`, and
`REFUNDED`. Invalid or backward transitions fail without mutating an entitlement.

Refund/reversal marks the corresponding immutable grant reversed, then recomputes every remaining
grant in confirmation order using T014's stacking rule. If no paid time remains, access ends now;
historical financial rows are not deleted or rewritten.

## Data and persistence changes

| Entity/field group | Purpose and sensitivity | Constraints/indexes | Retention/compatibility |
|---|---|---|---|
| `payment_orders` owner, plan snapshot, amount/currency, status, expiry | Financial/audit data | unique order ID; owner/status/created/expiry indexes | retained indefinitely |
| `payment_attempts` order/provider/status/timestamps/safe failure code | Provider interaction audit; no raw payload | ordered per order; provider/status index | retained indefinitely |
| provider transaction reference | Exactly-once economic identity | unique `(provider, reference)` | retained indefinitely; never reused |
| confirmation/refund timestamps and verified amount/currency | Server-verified audit evidence | checked in atomic transaction | retained indefinitely |

Raw callbacks, request headers, signatures, redirect parameters, and provider secrets are not stored.
Any bounded audit digest must be keyed/non-reversible and justified in the schema field catalog.

## Security requirements

- Treat callbacks as untrusted until the selected adapter verifies signature and freshness.
- Compare expected and verified order, amount, and currency inside the transaction.
- Protect against replay and concurrent duplicate callbacks.
- Keep provider transaction IDs out of logs, metrics labels, and Telegram messages.
- All admin refund/cancellation actions are role-authorized and durably audited.

## Failure semantics

- Provider timeout leaves an order pending/unknown and never grants access.
- Wrong amount/currency/order, unknown transaction, expired order, replay, and invalid transition
  produce distinct stable categories.
- Persistence uncertainty returns an unknown/pending outcome; it never retries an economic grant
  outside the idempotent transaction.

## Migration and backward compatibility

Add empty tables/indexes idempotently. Existing installations and Free users receive no orders or
plans. Billing remains disabled while no provider is registered and no plan is enabled.

## Telegram UX

None in this task. Return safe order/status views for T023.

## Acceptance gates

- One verified transaction can create at most one grant under sequential, duplicate, concurrent,
  restart, and retry execution.
- No redirect-only confirmation and no provider-specific branch in domain/application code.
- Payment records are excluded from media retention/purge paths.

## Tests

- Created/pending/paid/failed/cancelled/expired/refunded transitions.
- Duplicate and concurrent callback delivery; process restart between verification and handling.
- Wrong amount, currency, order, provider, unknown reference, expired order, timeout, and replay.
- Atomic rollback when grant creation or order transition fails.
- Refund/reversal recomputation with earlier and later stacked grants.

## Operational considerations

Provide aggregate order counts by status/provider and reconciliation queries for pending/unknown
orders. Provider identifiers are bounded labels; transaction/order/user identifiers are never labels.

## Risks

Callback forgery, duplicate grants, chargebacks, and partial transactions are mitigated here and in
T016/T024/T025.

## Definition of done

Provider-neutral types, ports, repositories, atomic services, fake-adapter tests, audit/retention
documentation, and full gates pass with no real checkout integration.
