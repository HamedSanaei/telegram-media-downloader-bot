import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import CopyMessage
from aiogram.types import MessageId
from aiogram.utils.formatting import sizeof

from telegram_media_bot.application.services.audit_outbox import AuditOutboxProcessor
from telegram_media_bot.domain.audit import (
    AuditCategory,
    AuditDeliveryOutcome,
    AuditEvent,
    AuditEventType,
    AuditSeverity,
    DeliveredOutputAuditContext,
    LoggerOutboxItem,
    LoggerOutboxState,
    TelegramSourceReference,
)
from telegram_media_bot.infrastructure.persistence.sqlite_audit import SqliteAuditRepository
from telegram_media_bot.infrastructure.telegram.audit_delivery import TelegramAuditDelivery

LOGGER_CHAT_ID = -1001234567890
USER_ID = 821868829
SOURCE_URL = "https://www.instagram.com/reel/DeL5jdsIMo3/"


class CaptionDisplay(HTMLParser):
    """Observe Telegram HTML's visible text and clickable targets, not its wire escapes."""

    def __init__(self, caption: str) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.tags: list[str] = []
        self.feed(caption)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "a":
            href = dict(attrs).get("href")
            assert href is not None
            self.links.append(href)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self.parts)


@dataclass
class MediaRecord:
    message_id: int
    chat_id: int
    source_chat_id: int
    source_message_id: int
    caption: str
    parse_mode: str | None
    show_caption_above_media: bool | None
    group_id: str | None = None

    @property
    def display(self) -> CaptionDisplay:
        assert self.parse_mode == "HTML"
        return CaptionDisplay(self.caption)


class FakeBot:
    def __init__(
        self,
        failure: Exception | None = None,
        *,
        fail_copy_at: int = 1,
        edit_failure: Exception | None = None,
        fail_edit_at: int = 1,
        bulk_result_count: int | None = None,
    ) -> None:
        self.failure = failure
        self.fail_copy_at = fail_copy_at
        self.edit_failure = edit_failure
        self.fail_edit_at = fail_edit_at
        self.bulk_result_count = bulk_result_count
        self.copy_attempts = 0
        self.edit_attempts = 0
        self.copies: list[dict[str, object]] = []
        self.groups: list[dict[str, object]] = []
        self.messages: list[tuple[int, str]] = []
        self.media: list[MediaRecord] = []
        self.source_captions: dict[int, str] = {}
        self.captions_before_edit: list[str] = []

    def _before_copy(self) -> None:
        self.copy_attempts += 1
        if self.failure is not None and self.copy_attempts == self.fail_copy_at:
            raise self.failure

    def _record(
        self, kwargs: dict[str, object], source_id: int, *, group_id: str | None = None
    ) -> MessageId:
        caption = (
            ""
            if kwargs.get("remove_caption")
            else cast(str, kwargs.get("caption", self.source_captions.get(source_id, "")))
        )
        message_id = 5000 + len(self.media)
        self.media.append(
            MediaRecord(
                message_id=message_id,
                chat_id=cast(int, kwargs["chat_id"]),
                source_chat_id=cast(int, kwargs["from_chat_id"]),
                source_message_id=source_id,
                caption=caption,
                parse_mode=cast(str | None, kwargs.get("parse_mode")),
                show_caption_above_media=cast(bool | None, kwargs.get("show_caption_above_media")),
                group_id=group_id,
            )
        )
        return MessageId(message_id=message_id)

    async def copy_message(self, **kwargs: object) -> MessageId:
        self._before_copy()
        self.copies.append(kwargs)
        return self._record(kwargs, cast(int, kwargs["message_id"]))

    async def copy_messages(self, **kwargs: object) -> list[MessageId]:
        self._before_copy()
        self.groups.append(kwargs)
        source_ids = cast(list[int], kwargs["message_ids"])
        assert 2 <= len(source_ids) <= 100
        assert all(left < right for left, right in pairwise(source_ids))
        selected = (
            source_ids if self.bulk_result_count is None else source_ids[: self.bulk_result_count]
        )
        return [
            self._record(kwargs, source_id, group_id=f"album-{len(self.groups)}")
            for source_id in selected
        ]

    async def edit_message_caption(self, **kwargs: object) -> bool:
        self.edit_attempts += 1
        if self.edit_failure is not None and self.edit_attempts == self.fail_edit_at:
            raise self.edit_failure
        record = next(
            media
            for media in self.media
            if media.chat_id == kwargs["chat_id"] and media.message_id == kwargs["message_id"]
        )
        self.captions_before_edit.append(record.caption)
        record.caption = cast(str, kwargs["caption"])
        record.parse_mode = cast(str, kwargs["parse_mode"])
        record.show_caption_above_media = cast(bool, kwargs["show_caption_above_media"])
        return True

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def _transport(bot: FakeBot) -> TelegramAuditDelivery:
    return TelegramAuditDelivery(cast(Bot, cast(Any, bot)))


