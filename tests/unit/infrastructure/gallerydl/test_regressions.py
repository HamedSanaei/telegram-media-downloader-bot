from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image
from structlog.testing import capture_logs

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.credential_resolution import ResolvedCredential
from telegram_media_bot.domain.errors import (
    GalleryDlAccessDeniedError,
    GalleryDlAuthenticationRequiredError,
    GalleryDlCookiesExpiredError,
    GalleryDlEmptyResponseError,
    GalleryDlExtractionError,
    GalleryDlOutputChangedError,
    GalleryDlRateLimitedError,
    GalleryDlTransientError,
    GalleryDlUnavailableError,
    MediaBotError,
    MediaUnavailableError,
)
from telegram_media_bot.domain.failures import FailureStage
from telegram_media_bot.domain.models import (
    DownloadMode,
    DownloadRequest,
    JobId,
    MediaInspection,
    MediaKind,
)
from telegram_media_bot.infrastructure.gallerydl.adapter import GalleryDlEngine
from telegram_media_bot.infrastructure.gallerydl.command_builder import GalleryDlCommandBuilder
from telegram_media_bot.infrastructure.gallerydl.errors import map_process_failure
from telegram_media_bot.infrastructure.gallerydl.models import GalleryProcessResult
from telegram_media_bot.infrastructure.gallerydl.parser import parse_inspection
from telegram_media_bot.infrastructure.gallerydl.runner import GalleryDlRunner

_FIXTURES = Path("tests/fixtures/gallerydl")
_URL = "https://www.instagram.com/p/Fixture1/"


@pytest.mark.parametrize(
    ("stderr", "expected", "category", "status", "retryable"),
    [
        (
            b"HTTP 401 Unauthorized",
            GalleryDlAuthenticationRequiredError,
            "authentication_required",
            401,
            False,
        ),
        (b"HTTP 403 Forbidden", GalleryDlAccessDeniedError, "access_denied", 403, False),
        (
            b"HttpError: '403 Forbidden' for 'https://example.invalid/'",
            GalleryDlAccessDeniedError,
            "access_denied",
            403,
            False,
        ),
        (
            b"HTTP 403: login_required",
            GalleryDlAuthenticationRequiredError,
            "authentication_required",
            403,
            False,
        ),
        (
            b"HTTP 403: challenge_required",
            GalleryDlAuthenticationRequiredError,
            "challenge_required",
            403,
            False,
        ),
        (
            b"HTTP 403: session expired",
            GalleryDlCookiesExpiredError,
            "session_rejected",
            403,
            False,
        ),
        (
            b"HTTP redirect to login page",
            GalleryDlAuthenticationRequiredError,
            "authentication_required",
            None,
            False,
        ),
        (
            b"HTTP redirect to challenge page",
            GalleryDlAuthenticationRequiredError,
            "challenge_required",
            None,
            False,
        ),
        (b"HTTP 429 Too Many Requests", GalleryDlRateLimitedError, "rate_limited", 429, True),
        (
            b"Please wait a few minutes before you try again",
            GalleryDlRateLimitedError,
            "rate_limited",
            None,
            True,
        ),
        (b"HTTP 404 Not Found", MediaUnavailableError, "media_unavailable", 404, False),
        (b"HTTP 410 Gone", MediaUnavailableError, "media_unavailable", 410, False),
        (b"HTTP 503 Service Unavailable", GalleryDlTransientError, "upstream_server", 503, True),
        (
            b"HttpError: '502 Bad Gateway' for 'https://example.invalid/'",
            GalleryDlTransientError,
            "upstream_server",
            502,
            True,
        ),
        (b"ReadTimeout: request timed out", GalleryDlTransientError, "network", None, True),
        (b"ConnectionError: connection reset", GalleryDlTransientError, "network", None, True),
        (
            b"NameResolutionError: Name or service not known",
            GalleryDlTransientError,
            "network",
            None,
            True,
        ),
        (
            b"GraphQL query not found",
            GalleryDlOutputChangedError,
            "extractor_incompatible",
            None,
            False,
        ),
        (
            b"GraphQL HTTP 404: document not found",
            GalleryDlOutputChangedError,
            "extractor_incompatible",
            404,
            False,
        ),
        (
            b"GraphQL unknown doc_id",
            GalleryDlOutputChangedError,
            "extractor_incompatible",
            None,
            False,
        ),
        (b"NotFoundError: story", MediaUnavailableError, "media_unavailable", None, False),
        (b"GraphQL error: query rejected", GalleryDlExtractionError, "graphql_error", None, False),
        (
            b"GraphQL error: login_required",
            GalleryDlAuthenticationRequiredError,
            "authentication_required",
            None,
            False,
        ),
        (b"KeyError: 'data'", GalleryDlOutputChangedError, "extractor_incompatible", None, False),
        (
            b"JSONDecodeError: Expecting value",
            GalleryDlOutputChangedError,
            "extractor_incompatible",
            None,
            False,
        ),
        (
            b"Unsupported with GraphQL API",
            GalleryDlOutputChangedError,
            "extractor_incompatible",
            None,
            False,
        ),
        (
            b"No module named gallery_dl",
            GalleryDlUnavailableError,
            "adapter_unavailable",
            None,
            False,
        ),
        (b"Unknown failure", GalleryDlExtractionError, "extraction_unknown", None, False),
        (
            b"https://example.invalid/429/sessionid=fixture",
            GalleryDlExtractionError,
            "extraction_unknown",
            None,
            False,
        ),
    ],
)
def test_process_failures_have_evidence_based_non_secret_diagnostics(
    stderr: bytes,
    expected: type[MediaBotError],
    category: str,
    status: int | None,
    retryable: bool,
) -> None:
    error = map_process_failure(4, stderr)
    assert type(error) is expected
    assert error.http_status == status
    assert error.retryable is retryable
    assert error.diagnostic_category == category
    assert error.exit_code == 4
    assert error.adapter == "gallery-dl"
    assert "example.invalid" not in str(error)
    assert "sessionid" not in str(error)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retry_after_delta_is_retained_only_for_transient_errors(status: int) -> None:
    error = map_process_failure(1, f"HTTP {status}\nRetry-After: 125".encode())
    assert error.retry_after_seconds == 125
    assert error.retryable
    denied = map_process_failure(1, b"HTTP 403\nRetry-After: 125")
    assert denied.retry_after_seconds is None
    assert not denied.retryable


