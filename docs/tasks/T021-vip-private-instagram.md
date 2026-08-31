# T021 - VIP private Instagram media

**Status:** implemented locally

## Goal

Allow active VIP users to download private/restricted Instagram posts, Reels, Stories, Highlights,
and mixed media only through their own connected account and only when that account already has
legitimate visibility.

## Why

Using the operator account for private authorization could leak its follows or Close Friends access
across Telegram users. Private access therefore requires a separate fail-closed policy.

## Dependencies

- T020 credential policy, content scope, and typed attempt orchestration.
- T014 `instagram_private_media` capability snapshot.
- T018/T019 connected user session and explicit resolver.

## Scope

- Authorize private media only with active VIP capability plus a healthy connected user credential.
- Use `USER_ONLY` for inspection, reinspection, gallery images, yt-dlp video children, retries, and
  every Story/Highlight/mixed-media operation.
- Permit only content the connected account can already see as an accepted follower or legitimate
  Close Friends participant.
- Deny Free users even when they connected Instagram; offer VIP without using their session to
  reveal content.
- Prompt VIP users without a session to connect.
- Map legitimate no-access to a project category equivalent to `USER_PRIVATE_ACCESS_DENIED` and
  expired/challenged state to reconnect guidance.
- Fail closed when privacy detection or normalized scope is ambiguous.

## Non-goals

- No follower approval bypass, access-control circumvention, DRM behavior, account takeover,
  operator fallback, or guarantee that a URL exists.

## Architecture

Private authorization occurs in application policy before a download job is accepted. The durable
job carries the accepted capability snapshot, owner, user credential generation, `USER_ONLY`, and
restricted scope. Worker/router/adapters cannot resolve `OPERATOR_PUBLIC` for that job. New callbacks
after VIP expiry reauthorize; an already accepted job may finish unless the credential is revoked.

## Data and persistence changes

Use T014/T020 nullable safe job fields. Persist only scope/category, never private metadata beyond
the existing canonical URL and normalized media contract. Do not store follower graphs, usernames,
raw access responses, or Instagram account identifiers.

## Security requirements

- Assert owner equality at policy, resolver, worker, and adapter-facing test boundaries.
- Operator credential resolution for `USER_ONLY` is an impossible/typed invariant violation.
- Generic user responses do not distinguish deleted, nonexistent, private-no-access, or ambiguous
  content beyond what their connected session legitimately established.
- Preserve URL safety, size, cancellation, cleanup, delivery uncertainty, and source-link behavior.

## Failure semantics

- Free -> VIP-required response.
- VIP/no credential -> connection-required response.
- Expired/challenged -> reconnect response and per-user state update.
- Healthy but not authorized -> private-access-denied response explaining accepted-follow status.
- Unknown/malformed/upstream-change -> fail closed; no fallback.
- Local/download/delivery failures retain their current categories and never alter credential policy.

## Migration and backward compatibility

Private access is disabled by default. Existing public jobs/configurations remain unchanged. No Free
user is forced to connect or create a subscription/credential row.

## Telegram UX

Use Persian messages/actions equivalent to:

- `دانلود محتوای خصوصی مخصوص کاربران VIP است` with buy/connect actions for Free users;
- active VIP but no session -> connect action;
- expired/challenged -> reconnect action;
- access denied -> connected account must already be an accepted follower.

Messages never confirm private content existence beyond the connected account's observed result.

## Acceptance gates

- Every private/restricted code path is `USER_ONLY`; operator fallback count is exactly zero.
- The full private rows of the credential matrix pass for posts, Reels, Stories, Highlights, Close
  Friends, and mixed media.
- Free connection remains allowed but confers no private capability.

## Tests

- Free + connected -> denied/VIP offer.
- VIP + no session -> connect prompt.
- VIP + follower -> allowed; VIP + not follower -> denied.
- VIP + expired/challenged -> reconnect; revoked mid-queue -> fail before decrypt/download.
- Every user-session failure -> zero operator resolution/calls.
- Story/Highlight/mixed child calls preserve the same user/generation.
- Ambiguous privacy and existence-leak regression tests.

## Operational considerations

Document platform terms, user consent, abuse response, rate limits, private-data minimization, and
the inability to guarantee session stability.

## Risks

Cross-user or operator permission leakage is a release blocker. T025 must independently verify
resolver call traces, logs, durable state, and end-to-end fixtures before activation.

## Definition of done

The application private-media policy is implemented locally with explicit VIP, connection,
reconnect, visibility, and fail-closed decisions. Every allowed decision is `USER_ONLY`; adapter
and worker wiring plus full media-kind acceptance remain gated by T022-T025.