def _item(
    message_ids: tuple[int, ...] = (10,),
    *,
    source_chat_id: int = 4242,
) -> LoggerOutboxItem:
    event = AuditEvent(
        event_id="event-1",
        event_type=AuditEventType.USER_SUBMISSION_RECEIVED,
        category=AuditCategory.USER_SUBMISSION,
        severity=AuditSeverity.INFO,
        occurred_at=datetime(2026, 8, 31, 12, 0, tzinfo=UTC),
        correlation_id="submission:update:77",
        message="Accepted Telegram download submission",
        telegram_user_id=4242,
        update_id=77,
        job_id="inspection-1",
        content_type="photo",
        provider="example.com",
        source=TelegramSourceReference(source_chat_id, message_ids),
    )
    return LoggerOutboxItem(
        event=event,
        destination_chat_id=LOGGER_CHAT_ID,
        state=LoggerOutboxState.LEASED,
        attempt_count=1,
        lease_token="lease",
    )


def _output_item(
    message_ids: tuple[int, ...] = (10,),
    *,
    captions: tuple[str, ...] | None = None,
    source_chat_id: int = 4242,
    username: str | None = "sample_user",
    source_url: str = SOURCE_URL,
) -> LoggerOutboxItem:
    item = _item(message_ids, source_chat_id=source_chat_id)
    return replace(
        item,
        event=replace(
            item.event,
            event_type=AuditEventType.DOWNLOAD_OUTPUT_DELIVERED,
            event_id="delivery-output:job-1",
            correlation_id="job-1",
            message="Delivered download output",
            telegram_user_id=USER_ID,
            output=DeliveredOutputAuditContext(
                source_url=source_url,
                captions=(
                    tuple(f"توضیحات آیتم {message_id}" for message_id in message_ids)
                    if captions is None
                    else captions
                ),
                telegram_username=username,
            ),
        ),
    )


def _assert_footer(
    record: MediaRecord, *, username: str | None = "sample_user", source_url: str = SOURCE_URL
) -> CaptionDisplay:
    assert record.chat_id == LOGGER_CHAT_ID
    assert record.show_caption_above_media is False
    display = record.display
    assert display.tags == ["code", "a"]
    assert display.links == [source_url]
    assert f"آیدی عددی: {USER_ID}" in display.text
    assert display.text.count("🔗 لینک اصلی: ") == 1
    if username is None:
        assert "یوزرنیم:" not in display.text
        assert "@None" not in display.text
    else:
        assert f"یوزرنیم: @{username}" in display.text
    assert sizeof(display.text) <= 1024
    return display


@pytest.mark.parametrize("message_ids", [(10,), (10, 11, 12)])
async def test_historical_accepted_submission_is_retired_without_telegram_effects(
    message_ids: tuple[int, ...],
) -> None:
    bot = FakeBot()
    result = await _transport(bot).deliver(_item(message_ids))
    assert result.outcome is AuditDeliveryOutcome.FAILED_TERMINAL
    assert result.failure_class == "SubmissionMirrorRetired"
    assert bot.copies == []
    assert bot.groups == []
    assert bot.messages == []


async def test_single_output_is_one_media_post_with_description_identity_and_link() -> None:
    description = "توضیحات فارسی 🎬 <b>literal</b> & > **Markdown**\nخط دوم\n🤖 @DownloadKadeBot"
    bot = FakeBot()

    result = await _transport(bot).deliver(_output_item(captions=(description,)))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert len(bot.media) == 1
    assert bot.messages == []
    assert bot.groups == []
    assert bot.edit_attempts == 0
    display = _assert_footer(bot.media[0])
    assert display.text == (
        f"{description}\n\nآیدی عددی: {USER_ID}\nیوزرنیم: @sample_user\n🔗 لینک اصلی: {SOURCE_URL}"
    )
    assert "آیدی عددی: 4242" not in display.text
    assert "Accepted download submission" not in display.text
    assert "Delivered download output" not in display.text