def test_retry_after_http_date_is_numeric_and_does_not_escape_adapter() -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=3)
    error = map_process_failure(
        1, f"HTTP 429\nRetry-After: {format_datetime(deadline, usegmt=True)}".encode()
    )
    assert error.retry_after_seconds is not None
    assert 175 <= error.retry_after_seconds <= 180
    assert "GMT" not in str(error)


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "1e9", "fixture-secret"])
def test_malformed_retry_after_is_not_propagated(value: str) -> None:
    error = map_process_failure(1, f"HTTP 429\nRetry-After: {value}".encode())
    assert error.retry_after_seconds is None


def test_oversized_retry_after_is_preserved_as_terminal_numeric_floor() -> None:
    error = map_process_failure(1, b"HTTP 429\nRetry-After: 9999999999999999999999")
    assert error.retry_after_seconds is not None
    assert error.retry_after_seconds > 900
    padded = map_process_failure(1, b"HTTP 429\nRetry-After: 00000000000001")
    assert padded.retry_after_seconds == 1


class _OutcomeRunner:
    def __init__(self, payload: bytes, *, stderr: bytes = b"", return_code: int = 0) -> None:
        self.payload = payload
        self.stderr = stderr
        self.return_code = return_code
        self.inspections = 0
        self.download_return_code = 0
        self.download_stderr = b""
        self.omit_download = False

    def run(self, args: list[str], **_kwargs: Any) -> GalleryProcessResult:
        if "--dump-json" in args:
            self.inspections += 1
            return GalleryProcessResult(self.return_code, self.payload, self.stderr, 0.01)
        workspace = Path(args[args.index("--directory") + 1])
        if self.download_return_code:
            (workspace / "0001-image.jpg.part").write_bytes(b"partial")
        elif not self.omit_download:
            Image.new("RGB", (16, 16)).save(workspace / "0001-image.jpg", format="JPEG")
        return GalleryProcessResult(self.download_return_code, b"", self.download_stderr, 0.01)


