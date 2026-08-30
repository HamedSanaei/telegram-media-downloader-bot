# T024 - First real payment gateway adapter

**Status:** blocked - payment provider not selected

## Goal

Implement the first real payment gateway strictly behind T015's provider-neutral port after the
operator selects a provider and supplies its authoritative integration contract.

## Why

Provider credentials, signing, callback fields, settlement, expiry, and refund behavior cannot be
invented safely. The rest of the milestone must not couple itself to an unknown gateway.

## Dependencies

- T015 billing foundation and idempotent confirmation.
- T016 callback boundary.
- T023 purchasing/status UX.
- **Blocker:** operator-selected provider, credentials, sandbox access, callback documentation,
  currencies/amount rules, and refund/reversal capabilities.

## Scope

Once unblocked:

- implement `infrastructure/payment/<provider>/` for checkout creation, callback verification, and
  status query;
- add strict least-privilege ignored configuration and secret redaction;
- implement provider signing/verification, timestamp/nonce/replay rules, timeouts, safe retry, and
  normalized typed errors;
- verify server-side order, amount, currency, provider transaction reference, and final status;
- use T015's atomic confirmation and refund/reversal services;
- add sandbox fixtures/contracts and operator runbooks.

## Non-goals

- No provider selection in code, browser-redirect trust, provider-specific domain/application type,
  card/CVV storage, secret in environment/URL/logs, or change to Instagram credential policy.

## Architecture

The adapter parses and cryptographically verifies raw provider input, then emits project-owned
verified results. T015 owns economic state; T016 owns HTTP; T023 owns presentation. Adding a second
provider must require only a new adapter/configuration/UI selection, not changes to subscriptions or
Instagram policy.

## Data and persistence changes

Use T015 tables. Add only provider-required safe references/status evidence after documenting each
field's purpose, sensitivity, uniqueness, index, retention, and replay implications. Raw payloads,
headers, signatures, credentials, card data, and callback URLs are not durable.

## Security requirements

- Credentials in ignored least-privilege YAML.
- Constant-time signature verification and provider-defined replay protection.
- No trust in browser redirect; server callback/query verification required.
- Exact amount/currency/order matching and unique provider transaction reference.
- Safe timeouts/retries; refunds/reversals only from verified provider evidence or audited admin
  procedure supported by the provider.

## Failure semantics

Map invalid signature, replay, wrong amount/order/currency, timeout, unavailable provider, unknown
transaction, pending, failed, expired, refunded, and unsupported reversal to stable project types.
Uncertainty never activates or extends VIP.

## Migration and backward compatibility

Provider configuration is optional and disabled by default. Existing deployments and other future
adapters remain unaffected. No provider field becomes mandatory in general subscription state.

## Telegram UX

T023 renders the normalized checkout/status. Do not expose raw provider errors or references.

## Acceptance gates

- Provider documentation and sandbox behavior are cited in the implementation review.
- Duplicate/concurrent callbacks create one grant.
- Wrong/forged/replayed callbacks create none.
- Timeout/reconciliation/refund behavior matches the provider contract.

## Tests

- Signed success/pending/failure/expiry/refund fixtures.
- Invalid signature, timestamp, nonce/replay, amount, currency, order, and reference.
- Duplicate/concurrent callback, restart, provider timeout, query reconciliation, and safe retry.
- Secret scans and redacted logs/errors/metrics.

## Operational considerations

Document sandbox-to-production switch, credential rotation, callback URL/TLS, alerting, settlement
reconciliation, incident disable, refunds, and provider outage behavior.

## Risks

Provider contract uncertainty is the reason this task remains blocked. Do not weaken its gates to
advance the roadmap.

## Definition of done

The blocker is resolved explicitly, one reviewed adapter passes sandbox/security/idempotency gates,
operations docs are complete, and no core entitlement/Instagram policy branch names the provider.