@pytest.mark.parametrize("description", ["", "توضیحات بدون یوزرنیم"])
async def test_output_without_username_omits_entire_username_line(description: str) -> None:
    bot = FakeBot()

    result = await _transport(bot).deliver(_output_item(captions=(description,), username=None))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    display = _assert_footer(bot.media[0], username=None)
    expected_prefix = f"{description}\n\n" if description else ""
    assert display.text == (f"{expected_prefix}آیدی عددی: {USER_ID}\n🔗 لینک اصلی: {SOURCE_URL}")
    assert bot.messages == []


async def test_output_album_preserves_recipient_source_grouping_and_each_caption() -> None:
    captions = ("توضیحات اول 🎬", "خط اول\nخط دوم & <literal>", "")
    bot = FakeBot()
    bot.source_captions = {901: "UNSAFE ORIGINAL", 902: "UNSAFE ORIGINAL", 903: "UNSAFE ORIGINAL"}

    result = await _transport(bot).deliver(
        _output_item((901, 902, 903), captions=captions, source_chat_id=-1007770001112)
    )

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert len(bot.groups) == 1
    assert bot.copies == []
    assert bot.messages == []
    assert [record.source_message_id for record in bot.media] == [901, 902, 903]
    assert {record.source_chat_id for record in bot.media} == {-1007770001112}
    assert len({record.group_id for record in bot.media}) == 1
    assert bot.media[0].group_id is not None
    assert bot.captions_before_edit == ["", "", ""]
    for record, description in zip(bot.media, captions, strict=True):
        display = _assert_footer(record)
        assert display.text.startswith(f"{description}\n\n" if description else "آیدی عددی:")
        assert "UNSAFE ORIGINAL" not in display.text


async def test_large_output_copy_is_bounded_and_preserves_global_caption_order() -> None:
    bot = FakeBot()
    message_ids = tuple(range(1, 207))

    result = await _transport(bot).deliver(_output_item(message_ids))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    copied_groups = [cast(list[int], group["message_ids"]) for group in bot.groups]
    assert [len(group) for group in copied_groups] == [100, 100, 6]
    assert [record.source_message_id for record in bot.media] == list(message_ids)
    assert len({record.group_id for record in bot.media}) == 3
    assert bot.messages == []
    for record, message_id in zip(bot.media, message_ids, strict=True):
        assert _assert_footer(record).text.startswith(f"توضیحات آیتم {message_id}\n\n")


async def test_nonincreasing_output_ids_keep_ordinal_caption_associations() -> None:
    bot = FakeBot()
    ids = (101, 103, 102, 104)
    captions = ("اول", "دوم", "سوم", "چهارم")

    result = await _transport(bot).deliver(_output_item(ids, captions=captions))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert [group["message_ids"] for group in bot.groups] == [[101, 103], [102, 104]]
    assert [record.source_message_id for record in bot.media] == list(ids)
    for record, description in zip(bot.media, captions, strict=True):
        assert _assert_footer(record).text.startswith(f"{description}\n\n")
    assert bot.messages == []


async def test_singleton_runs_and_last_chunk_use_single_copy_without_extra_messages() -> None:
    bot = FakeBot()
    ids = (103, 102, *range(104, 204))

    result = await _transport(bot).deliver(_output_item(ids))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert [copy["message_id"] for copy in bot.copies] == [103, 203]
    assert [len(cast(list[int], group["message_ids"])) for group in bot.groups] == [100]
    assert [record.source_message_id for record in bot.media] == list(ids)
    for record, source_id in zip(bot.media, ids, strict=True):
        assert _assert_footer(record).text.startswith(f"توضیحات آیتم {source_id}\n\n")
    assert bot.messages == []


async def test_oversized_astral_description_truncates_only_prefix_with_ellipsis() -> None:
    description = "توضیحات 🎬 <>&\n" + "😀" * 1000
    bot = FakeBot()

    result = await _transport(bot).deliver(_output_item(captions=(description,)))

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    display = _assert_footer(bot.media[0])
    prefix, footer = display.text.rsplit("\n\n", 1)
    assert prefix.endswith("…")
    assert description.startswith(prefix[:-1])
    assert "😀" in prefix
    assert "\ufffd" not in prefix
    assert footer == (f"آیدی عددی: {USER_ID}\nیوزرنیم: @sample_user\n🔗 لینک اصلی: {SOURCE_URL}")
    assert sizeof(display.text) >= 1023
    assert bot.messages == []