def _engine(settings: Settings, runner: _OutcomeRunner) -> GalleryDlEngine:
    engine = GalleryDlEngine(settings, runner=cast(GalleryDlRunner, runner))
    engine._validator = cast(Any, _AllowValidator())
    return engine


class _AllowValidator:
    def validate(self, url: str) -> str:
        return url


@pytest.mark.parametrize(
    ("stderr", "expected", "category"),
    [
        (b"", GalleryDlEmptyResponseError, "empty_ambiguous"),
        (b"No results for a request", GalleryDlEmptyResponseError, "empty_ambiguous"),
        (b"HTTP 403 Forbidden", GalleryDlAccessDeniedError, "access_denied"),
        (b"HTTP 401 Unauthorized", GalleryDlAuthenticationRequiredError, "authentication_required"),
        (b"GraphQL error: query rejected", GalleryDlExtractionError, "graphql_error"),
        (b"HTTP 503 Service Unavailable", GalleryDlTransientError, "upstream_server"),
        (b"NotFoundError: story", MediaUnavailableError, "media_unavailable"),
        (b"KeyError: 'items'", GalleryDlOutputChangedError, "extractor_incompatible"),
    ],
)
def test_successful_empty_output_preserves_only_observed_error_evidence(
    settings: Settings, stderr: bytes, expected: type[MediaBotError], category: str
) -> None:
    runner = _OutcomeRunner(b"", stderr=stderr)
    with pytest.raises(expected) as caught:
        _engine(settings, runner).inspect(_URL)
    assert type(caught.value) is expected
    assert caught.value.diagnostic_category == category
    assert caught.value.failure_stage is FailureStage.INSPECTION
    assert caught.value.source == "instagram"
    assert caught.value.exit_code == 0
    assert runner.inspections == 1


def test_failure_log_has_fixed_numeric_fields_without_provider_body_or_paths(
    settings: Settings,
) -> None:
    secret = b"HTTP 429\nRetry-After: 30\ncookie sessionid=fixture-secret C:/private/cookies.txt https://cdn.invalid/file?sig=fixture-secret"
    runner = _OutcomeRunner(b"", stderr=secret, return_code=4)
    with capture_logs() as logs, pytest.raises(GalleryDlRateLimitedError) as caught:
        _engine(settings, runner).inspect(_URL)
    assert logs[-1]["diagnostic_category"] == "rate_limited"
    assert logs[-1]["http_status"] == 429
    assert logs[-1]["exit_code"] == 4
    assert logs[-1]["stage"] == "inspection"
    assert logs[-1]["retryable"] is True
    assert logs[-1]["retry_after_seconds"] == 30
    diagnostic = repr(logs) + str(caught.value) + repr(vars(caught.value))
    for forbidden in ("fixture-secret", "cookies.txt", "cdn.invalid", "sessionid", "sig="):
        assert forbidden not in diagnostic


def test_bound_gallery_inspection_is_reused_without_another_extraction(
    settings: Settings, tmp_path: Path
) -> None:
    runner = _OutcomeRunner((_FIXTURES / "instagram-single.json").read_bytes())
    engine = _engine(settings, runner)
    credential = ResolvedCredential.none()
    info = engine.inspect(_URL, credential=credential)
    request = DownloadRequest(
        JobId("bound-inspection"),
        _URL,
        DownloadMode.IMAGE_ORIGINAL,
        tmp_path / "job",
        inspection=MediaInspection(info, credential, None),
    )
    result = engine.download(request, credential=credential)
    assert result.kind is MediaKind.IMAGE
    assert result.file_path.is_file()
    assert runner.inspections == 1


