# T020 - VIP public Instagram user-first fallback

**Status:** implemented locally

## Goal

For public Instagram media, prefer a healthy connected VIP user's session and switch exactly once
to the attested public-only operator credential after an eligible typed credential failure.

## Why

VIP users should benefit from their own authenticated session without losing resilient public
downloads when that session expires. A broad exception retry would hide real failures and could
cross privacy boundaries.

## Dependencies

- T014 entitlement snapshot and capabilities.
- T018 connected user credential lifecycle.
- T019 explicit credential contexts and public-only operator attestation.

## Scope

- Add `ContentAccessScope` values `PUBLIC`, `USER_RESTRICTED`, and `UNKNOWN` to normalized inspection.
- Add application policy equivalent to `OPERATOR_PUBLIC`, `USER_FIRST_PUBLIC_FALLBACK`, and
  `USER_ONLY` and persist its safe decision on the job.
- Select operator for every Free public request, even when a Free user has connected Instagram.
- Select operator for VIP public requests without a healthy user session.
- Select user first for VIP public requests with a healthy session.
- Permit one switch to operator only for typed session-expired/invalid/login-required/credential-
  rejected failures and only while content is not known restricted.
- Treat the dedicated zero-follow operator attempt as the public classifier: accept only an explicit
  `PUBLIC` result; unknown/unavailable/restricted output fails closed and is not delivered.
- Persist credential phase and `fallback_used` before switching so retry/restart cannot loop.
- Record bounded aggregate outcome metrics without identity labels.

## Non-goals

- No private download, arbitrary retry, cross-provider credential selection, or Free-user preference
  for their connected session.

## Architecture

The application orchestration owns the attempt sequence. Each attempt resolves one context and
invokes the existing inspection/download service. Adapters return normalized scope and typed errors;
they never choose another credential. Once scope is `USER_RESTRICTED`, policy becomes `USER_ONLY`
and operator resolution is structurally prohibited.

## Data and persistence changes

Add nullable safe job fields for policy, access scope, user/operator generation, current credential
phase, and fallback-used boolean. Include relevant fields in active-job idempotency so reconnecting
to a new generation cannot silently reuse an incompatible active job. No secret is durable.

## Security requirements

- Require current public-only operator attestation before any operator Instagram attempt.
- Never switch on filesystem, FFmpeg, output-schema, post-processing, size, Telegram delivery,
  cancellation, local runtime, or generic internal failure.
- Never switch after explicit `USER_RESTRICTED` classification.
- Do not expose whether unavailable/unknown content exists.

## Failure semantics

Eligible user credential failures transition the credential phase durably, then make one operator
attempt. Operator auth failure remains an operator-cookie failure and uses existing admin health
remediation. Noneligible errors retain their original category/stage. A failed fallback is terminal
for this request and cannot switch back to user.

## Migration and backward compatibility

Features are disabled by default. Legacy/Free public jobs use operator behavior. Existing job rows
with no policy fields receive the legacy operator policy on read/recovery.

## Telegram UX

Public fallback is normally transparent. If the user session becomes expired/challenged, show a
sanitized reconnect action after the public result or failure without revealing cookies or raw
errors. Operator-cookie failure remains an operator/admin concern.

## Acceptance gates

- The authoritative public rows of the milestone credential matrix are implemented exactly.
- At most one user-to-operator switch occurs across retry, restart, and Redis loss.
- Every non-credential error proves zero credential switches.
- Unknown/restricted operator results are never delivered.

## Tests

- Free/no session -> operator; Free/session -> operator.
- VIP/no session -> operator; VIP/healthy session -> user.
- Each eligible user auth failure -> operator exactly once.
- Every noneligible local/schema/postprocess/size/delivery/cancel failure -> no switch.
- Restart before/after persisted switch, Redis loss, and concurrent retry.
- Scope changes public -> restricted fail closed; restricted never operator.
- Metrics/logs contain only bounded policy/outcome/category labels.

## Operational considerations

Expose fallback rate/outcome and operator-attestation readiness. High fallback rates prompt user
reconnection guidance; they do not trigger scheduled Instagram probes.

## Risks

Incorrect failure or visibility classification can leak operator permissions or mask defects.
Central typed mapping, zero-follow attestation, one-switch persistence, and T025 E2E gates mitigate it.

## Definition of done

Public policy, scope vocabulary, one-switch state transition, durable recovery fields, and the
credential failure matrix are implemented locally. Private media and provider activation remain
disabled; full worker wiring and end-to-end gates continue in T021-T025.
