from __future__ import annotations

import asyncio

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AccessDeniedError,
    InvalidUrlError,
    PolicyBackendError,
    SelectionExpiredError,
    SelectionOwnershipError,
    UnsafeUrlError,
    UserRateLimitError,
)
from telegram_media_bot.domain.models import (
    DownloadMode,
    ErrorCategory,
    JobId,
    JobStatus,
    SelectionToken,
)
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.telegram.middleware import CorrelationMiddleware
from telegram_media_bot.telegram.texts import (
    ACCESS_DENIED_TEXT,
    CANCELLED_TEXT,
    CANNOT_CANCEL_TEXT,
    INSPECTION_QUEUED_TEXT,
    INVALID_URL_TEXT,
    QUEUED_TEXT,
    RATE_LIMIT_TEXT,
    SELECTION_EXPIRED_TEXT,
    SELECTION_INVALID_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    START_TEXT,
    UNSAFE_URL_TEXT,
)
from telegram_media_bot.telegram.ui import cancellation_keyboard
from telegram_media_bot.telegram.url_extractor import extract_first_url

logger = structlog.get_logger(__name__)


def build_router(
    *,
    settings: Settings,
    queue: JobQueue,
    repository: JobRepository,
    access_policy: AccessPolicyService,
    jobs: JobService,
) -> Router:
    router = Router(name="main")
    router.message.outer_middleware(CorrelationMiddleware())
    router.callback_query.outer_middleware(CorrelationMiddleware())
    url_validator = PublicUrlValidator(
        reject_private_networks=settings.security.reject_private_network_urls
    )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(START_TEXT)

    @router.message(Command("health"))
    async def health(message: Message) -> None:
        if not _is_admin(message, settings) or message.from_user is None:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        redis_ok, depth, database_ok = await asyncio.gather(
            queue.healthy(),
            queue.queue_depth(),
            asyncio.to_thread(repository.healthy),
        )
        await message.answer(
            f"Redis: {'OK' if redis_ok else 'FAIL'}\n"
            f"Database: {'OK' if database_ok else 'FAIL'}\n"
            f"Queue depth: {depth}"
        )

    @router.message(Command("queue"))
    async def queue_status(message: Message) -> None:
        if not _is_admin(message, settings):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        depth, counts = await asyncio.gather(
            queue.queue_depth(), asyncio.to_thread(repository.counts)
        )
        await message.answer(
            f"Redis queue: {depth}\nQueued: {counts.queued}\nRunning: {counts.running}\n"
            f"Retrying: {counts.retrying}\nFailed: {counts.failed}"
        )

    @router.message(Command("failed"))
    async def failed(message: Message) -> None:
        if not _is_admin(message, settings):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        records = await asyncio.to_thread(repository.failed_jobs, 10)
        if not records:
            await message.answer("کار ناموفقی ثبت نشده است.")
            return
        lines = [
            f"{record.job_id}: {record.error_category.value if record.error_category else 'unknown'}"
            for record in records
        ]
        await message.answer("آخرین خطاها:\n" + "\n".join(lines))

    @router.message(Command("block", "unblock"))
    async def manage_block(message: Message) -> None:
        if not _is_admin(message, settings) or message.from_user is None:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("کاربرد: /block USER_ID یا /unblock USER_ID")
            return
        target = int(parts[1])
        if target in settings.telegram.admin_ids:
            await message.answer("مسدودکردن مدیر مجاز نیست.")
            return
        command = parts[0].split("@", maxsplit=1)[0]
        if command == "/block":
            await asyncio.to_thread(repository.block_user, target, message.from_user.id)
            await message.answer(f"کاربر {target} مسدود شد.")
        else:
            await asyncio.to_thread(repository.unblock_user, target)
            await message.answer(f"مسدودی کاربر {target} برداشته شد.")

    @router.message(Command("resolve"))
    async def resolve_uncertain(message: Message) -> None:
        if not _is_admin(message, settings):
            await message.answer(ACCESS_DENIED_TEXT)
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("کاربرد: /resolve JOB_ID")
            return
        job_id = JobId(parts[1])
        record = await asyncio.to_thread(repository.get_job, job_id)
        if record is None or record.status is not JobStatus.DELIVERY_UNCERTAIN:
            await message.answer("کار نامشخصی با این شناسه وجود ندارد.")
            return
        await asyncio.to_thread(
            repository.transition,
            job_id,
            JobStatus.FAILED,
            error_category=ErrorCategory.DELIVERY_UNCERTAIN,
            error_summary="operator_reviewed",
        )
        await message.answer("وضعیت نامشخص بررسی‌شده علامت خورد؛ درخواست تازه اکنون مجاز است.")

    @router.callback_query(F.data.startswith("fmt:"))
    async def choose_format(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        try:
            await access_policy.authorize_request(callback.from_user.id)
            token, mode = parse_selection_callback(callback.data)
            selection = await asyncio.to_thread(
                repository.get_selection,
                token,
                callback.from_user.id,
            )
            if mode not in selection.allowed_modes:
                raise SelectionOwnershipError("Mode was not offered")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        except AccessDeniedError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
            return
        except UserRateLimitError:
            await callback.answer(RATE_LIMIT_TEXT, show_alert=True)
            return
        except PolicyBackendError:
            await callback.answer(SERVICE_UNAVAILABLE_TEXT, show_alert=True)
            return

        record, created = await asyncio.to_thread(
            jobs.create_download,
            chat_id=selection.chat_id,
            user_id=selection.owner_user_id,
            url=selection.media.webpage_url,
            mode=mode,
        )
        if record.status is JobStatus.DELIVERY_UNCERTAIN:
            await callback.answer(
                "وضعیت ارسال قبلی نامشخص است؛ مدیر باید آن را بررسی کند.", show_alert=True
            )
            return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                QUEUED_TEXT.format(job_id=record.job_id),
                reply_markup=cancellation_keyboard(record.job_id),
            )
            await asyncio.to_thread(
                repository.set_status_message, record.job_id, callback.message.message_id
            )
        if created:
            try:
                await queue.enqueue_download(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                    mode=mode,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    repository.transition,
                    record.job_id,
                    JobStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL,
                    error_summary="queue_enqueue_failed",
                )
                if isinstance(callback.message, Message):
                    await callback.message.edit_text("ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.")
                await logger.aexception(
                    "download_enqueue_failed", job_id=record.job_id, error_type=type(exc).__name__
                )
                await callback.answer("صف موقتاً در دسترس نیست", show_alert=True)
                return
        await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        raw_job_id = callback.data.removeprefix("cancel:")
        cancelled = await asyncio.to_thread(
            repository.request_cancel, JobId(raw_job_id), callback.from_user.id
        )
        if not cancelled:
            await callback.answer(CANNOT_CANCEL_TEXT, show_alert=True)
            return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(CANCELLED_TEXT)
        await callback.answer("درخواست لغو ثبت شد")

    @router.message()
    async def enqueue_url(message: Message) -> None:
        if message.from_user is None:
            return
        try:
            await access_policy.authorize_request(message.from_user.id)
        except AccessDeniedError:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        except UserRateLimitError:
            await message.answer(RATE_LIMIT_TEXT)
            return
        except PolicyBackendError:
            await message.answer(SERVICE_UNAVAILABLE_TEXT)
            return
        url = extract_first_url(message.text or message.caption)
        if url is None:
            await message.answer(INVALID_URL_TEXT)
            return
        try:
            validated = await asyncio.to_thread(url_validator.validate, url)
        except UnsafeUrlError:
            await message.answer(UNSAFE_URL_TEXT)
            return
        except InvalidUrlError:
            await message.answer(INVALID_URL_TEXT)
            return
        record, created = await asyncio.to_thread(
            jobs.create_inspection,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            url=validated,
        )
        response = await message.answer(INSPECTION_QUEUED_TEXT.format(job_id=record.job_id))
        await asyncio.to_thread(repository.set_status_message, record.job_id, response.message_id)
        if created:
            try:
                await queue.enqueue_inspection(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    repository.transition,
                    record.job_id,
                    JobStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL,
                    error_summary="queue_enqueue_failed",
                )
                await response.edit_text("ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.")
                await logger.aexception(
                    "inspection_enqueue_failed",
                    job_id=record.job_id,
                    error_type=type(exc).__name__,
                )

    return router


def _is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.telegram.admin_ids


def parse_selection_callback(data: str) -> tuple[SelectionToken, DownloadMode]:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "fmt" or not 10 <= len(parts[1]) <= 32:
        raise ValueError("Invalid selection callback")
    return SelectionToken(parts[1]), DownloadMode(parts[2])