@pytest.mark.parametrize("path_length", [240, 1400])
async def test_long_source_target_remains_complete_and_escapes_href(path_length: int) -> None:
    source_url = "https://example.com/" + "x" * path_length + "?id=7&index=2"
    description = "متن اصلی <>&"
    bot = FakeBot()

    result = await _transport(bot).deliver(
        _output_item(captions=(description,), source_url=source_url)
    )

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    display = _assert_footer(bot.media[0], source_url=source_url)
    assert display.text.startswith(f"{description}\n\n")
    expected_label = "مشاهده پست" if path_length > 1024 else source_url
    assert display.text.endswith(f"🔗 لینک اصلی: {expected_label}")
    assert "&amp;index=2" in bot.media[0].caption
    assert bot.messages == []


async def test_footer_alone_at_limit_drops_description_without_shortening_identity_or_url() -> None:
    footer_without_url = f"آیدی عددی: {USER_ID}\nیوزرنیم: @sample_user\n🔗 لینک اصلی: "
    url_base = "https://example.com/"
    source_url = url_base + "x" * (1024 - sizeof(footer_without_url) - sizeof(url_base))
    bot = FakeBot()

    result = await _transport(bot).deliver(
        _output_item(captions=("توضیحات",), source_url=source_url)
    )

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert (
        _assert_footer(bot.media[0], source_url=source_url).text == footer_without_url + source_url
    )
    assert bot.messages == []


@pytest.mark.parametrize(
    ("missing", "failure_class"),
    [
        ("output", "MissingOutputCaptionContext"),
        ("telegram_user_id", "MissingOutputUserIdentity"),
        ("source", "MissingSourceReference"),
        ("alignment", "MissingOutputCaptionContext"),
    ],
)
async def test_missing_output_requirements_fail_before_any_telegram_call(
    missing: str, failure_class: str
) -> None:
    item = _output_item()
    if missing == "alignment":
        # The domain rejects new mismatches; the transport still guards corrupted input.
        object.__setattr__(
            item.event, "output", DeliveredOutputAuditContext(SOURCE_URL, ("first", "second"))
        )
    elif missing == "source":
        # Source-free copy events cannot be constructed through the public domain API.
        object.__setattr__(item.event, "source", None)
    elif missing == "output":
        item = replace(item, event=replace(item.event, output=None))
    else:
        item = replace(item, event=replace(item.event, telegram_user_id=None))
    bot = FakeBot()

    result = await _transport(bot).deliver(item)

    assert result.outcome is AuditDeliveryOutcome.FAILED_TERMINAL
    assert result.failure_class == failure_class
    assert bot.copy_attempts == bot.edit_attempts == 0
    assert bot.media == []
    assert bot.messages == []


def _failure(kind: str) -> Exception:
    method = CopyMessage(chat_id=LOGGER_CHAT_ID, from_chat_id=4242, message_id=10)
    if kind == "retry":
        return TelegramRetryAfter(method=method, message="retry later", retry_after=2)
    if kind == "timeout":
        return TimeoutError("timed out")
    classes = {
        "forbidden": TelegramForbiddenError,
        "bad_request": TelegramBadRequest,
        "network": TelegramNetworkError,
        "server": TelegramServerError,
        "api": TelegramAPIError,
    }
    return classes[kind](method=method, message="fixture failure")


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        ("retry", AuditDeliveryOutcome.RETRYABLE),
        ("forbidden", AuditDeliveryOutcome.FAILED_TERMINAL),
        ("bad_request", AuditDeliveryOutcome.FAILED_TERMINAL),
        ("network", AuditDeliveryOutcome.UNCERTAIN),
        ("server", AuditDeliveryOutcome.UNCERTAIN),
        ("timeout", AuditDeliveryOutcome.UNCERTAIN),
        ("api", AuditDeliveryOutcome.UNCERTAIN),
    ],
)
async def test_before_copy_failures_preserve_outcome_classification(
    kind: str, outcome: AuditDeliveryOutcome
) -> None:
    failure = _failure(kind)
    bot = FakeBot(failure)

    result = await _transport(bot).deliver(_output_item())

    assert result.outcome is outcome
    assert result.failure_class == type(failure).__name__
    assert bot.copy_attempts == 1
    assert bot.media == []
    assert bot.messages == []


