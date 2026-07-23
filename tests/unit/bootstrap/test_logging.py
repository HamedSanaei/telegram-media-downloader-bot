from telegram_media_bot.bootstrap.logging import redact_sensitive_data


def test_redacts_nested_secrets_and_url_credentials() -> None:
    result = redact_sensitive_data(
        None,
        "info",
        {
            "bot_token": "secret",
            "nested": {"authorization_header": "bearer secret"},
            "endpoint": "https://user:pass@example.com/path",  # pragma: allowlist secret
            "job_id": "safe",
        },
    )
    assert result["bot_token"] == "[REDACTED]"
    assert result["nested"] == {"authorization_header": "[REDACTED]"}
    assert result["endpoint"] == "https://[REDACTED]@example.com/path"
    assert result["job_id"] == "safe"


def test_redaction_does_not_fail_on_malformed_url_port() -> None:
    result = redact_sensitive_data(
        None,
        "info",
        {"endpoint": "https://user:pass@example.com:not-a-port/path"},  # pragma: allowlist secret
    )

    assert result["endpoint"] == "[REDACTED_URL]"
