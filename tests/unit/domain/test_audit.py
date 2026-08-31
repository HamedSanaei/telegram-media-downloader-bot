from datetime import UTC, datetime

import pytest

from telegram_media_bot.application.services.audit_sanitizer import (
    UnsafeAuditPayloadError,
    sanitize_audit_message,
)
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    TelegramSourceReference,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import (
    deserialize_event,
    serialize_event,
)


def _event(**changes: object) -> AuditEvent:
    values: dict[str, object] = {
        "event_id": "event-1",
        "event_type": AuditEventType.SYSTEM_HEALTH,
        "category": AuditCategory.SYSTEM,
        "severity": AuditSeverity.INFO,
        "occurred_at": datetime(2026, 8, 31, 12, 30, tzinfo=UTC),
        "correlation_id": "request-1",
        "message": "سامانه سالم است",
        "telegram_user_id": 42,
        "provider": "instagram",
    }
    values.update(changes)
    return AuditEvent(**values)  # type: ignore[arg-type]


def test_categories_and_severities_are_closed_typed_sets() -> None:
    assert {item.value for item in AuditCategory} == {
        "error",
        "cookie_health",
        "user_submission",
        "system",
    }
    assert {item.value for item in AuditSeverity} == {
        "info",
        "warning",
        "error",
        "critical",
    }


def test_typed_event_keeps_approved_numeric_user_id_and_safe_source_reference() -> None:
    source = TelegramSourceReference(-1009876543210, (11, 12), "album-1")
    event = _event(source=source)
    assert event.telegram_user_id == 42
    assert event.source == source


@pytest.mark.parametrize("telegram_user_id", ["42", 42.0, True, 0, -42])
def test_event_rejects_non_numeric_or_non_user_telegram_ids(telegram_user_id: object) -> None:
    with pytest.raises(ValueError, match="positive numeric user ID"):
        _event(telegram_user_id=telegram_user_id)


def test_event_type_and_category_must_match() -> None:
    with pytest.raises(ValueError, match="type and category do not match"):
        _event(
            event_type=AuditEventType.TERMINAL_OPERATIONAL_ERROR,
            category=AuditCategory.SYSTEM,
        )


def test_submission_event_requires_typed_source_reference() -> None:
    with pytest.raises(ValueError, match="require a source reference"):
        _event(
            event_type=AuditEventType.USER_SUBMISSION_RECEIVED,
            category=AuditCategory.USER_SUBMISSION,
            source=None,
        )


def test_event_requires_utc_and_bounded_safe_classifications() -> None:
    with pytest.raises(ValueError, match="must be UTC"):
        _event(occurred_at=datetime(2026, 8, 31, 12, 30))
    with pytest.raises(ValueError, match="safe classification"):
        _event(provider="https://instagram.com/user")


@pytest.mark.parametrize("identity", ["_urlsafe-job", "-urlsafe-job", "+urlsafe-job"])
def test_event_accepts_project_generated_urlsafe_identities(identity: str) -> None:
    event = _event(event_id=identity, correlation_id=identity, job_id=identity)
    assert event.event_id == identity


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("Authorization: Bearer abc.def", "abc.def"),
        ("bot_token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        ("instagram_password=hunter2", "hunter2"),
        ("2fa=123456", "123456"),
        ("checkpoint: 654321", "654321"),
        ("instagram_session=session-value", "session-value"),
        ("cookies=sid-value", "sid-value"),
        ("vault_key=base64value", "base64value"),
        ("payment_secret=pay-secret", "pay-secret"),
        ("payment_callback_signature=sig-value", "sig-value"),
        ("provider_transaction_reference=txn-123", "txn-123"),
        ("gateway_secret=gateway-value", "gateway-value"),
        ("signed_login_token=login-value", "login-value"),
        ("card_number=4111111111111111", "4111111111111111"),
        ("proxy_password=proxy-pass", "proxy-pass"),
        ("proxy_credentials=user:pass", "user:pass"),
        ("https://proxy-user:proxy-pass@proxy.example", "proxy-pass"),  # pragma: allowlist secret
        ("Cookie: sessionid=first-secret; csrftoken=second-secret", "second-secret"),
        ("Set-Cookie: sessionid=cookie-secret; Path=/; HttpOnly", "cookie-secret"),
        ("password: correct horse battery staple", "correct horse battery staple"),
        ('{"password": "quoted-secret"}', "quoted-secret"),  # pragma: allowlist secret
        ("sessionid=session-secret", "session-secret"),
        ("Instagram session: long session secret", "long session secret"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ],
)
def test_sanitizer_redacts_realistic_secret_values(value: str, secret: str) -> None:
    sanitized = sanitize_audit_message(value)
    assert secret not in sanitized
    assert "redacted" in sanitized


@pytest.mark.parametrize(
    "value", ["token refresh failed", "cookie health expired", "password field absent"]
)
def test_sanitizer_does_not_over_reject_safe_operational_words(value: str) -> None:
    assert sanitize_audit_message(value) == value


@pytest.mark.parametrize(
    "value",
    [
        RuntimeError("raw exception"),
        'Traceback (most recent call last):\n  File "secret.py"',
        r"secret path C:\Users\operator\vault.key",
        ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tsecret",
        ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tsecret\r\n",
    ],
)
def test_sanitizer_rejects_unsafe_structures(value: object) -> None:
    with pytest.raises(UnsafeAuditPayloadError):
        sanitize_audit_message(value)


def test_event_serialization_is_stable_and_round_trips() -> None:
    event = _event(source=TelegramSourceReference(-1009876543210, (11, 12), "album-1"))
    first = serialize_event(event)
    second = serialize_event(event)
    assert first == second
    assert deserialize_event(first) == event


def test_sanitizer_is_idempotent_for_persistence_boundary() -> None:
    sanitized = sanitize_audit_message(
        'Cookie: sessionid=secret; password="correct horse battery staple"'  # pragma: allowlist secret
    )
    assert sanitize_audit_message(sanitized) == sanitized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("correlation_id", "Authorization: Bearer secret"),
        ("job_id", "provider_transaction_reference=secret"),
        ("event_id", "Traceback (most recent call last)"),
    ],
)
def test_secret_bearing_metadata_identifiers_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="safe identifier"):
        _event(**{field: value})
