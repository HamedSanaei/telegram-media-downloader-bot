# T014 - VIP entitlement and subscription domain

**Status:** implemented

Implements the provider-neutral VIP entitlement and subscription foundation: typed plans,
capabilities, immutable grants, a durable subscription projection, deterministic UTC calendar-month
arithmetic with end-of-month clamping, reversal recomputation, a fail-closed
`EntitlementService.authorize()`, additive SQLite/WAL tables, and a nullable job entitlement
snapshot with child-job inheritance. No payment provider, Instagram credential, VIP UI, or pricing
was introduced; the plan catalog is empty by default.

## Goal

Introduce the provider-neutral subscription and entitlement foundation for the product-facing VIP
feature. No payment provider, Instagram credential, or Telegram purchasing UI is implemented here.

## Why

`UserProfile.is_premium` describes Telegram's own Premium flag and must never become bot VIP state.
Future paid behavior needs durable, typed capabilities and deterministic time semantics without
scattering `if user.vip` checks through handlers and workers.

## Dependencies

- Existing SQLite/WAL ownership from T009 and ADR-007/017/023.
- Existing `AccessPolicyService`, `JobService`, `JobRecord`, queue, and recovery contracts.
- No new milestone task dependency.

## Scope

- Add project-owned `SubscriptionPlan`, `Subscription`, `EntitlementGrant`,
  `SubscriptionStatus`, `Capability`, and `EntitlementSnapshot` models.
- Support any positive integer `duration_months`; do not hardcode a fixed 1/3/6/12-month catalog.
- Store price in integer minor units and currency as a validated uppercase currency code.
- Model at least `instagram_private_media` and `instagram_user_session_preference` capabilities.
- Add subscription/entitlement application ports and services.
- Add additive, idempotent SQLite migrations and repository adapters.
- Authorize protected requests before durable acceptance and persist a safe snapshot on the job.
- Let an accepted job finish after subscription expiry; reject every new protected request after
  expiry. Automatically created child jobs inherit the accepted parent snapshot, while a later
  user callback is a new authorization.

## Non-goals

- No prices, enabled commercial plans, payment order, provider, checkout, or Telegram VIP menu.
- No Instagram credential storage or media-routing change.
- No unrelated higher-limit capability behavior.

## Architecture

`EntitlementService.authorize(user_id, capability, accepted_at)` returns an immutable project-owned
snapshot or raises a typed denial. Application services call it; Telegram presentation only maps
the result to Persian UX. The domain has no aiogram, SQLite, Redis, or provider dependency.

Calendar-month arithmetic is UTC based and clamps to the last valid day of the destination month.
For each non-reversed grant in confirmation order:

```text
grant_start = max(grant.confirmed_at, preceding_expiry)
grant_expiry = add_calendar_months(grant_start, grant.duration_months)
```

This preserves paid time during renewal and supplies the deterministic recomputation used by T015.

## Data and persistence changes

| Entity/field group | Purpose and sensitivity | Constraints/indexes | Retention/compatibility |
|---|---|---|---|
| `subscription_plans` identity/name/duration/price/currency/enabled | Operator-owned catalog; price is financial, not secret | unique immutable plan ID; enabled index; positive months/minor units | retained indefinitely; table starts empty |
| plan capabilities | Typed entitlements granted by a plan | unique `(plan_id, capability)` | retained with the plan |
| `subscriptions` user/status/derived expiry | Current account projection | one current subscription per user; user/status/expiry indexes | retained indefinitely for audit |
| `entitlement_grants` order/source/duration/confirmed/reversed timestamps | Immutable economic grant ledger | unique source/order reference; user/confirmation index | retained indefinitely; reversal never deletes a grant |
| job entitlement snapshot | Capability, accepted time, grant reference, authorized expiry | safe nullable additive job columns | legacy jobs remain readable; contains no payment or credential secret |

Document every concrete field with purpose, sensitivity, index, uniqueness, retention, and migration
behavior before implementing the migration.

## Security requirements

- Fail closed when the entitlement repository is unavailable.
- Do not trust Telegram callback state as authorization.
- Do not expose user IDs or grant/order identifiers as metric labels.
- Keep subscription mutations behind application ports and role-authorized admin actions.

## Failure semantics

- Distinguish inactive, expired, cancelled, reversed, capability-missing, and backend-unavailable.
- Clock input is injectable for deterministic tests; application code never uses local time.
- An expired snapshot is valid only for the already accepted job that owns it.

## Migration and backward compatibility

- Existing databases gain empty tables and nullable job fields without rewriting or deleting rows.
- Free users require no subscription row.
- Old configurations need no new key while VIP features are disabled.
- Older jobs retain their current public behavior and cannot acquire VIP capability retroactively.

## Telegram UX

None in this task. Expose typed presentation data for T023; do not add buttons or messages yet.

## Acceptance gates

- The plan catalog is empty by default and no commercial value is invented.
- No business branch reads `UserProfile.is_premium` to decide bot VIP access.
- Stacking, expiration, cancellation, and reversal are deterministic and UTC-only.
- Durable acceptance and child-job inheritance follow the documented snapshot rule.

## Tests

- Free, active, expired, cancelled, capability-missing, and repository-failure authorization.
- First activation, renewal, stacked purchase, month-end/leap-year clamping, and clock boundaries.
- Accepted queued work after expiry versus new work after expiry.
- Automatic child snapshot inheritance and later callback reauthorization.
- Empty-database and legacy-database migration/round-trip coverage under WAL contention.

## Operational considerations

Expose aggregate active/expired counts only. Plan and grant data are durable control/audit state,
not media data, and must be included in normal SQLite backup and restore.

## Risks

- Clock or calendar errors can over/under-grant access; mitigate with injected clocks and boundary
  fixtures.
- Conflating Telegram Premium with VIP can grant unintended access; enforce separate types/tests.

## Definition of done

Typed domain, ports, additive migrations, repositories, services, tests, configuration compatibility,
architecture/ADR updates, and all repository gates pass without a payment or Instagram feature.
