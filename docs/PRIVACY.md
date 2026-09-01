# Privacy

This document describes how the operator logger handles user data. It is the
authoritative privacy/transparency reference for the operator logger feature.

## Non-blocking disclosure (since v1.4.0-rc.2)

Submission mirroring is an **operator-enabled audit feature**. When the operator
enables the logger, enables `submission_mirror_enabled`, and explicitly attests
the policy with `operator_privacy_attested: true`, accepted download submissions
(URL text, photos, videos, documents, audio, animations, captions, and media
groups) may be copied to the configured private operational logger channel and
retained indefinitely.

**Users are never required to acknowledge this policy.** There is no blocking
privacy prompt in the download path, no acknowledgement button, and no
per-user acknowledgement record consulted before a download is accepted. A
download is never rejected, delayed, or interrupted because the user has not
acknowledged anything.

The transparency surface is informational only:

- The `/privacy` command in the bot replies with a short Persian disclosure:
  some download requests may be recorded in a private operational channel for
  security, support, and error review; login information, passwords, 2FA codes,
  and other sensitive data are never recorded in the logger.
- No acknowledgement is requested, and the disclosure is never sent before
  every download.

## Runtime contract

Accepted-submission mirroring is active only when **all three** operator
settings are true:

```yaml
telegram:
  logger:
    enabled: true
    submission_mirror_enabled: true
    operator_privacy_attested: true
```

- `logger.enabled: false` disables the whole logger (alerts and mirror).
- `submission_mirror_enabled: false` disables only the mirror.
- `operator_privacy_attested: false` disables the mirror even when the other
  two flags are true (fail closed).
- Configuring a channel alone never activates mirroring.
- `privacy_notice_version` is retained for backward compatibility; it is **not**
  consulted at runtime and no per-user acknowledgement is stored or required.

## What is never logged

The central sanitizer rejects and redacts, among other things:

- bot tokens
- `Authorization` headers and cookies (including Netscape cookie records)
- Instagram passwords, 2FA/checkpoint codes, sessions, and vault keys
- payment secrets, callback signatures, and provider transaction references
- proxy credentials
- raw exception text

The approved numeric Telegram user id remains present in private audit
metadata for operations; it is intentionally not anonymized away.

## Retention

Retention is **indefinite**. There is no automatic deletion, no 30/90-day
purge, and no retention worker. Operators who want a different policy must
implement and document it explicitly.

## The fact that the service is free is not a security or privacy exemption.

All the guarantees in this document apply equally to free and paid users.
