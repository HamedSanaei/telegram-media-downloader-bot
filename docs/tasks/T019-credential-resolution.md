# T019 - Credential resolution and adapter integration

**Status:** planned

## Goal

Introduce explicit credential contexts across application, worker, router, gallery-dl, and yt-dlp
boundaries while preserving the current canonical operator cookie behavior.

## Why

Both media adapters currently read one global effective cookie path from settings. Per-user
sessions require an explicit, owner-safe attempt context, but adapters must not learn VIP policy.

## Dependencies

- T017 credential vault/materializer and lease.
- Existing T003/T013 adapter boundaries and ADR-002/027/028/030.
- Proposed ADR-034 must be accepted during this task.

## Scope

- Add typed `CredentialKind`, `CredentialPolicy`, safe credential references, and an ephemeral
  engine credential context supporting `NONE`, `OPERATOR_PUBLIC`, and `USER_INSTAGRAM`.
- Extend engine inspection/download ports and routed mixed-media calls to receive the same explicit
  context for every child attempt.
- Make gallery-dl command construction and yt-dlp options use only the supplied materialized file;
  remove implicit per-attempt reads of global user credential state.
- Keep the existing canonical cookie file as the operator credential.
- Add an explicit operator action that verifies the Instagram account follows zero accounts and
  attests it as `OPERATOR_PUBLIC`.
- Bind attestation to the generation/keyed verifier of only the canonical file's Instagram records;
  an Instagram cookie replacement or external change invalidates it.
- Materialize user cookies inside the exact job workspace with T017 permissions/cleanup.
- Persist/queue only owner ID, credential kind/policy, credential generation, operator generation,
  and safe attempt state.

## Non-goals

- No entitlement selection, public fallback, private authorization, account login, or provider
  health polling.

## Architecture

Business policy chooses a safe reference. `CredentialResolver` checks owner/generation/state,
acquires the lease, and yields a short-lived materialized credential. The worker invokes the engine
inside that context. The router passes one context unchanged to gallery-dl and every yt-dlp child,
so mixed posts cannot combine credentials.

Adapters receive no `Subscription`, `Capability`, or VIP flag. Existing upstream error mappers may
interpret vendor output centrally, but credential-switch policy never matches exception strings.

## Data and persistence changes

| Field group | Purpose and sensitivity | Constraints/retention |
|---|---|---|
| job credential policy/kind/owner/generation | Safe reproducible attempt selection; owner is personal | nullable additive fields retained with job |
| operator Instagram generation/attestation time/actor | Public-only readiness | one current record; invalidated on Instagram cookie change |
| keyed verifier | Detect changed Instagram cookie subset; secret-adjacent | never logged/exported; retained until generation replacement |

No cookie value/path/ciphertext is placed in `JobRecord`, ARQ arguments, selection JSON, or metrics.

## Security requirements

- Resolver requires `job.user_id == credential.owner_user_id` for every user credential.
- A worker cannot guess by username or enumerate another user's credential.
- Public-only attestation requires a real explicit operator validation reporting zero followed
  accounts; unverifiable state fails closed.
- No scheduled validation request is added; ADR-030's passive health rule remains.
- Child extraction, cancellation, retry, and cleanup preserve one credential context.

## Failure semantics

Distinguish no credential, owner mismatch, generation mismatch, revoked/expired/challenged, lease
busy, operator unattested/stale, operator expired, materialization local failure, and adapter auth
failure. No switch is performed in this task.

## Migration and backward compatibility

Legacy jobs and disabled user-credential features resolve the existing operator path exactly as
today. New fields are nullable/defaulted. Operator attestation is required only before the future
authenticated-user routing feature is enabled.

## Telegram UX

Add only sanitized operator attestation/invalidation results to the private role-authorized admin
panel. User connection and download messages belong to T018/T020/T021/T023.

## Acceptance gates

- Every media-engine call has one explicit credential context.
- Mixed Instagram inspection/download/yt-dlp child resolution cannot change credential implicitly.
- Canonical operator cookie management and passive health continue to work.
- Replacing Instagram records invalidates public-only attestation without exposing a verifier.

## Tests

- No/operator/user context for both adapters and router-owned mixed media.
- Cross-user and wrong-generation resolution failures.
- Job-scoped cookie permissions and cleanup for success/failure/cancellation/retry.
- Operator zero-follow validation, unverifiable/nonzero rejection, generation invalidation, and
  unrelated non-Instagram cookie replacement.
- Legacy job/configuration behavior remains operator-backed.

## Operational considerations

Document creation of a dedicated zero-follow operator account, explicit attestation, replacement,
incident invalidation, and doctor readiness. Do not promise automated verification if upstream no
longer exposes a trustworthy following count.

## Risks

An over-privileged operator account could leak private permissions. T020/T021/T025 must refuse
activation without current zero-follow attestation and explicit public classification.

## Definition of done

ADR-034 is accepted, explicit resolver/engine contracts and adapter tests pass, operator compatibility
is preserved, and no fallback or private behavior is enabled.