@pytest.mark.parametrize(
    "kind", ["retry", "forbidden", "bad_request", "network", "server", "timeout", "api"]
)
@pytest.mark.parametrize("boundary", ["edit", "later_copy"])
async def test_failure_after_a_copy_is_always_uncertain(kind: str, boundary: str) -> None:
    failure = _failure(kind)
    bot = (
        FakeBot(edit_failure=failure, fail_edit_at=2)
        if boundary == "edit"
        else FakeBot(failure, fail_copy_at=2)
    )
    item = _output_item((10, 11)) if boundary == "edit" else _output_item(tuple(range(1, 102)))

    result = await _transport(bot).deliver(item)

    assert result.outcome is AuditDeliveryOutcome.UNCERTAIN
    assert result.failure_class == type(failure).__name__
    assert len(bot.media) == (2 if boundary == "edit" else 100)
    _assert_footer(bot.media[0])
    if boundary == "edit":
        assert bot.media[1].caption == ""
    assert bot.messages == []


@pytest.mark.parametrize("returned_count", [0, 1])
async def test_incomplete_bulk_copy_is_uncertain_without_guessing_caption_mapping(
    returned_count: int,
) -> None:
    bot = FakeBot(bulk_result_count=returned_count)

    result = await _transport(bot).deliver(_output_item((10, 11)))

    assert result.outcome is AuditDeliveryOutcome.UNCERTAIN
    assert result.failure_class == "IncompleteOutputCopy"
    assert len(bot.media) == returned_count
    assert bot.edit_attempts == 0
    assert all(record.caption == "" for record in bot.media)
    assert bot.messages == []


@pytest.mark.parametrize("boundary", ["empty", "incomplete", "edit", "later_copy"])
async def test_uncertain_output_is_quarantined_across_sqlite_restart_without_recopy(
    tmp_path: Path, boundary: str
) -> None:
    path = tmp_path / "audit.sqlite3"
    repository = SqliteAuditRepository(path)
    repository.initialize()
    repository.reconcile_config((LOGGER_CHAT_ID,))
    if boundary in {"empty", "incomplete"}:
        bot = FakeBot(bulk_result_count=0 if boundary == "empty" else 1)
        item = _output_item((10, 11))
    elif boundary == "edit":
        bot = FakeBot(edit_failure=_failure("retry"), fail_edit_at=2)
        item = _output_item((10, 11))
    else:
        bot = FakeBot(_failure("retry"), fail_copy_at=2)
        item = _output_item(tuple(range(1, 102)))
    repository.enqueue(item.event)
    observed: list[AuditDeliveryOutcome] = []
    processor = AuditOutboxProcessor(
        repository, _transport(bot), observer=lambda outcome, category: observed.append(outcome)
    )

    assert await processor.dispatch_batch() == 0
    assert observed == [AuditDeliveryOutcome.UNCERTAIN]
    before_restart = [replace(record) for record in bot.media]
    copy_attempts = bot.copy_attempts
    edit_attempts = bot.edit_attempts
    # Remove the simulated transient failure: quarantine, rather than continued failure,
    # must prevent the second dispatch from issuing a new Telegram copy.
    bot.failure = bot.edit_failure = None
    bot.bulk_result_count = None
    reopened = SqliteAuditRepository(path)
    reopened.initialize()
    restarted = AuditOutboxProcessor(reopened, _transport(bot))

    assert await restarted.dispatch_batch() == 0
    assert bot.media == before_restart
    assert bot.copy_attempts == copy_attempts
    assert bot.edit_attempts == edit_attempts
    assert bot.messages == []
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT state FROM logger_outbox WHERE event_id=? AND destination_chat_id=?",
            (item.event.event_id, LOGGER_CHAT_ID),
        ).fetchone()
    assert row == (LoggerOutboxState.UNCERTAIN.value,)


@pytest.mark.parametrize(
    ("event_type", "category"),
    [
        (AuditEventType.TERMINAL_OPERATIONAL_ERROR, AuditCategory.ERROR),
        (AuditEventType.COOKIE_HEALTH_CHANGED, AuditCategory.COOKIE_HEALTH),
        (AuditEventType.SYSTEM_HEALTH, AuditCategory.SYSTEM),
        (AuditEventType.PAYMENT_CONFIRMED, AuditCategory.PAYMENT),
    ],
)
async def test_nonmedia_reporting_remains_one_plain_text_message(
    event_type: AuditEventType, category: AuditCategory
) -> None:
    item = _item()
    item = replace(
        item,
        event=replace(
            item.event,
            event_type=event_type,
            category=category,
            source=None,
            message="Safe operational message",
        ),
    )
    bot = FakeBot()

    result = await _transport(bot).deliver(item)

    assert result.outcome is AuditDeliveryOutcome.SUCCEEDED
    assert bot.messages == [(LOGGER_CHAT_ID, "Safe operational message")]
    assert bot.media == []
    assert bot.copy_attempts == bot.edit_attempts == 0