@pytest.mark.parametrize("mismatch", ["url", "credential", "cookie_file"])
def test_gallery_reinspects_when_bound_inspection_does_not_match_current_operation(
    settings: Settings, tmp_path: Path, mismatch: str
) -> None:
    runner = _OutcomeRunner((_FIXTURES / "instagram-single.json").read_bytes())
    engine = _engine(settings, runner)
    credential = ResolvedCredential.none()
    info = engine.inspect(_URL, credential=credential)
    binding = MediaInspection(info, credential, None)
    if mismatch == "url":
        binding = replace(
            binding, media=replace(info, webpage_url="https://www.instagram.com/p/Other/")
        )
    elif mismatch == "credential":
        binding = replace(binding, credential=ResolvedCredential.operator_public())
    else:
        binding = replace(binding, cookie_file="other-attempt.txt")
    request = DownloadRequest(
        JobId("unbound-inspection"),
        _URL,
        DownloadMode.IMAGE_ORIGINAL,
        tmp_path / "job",
        inspection=binding,
    )
    engine.download(request, credential=credential)
    assert runner.inspections == 2


@pytest.mark.parametrize("failure", ["deleted", "missing_files"])
def test_media_disappearance_after_inspection_cleans_every_partial_artifact(
    settings: Settings, tmp_path: Path, failure: str
) -> None:
    runner = _OutcomeRunner((_FIXTURES / "instagram-single.json").read_bytes())
    engine = _engine(settings, runner)
    info = engine.inspect(_URL)
    if failure == "deleted":
        runner.download_return_code = 4
        runner.download_stderr = b"HTTP 404 Not Found"
        expected: type[MediaBotError] = MediaUnavailableError
    else:
        runner.omit_download = True
        expected = GalleryDlOutputChangedError
    workspace = tmp_path / "job"
    with pytest.raises(expected) as caught:
        engine.download_inspected(
            DownloadRequest(JobId("disappeared"), _URL, DownloadMode.IMAGE_ORIGINAL, workspace),
            info,
        )
    assert caught.value.failure_stage is FailureStage.DOWNLOAD
    assert not caught.value.retryable
    assert workspace.is_dir()
    assert not any(workspace.iterdir())
    assert runner.inspections == 1


@pytest.mark.parametrize(
    "payload",
    [
        b'{"data": null, "errors": [{"message": "changed schema"}]}',
        b'[-1,{"error":"AuthRequired","message":"fixture"}]',
        b'[3.0,"https://cdn.example.invalid/a.jpg",{}]',
        b"[true,{}]",
        b'[3,"https://cdn.example.invalid/a.jpg",[]]',
        b'[3,"https://cdn.example.invalid/a.jpg",{},"extra"]',
    ],
)
def test_changed_api_and_event_schemas_fail_closed_without_auth_inference(payload: bytes) -> None:
    with pytest.raises(GalleryDlOutputChangedError) as caught:
        parse_inspection(payload, expected_provider="instagram", max_assets=30)
    assert not caught.value.retryable
    assert not isinstance(caught.value, GalleryDlAuthenticationRequiredError)


def test_parser_rejects_provider_changes_inside_a_post() -> None:
    events = [
        json.loads(line)
        for line in (_FIXTURES / "instagram-carousel.json").read_bytes().splitlines()
    ]
    events[-1][-1]["category"] = "twitter"
    with pytest.raises(GalleryDlOutputChangedError):
        parse_inspection(
            b"\n".join(json.dumps(event).encode() for event in events),
            expected_provider="instagram",
            max_assets=30,
        )


def test_every_gallery_command_uses_read_only_cookies_and_single_attempt_configuration(
    settings: Settings, tmp_path: Path
) -> None:
    commands = GalleryDlCommandBuilder(settings.gallery_dl, tmp_path / "canonical.txt")
    operations = [
        commands.inspection(_URL)[1],
        commands.inspect_url("instagram", "https://www.instagram.com/fixtureuser/highlights/"),
        commands.download(_URL, tmp_path / "job")[1],
        commands.version(),
    ]
    for args in operations:
        assert "extractor.cookies-update=false" in args
        assert "extractor.retries=0" in args
        assert "downloader.retries=0" in args
        nested = next(value for value in args if value.startswith("downloader.ytdl.raw-options="))
        assert json.loads(nested.partition("=")[2]) == {
            "cookiefile": None,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "file_access_retries": 0,
        }
