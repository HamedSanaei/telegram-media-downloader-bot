# T017 - Encrypted per-user Instagram credential vault

**Status:** implemented

## Goal

Add an owner-bound encrypted Instagram session store, lifecycle, lease, and context-managed Netscape
export without changing media credential selection.

## Why

Per-user cookies are authentication secrets. They cannot share the operator cookie file, ordinary
job tables, Redis, logs, or a persistent plaintext per-user directory.

## Dependencies

- T016 secure companion boundary and accepted ADR-035.
- Proposed ADR-033 must be accepted during this task.
- Existing SQLite/WAL and job workspace/cleanup contracts.

## Scope

- Add `InstagramCredential`, `InstagramCredentialState`, encrypted envelope, event, and generation
  models plus repository/lease ports.
- Use AES-256-GCM with a random 96-bit nonce, envelope version, key ID/version, and associated data
  binding provider, credential ID, owner user ID, and generation.
- Configure one active encryption key and retained decrypt-only keys for rotation in ignored,
  least-privilege YAML; never commit a key.
- Support `CONNECTED`, `EXPIRED`, `CHALLENGE_REQUIRED`, `REVOKED`, and `DISCONNECTED`.
- Enforce one current credential identity per Telegram owner and monotonically increasing generation.
- Erase ciphertext immediately on disconnect/revoke while retaining only sanitized lifecycle audit.
- Add an atomic, expiring one-job lease per credential/generation.
- Provide a context-managed Netscape exporter that writes only inside the supplied job workspace,
  applies restrictive permissions, and removes plaintext on every exit path.

## Non-goals

- No password/2FA capture, Telegram connection button, adapter integration, public fallback, or
  private download.

## Architecture

```text
InstagramCredentialRepository -> encrypted envelope
CredentialResolver/materializer -> exact job workspace/cookies.txt -> bounded context lifetime
```

Only the vault adapter sees ciphertext/key bytes. The application sees safe metadata/state. A
lease acquisition checks owner, generation, non-revoked state, and expiry atomically before any
decryption. Decryption occurs as late as possible and buffers are released immediately after export.

## Data and persistence changes

| Entity/field group | Purpose and sensitivity | Constraints/indexes | Retention/compatibility |
|---|---|---|---|
| credential ID/owner/provider/state/generation | Safe lifecycle metadata; owner is personal data | unique active owner/provider; owner/state index | current row retained; no row required for Free users |
| envelope version/key ID/nonce/ciphertext | Authentication secret | never indexed/searched/logged | ciphertext erased immediately on revoke/disconnect |
| last verified/success/failure category timestamps | Sanitized health evidence | owner/state/time indexes only as needed | current projection retained |
| credential events | Sanitized audit without upstream text | credential/time index | purge after 90 days |
| credential leases | Job/generation/expiry coordination | unique active credential; expiry index | purge within 24 hours after expiry |

## Security requirements

- Associated-data mismatch, unknown key, corrupt tag, wrong owner, wrong generation, and revoked
  state fail closed before materialization.
- Cookie values, domains, usernames, paths, ciphertext, nonces, and key IDs stay out of logs/metrics
  unless a key ID is needed in a tightly controlled operator rotation report.
- Plaintext files use mode `0600` on POSIX and a current-user-only ACL strategy on Windows.
- No shared per-user cookie directory, username lookup, or global last-connected account.

## Failure semantics

Use stable categories for missing, expired, challenged, revoked, generation mismatch, lease busy,
decrypt failure, and local materialization failure. Cryptographic/local errors never become an
operator-cookie fallback reason.

## Migration and backward compatibility

Add empty tables and optional settings. No key is required while user credentials are disabled.
Existing canonical operator cookies are untouched. Migration never imports plaintext operator
cookies into the user vault.

## Telegram UX

None. Expose safe state/timestamps for T018/T023 without upstream error text.

## Acceptance gates

- Alice's repository/materializer can never resolve Bob's credential.
- No plaintext survives normal return, exception, cancellation, timeout, process cleanup, or sweep.
- Rotation can decrypt old envelopes and re-encrypt under the active key without losing state.
- Disconnect/revoke makes previous ciphertext and generation unusable immediately.

## Tests

- Encrypt/decrypt round trip, nonce uniqueness, associated-data tamper, corrupt tag, and unknown key.
- None/connected/expired/challenged/revoked/disconnected/reconnected state transitions.
- Wrong owner/generation and cross-user isolation.
- Lease contention, expiry, restart, and stale-release behavior.
- POSIX/Windows permission strategy and cleanup across success/failure/cancellation.
- Key-rotation batch rollback and mixed-key recovery.

## Operational considerations

Document key generation, storage, backup, restore, rotation, loss consequences, and a doctor check
that validates key availability without decrypting/logging credentials.

## Risks

Master-key loss makes sessions unrecoverable; compromise exposes all encrypted sessions. T025 must
exercise private backups, rotation, recovery, and incident revocation.

## Definition of done

ADR-033, typed vault/lease contracts, additive persistence, encryption/materialization adapters,
security tests, key operations, and full repository gates pass without routing media through them.

## Implementation notes

ADR-033 is accepted. The owner-bound encrypted vault and its lifecycle/boundary are implemented in
the working tree without routing media through them:

- `domain/instagram_credentials.py` — `InstagramCredential`, the five lifecycle states, monotonic
  per-owner `generation`, versioned `CredentialEnvelope` (base64 JSON), sanitized `CredentialEvent`
  and `CredentialLease`, plus `aad_for` binding provider/credential/owner/generation.
- `application/ports/instagram_credentials.py` — `VaultKeyStore`, `EnvelopeCryptor`,
  `InstagramCredentialRepository`, and `CredentialMaterializer` contracts.
- `infrastructure/credentials/envelope.py` — AES-256-GCM via `cryptography` with a random 96-bit
  nonce per encryption and bound associated data; `InvalidTag`/`ValueError` map to a typed
  `CredentialDecryptError`.
- `infrastructure/credentials/key_ring.py` — `VaultKeyRing` (one active + decrypt-only keys) and
  the application-facing `CredentialCryptor` that looks up keys by envelope key ID and fails
  closed on a missing key.
- `infrastructure/credentials/materializer.py` — `RestrictedCookieMaterializer` acquires an
  atomic expiring lease, decrypts late, writes `cookies.txt` inside the exact job workspace with
  mode `0600`, and removes the file and releases the lease on every exit path.
- `infrastructure/persistence/sqlite_instagram_credentials.py` — additive WAL tables for
  credentials (one active row per owner, unique on `(owner, provider)`), sanitized 90-day events,
  and expiring leases; ciphertext lives only as a bounded base64 envelope string and is never
  searched or logged.
- `application/services/credential_vault.py` — `CredentialVault` lifecycle: connect/re-connect
  (generation++), expiry/challenge markers, disconnect/revoke (immediate ciphertext erase), and
  admin key-rotation re-encryption; plaintext never crosses this layer.
- `bootstrap/config.py` gains a strict `vault` key-ring section; `config.example.yaml` documents
  it with no keys configured (disabled).

Tests cover round-trip, nonce uniqueness, associated-data tamper, corrupt tag, unknown/missing
key, state transitions, cross-user isolation, single-lease contention, generation mismatch,
expiry/challenge/revoke/disconnect blocks, cleanup on success/error/cancel, rotation/mixed-key
recovery, restrictive POSIX permissions, and workspace containment. No password, 2FA code, raw
cookie value, nonce, or key ID is durable or logged. Media routing, public fallback, and private
downloads remain not implemented (T019-T021).
