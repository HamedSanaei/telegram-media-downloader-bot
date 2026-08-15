from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup

from telegram_media_bot.application.ports.cookie_management import CookieManager
from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.application.ports.user_repository import UserRepository
from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.instagram_delivery import (
    instagram_default_bundle_option,
    requires_instagram_image_confirmation,
)
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.native_options import (
    build_native_option_catalog,
    is_native_video_option,
    native_video_codec,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import (
    AccessDeniedError,
    InvalidUrlError,
    MembershipRequiredError,
    PolicyBackendError,
    SelectionExpiredError,
    SelectionOwnershipError,
    UnsafeUrlError,
    UserRateLimitError,
)
from telegram_media_bot.domain.models import (
    ContainerPolicy,
    DownloadMode,
    ErrorCategory,
    ImageDeliveryMode,
    JobId,
    JobStatus,
    OutputContainer,
    SelectionToken,
    UserProfile,
)
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.telegram.admin_handlers import ADMIN_MENU_TEXT, build_admin_router
from telegram_media_bot.telegram.admin_menu import build_admin_main_keyboard
from telegram_media_bot.telegram.middleware import CorrelationMiddleware
from telegram_media_bot.telegram.texts import (
    ACCESS_DENIED_TEXT,
    CANCELLED_TEXT,
    CANNOT_CANCEL_TEXT,
    INSPECTION_ACTIVE_TEXT,
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
from telegram_media_bot.telegram.ui import (
    cancellation_keyboard,
    container_keyboard,
    render_media_info,
    required_channels_keyboard,
    selection_keyboard,
)
from telegram_media_bot.telegram.url_extractor import extract_first_url

logger = structlog.get_logger(__name__)


def build_router(
    *,
    settings: Settings,
    queue: JobQueue,
    repository: JobRepository,
    access_policy: AccessPolicyService,
    jobs: JobService,
    users: UserRepository,
    usage_analytics: UsageAnalyticsService | None = None,
    usage_chart_renderer: UsageChartRenderer | None = None,
    cookie_manager: CookieManager | None = None,
) -> Router:
    router = Router(name="main")
    router.message.outer_middleware(CorrelationMiddleware())
    router.callback_query.outer_middleware(CorrelationMiddleware())
    url_validator = PublicUrlValidator(
        reject_private_networks=settings.security.reject_private_network_urls
    )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        await _save_user(users, message, started=True)
        try:
            await access_policy.authorize_request(message.from_user.id)
        except MembershipRequiredError as exc:
            await message.answer(
                _membership_text(),
                reply_markup=required_channels_keyboard(exc.channels),
            )
            return
        except AccessDeniedError:
            await message.answer(ACCESS_DENIED_TEXT)
            return
        except UserRateLimitError:
            await message.answer(RATE_LIMIT_TEXT)
            return
        except PolicyBackendError:
            await message.answer(SERVICE_UNAVAILABLE_TEXT)
            return
        await message.answer(
            START_TEXT,
            reply_markup=build_admin_main_keyboard() if _is_admin(message, settings) else None,
        )

    @router.callback_query(F.data == "membership:recheck")
    async def recheck_membership(callback: CallbackQuery) -> None:
        if callback.from_user is None:
            return
        try:
            await access_policy.authorize_request(
                callback.from_user.id,
                force_membership_refresh=True,
            )
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("عضویت در همهٔ کانال‌ها هنوز تأیید نشده است.", show_alert=True)  # noqa: RUF001
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
        if isinstance(callback.message, Message):
            await callback.message.edit_text("عضویت شما تأیید شد. اکنون لینک را دوباره ارسال کنید.")
        await callback.answer("تأیید شد")

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
        await asyncio.to_thread(
            users.record_download_outcome,
            job_id=job_id,
            user_id=record.user_id,
            day=datetime.now(UTC).date(),
            succeeded=False,
        )
        await message.answer("وضعیت نامشخص بررسی‌شده علامت خورد؛ درخواست تازه اکنون مجاز است.")

    @router.callback_query(F.data.startswith("o2:"))
    async def choose_native_option(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            token, option_id = parse_native_option_callback(callback.data)
            selection = await asyncio.to_thread(
                repository.get_selection,
                token,
                callback.from_user.id,
            )
            catalog = build_native_option_catalog(selection.media)
            view = catalog.resolve(option_id)
            if view is None or view.mode not in selection.allowed_modes:
                raise SelectionOwnershipError("Native option was not offered")
            matching_option = next(
                (
                    option
                    for option in selection.media.format_options
                    if option.mode is view.mode
                    and option.container is view.container
                    and option.selected_format_ids == view.selected_format_ids
                    and option.width == view.actual_width
                    and option.height == view.actual_height
                    and option.fps == view.actual_fps
                ),
                None,
            )
            if matching_option is None:
                raise SelectionOwnershipError("Native plan no longer matches the selection")
            if view.container in {OutputContainer.MP4, OutputContainer.WEBM} and (
                view.transcode_required
                or matching_option.requires_transcode
                or not is_native_video_option(matching_option)
            ):
                raise SelectionOwnershipError("Transcoding video options are not public")
        except SelectionExpiredError:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"{SELECTION_EXPIRED_TEXT}\n{START_TEXT}")
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
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
            mode=view.mode,
            container=view.container,
            container_policy=matching_option.container_policy,
            native_video_codec=native_video_codec(view.video_codec),
            selected_format_ids=view.selected_format_ids,
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
                    mode=view.mode,
                    container=view.container,
                    container_policy=record.container_policy,
                    native_video_codec=record.native_video_codec,
                    selected_format_ids=record.selected_format_ids,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    repository.transition,
                    record.job_id,
                    JobStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL,
                    error_summary="queue_enqueue_failed",
                )
                await asyncio.to_thread(
                    users.record_download_outcome,
                    job_id=record.job_id,
                    user_id=record.user_id,
                    day=datetime.now(UTC).date(),
                    succeeded=False,
                )
                if isinstance(callback.message, Message):
                    await callback.message.edit_text("ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.")
                await logger.aexception(
                    "download_enqueue_failed", job_id=record.job_id, error_type=type(exc).__name__
                )
                await callback.answer("صف موقتاً در دسترس نیست", show_alert=True)
                return
        await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")

    @router.callback_query(F.data.startswith("i2:"))
    async def choose_instagram_image_delivery(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            _prefix, raw_token, raw_delivery_mode = callback.data.split(":", maxsplit=2)
            image_delivery_mode = ImageDeliveryMode(raw_delivery_mode)
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            selection = await asyncio.to_thread(
                repository.get_selection,
                SelectionToken(raw_token),
                callback.from_user.id,
            )
            if not requires_instagram_image_confirmation(selection.media):
                raise SelectionOwnershipError("Image delivery confirmation was not offered")
            option = instagram_default_bundle_option(selection.media)
            if option.mode not in selection.allowed_modes:
                raise SelectionOwnershipError("Instagram complete-media plan was not offered")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
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
            mode=option.mode,
            selected_format_ids=option.selected_format_ids,
            image_delivery_mode=image_delivery_mode,
        )
        if record.status is JobStatus.DELIVERY_UNCERTAIN:
            await callback.answer(
                "وضعیت ارسال قبلی نامشخص است؛ مدیر باید آن را بررسی کند.",
                show_alert=True,
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
                    mode=option.mode,
                    selected_format_ids=record.selected_format_ids,
                    image_delivery_mode=record.image_delivery_mode,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    repository.transition,
                    record.job_id,
                    JobStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL,
                    error_summary="queue_enqueue_failed",
                )
                await asyncio.to_thread(
                    users.record_download_outcome,
                    job_id=record.job_id,
                    user_id=record.user_id,
                    day=datetime.now(UTC).date(),
                    succeeded=False,
                )
                if isinstance(callback.message, Message):
                    await callback.message.edit_text("ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.")
                await logger.aexception(
                    "download_enqueue_failed",
                    job_id=record.job_id,
                    error_type=type(exc).__name__,
                )
                await callback.answer("صف موقتاً در دسترس نیست", show_alert=True)
                return
        await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")

    @router.callback_query(F.data.startswith("m2:"))
    async def choose_media_bundle(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            _prefix, raw_token, raw_mode = callback.data.split(":", maxsplit=2)
            mode = DownloadMode(raw_mode)
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            selection = await asyncio.to_thread(
                repository.get_selection,
                SelectionToken(raw_token),
                callback.from_user.id,
            )
            if requires_instagram_image_confirmation(selection.media):
                raise SelectionOwnershipError(
                    "Instagram image posts require an explicit delivery choice"
                )
            if mode not in selection.allowed_modes:
                raise SelectionOwnershipError("Media-bundle mode was not offered")
            option = next(
                (item for item in selection.media.format_options if item.mode is mode), None
            )
            if option is None:
                raise SelectionOwnershipError("Media-bundle plan is missing")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
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
            selected_format_ids=option.selected_format_ids,
        )
        if record.status is JobStatus.DELIVERY_UNCERTAIN:
            await callback.answer(
                "وضعیت ارسال قبلی نامشخص است؛ مدیر باید آن را بررسی کند.",
                show_alert=True,
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
                    selected_format_ids=record.selected_format_ids,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    repository.transition,
                    record.job_id,
                    JobStatus.FAILED,
                    error_category=ErrorCategory.INTERNAL,
                    error_summary="queue_enqueue_failed",
                )
                await asyncio.to_thread(
                    users.record_download_outcome,
                    job_id=record.job_id,
                    user_id=record.user_id,
                    day=datetime.now(UTC).date(),
                    succeeded=False,
                )
                if isinstance(callback.message, Message):
                    await callback.message.edit_text("ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.")
                await logger.aexception(
                    "download_enqueue_failed",
                    job_id=record.job_id,
                    error_type=type(exc).__name__,
                )
                await callback.answer("صف موقتاً در دسترس نیست", show_alert=True)
                return
        await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")

    @router.callback_query(F.data.startswith("c2:"))
    async def choose_native_container(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            token, container = parse_native_container_callback(callback.data)
            selection = await asyncio.to_thread(
                repository.get_selection,
                token,
                callback.from_user.id,
            )
            catalog = build_native_option_catalog(selection.media)
            if not catalog.for_container(container):
                raise SelectionOwnershipError("Container was not offered")
        except SelectionExpiredError:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"{SELECTION_EXPIRED_TEXT}\n{START_TEXT}")
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
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
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                render_media_info(selection.media, container, catalog),
                reply_markup=selection_keyboard(selection, container, catalog),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("n2:"))
    async def navigate_selection(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            token, destination = parse_navigation_callback(callback.data)
            selection = await asyncio.to_thread(
                repository.get_selection,
                token,
                callback.from_user.id,
            )
        except SelectionExpiredError:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    f"{SELECTION_EXPIRED_TEXT}\n{START_TEXT}",
                )
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        if isinstance(callback.message, Message):
            if destination == "s":
                await callback.message.edit_text(START_TEXT)
            else:
                catalog = build_native_option_catalog(selection.media)
                await callback.message.edit_text(
                    render_media_info(selection.media, catalog=catalog),
                    reply_markup=container_keyboard(selection, catalog),
                )
        await callback.answer()

    @router.callback_query(F.data.startswith("fmt:") | F.data.startswith("container:"))
    async def reject_legacy_selection(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            token = _legacy_callback_token(callback.data)
            selection = await asyncio.to_thread(
                repository.get_selection,
                token,
                callback.from_user.id,
            )
        except SelectionExpiredError:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"{SELECTION_EXPIRED_TEXT}\n{START_TEXT}")
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
            return
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
            return
        catalog = build_native_option_catalog(selection.media)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                render_media_info(selection.media, catalog=catalog),
                reply_markup=container_keyboard(selection, catalog),
            )
        await callback.answer(
            "این گزینه قدیمی شده است. یکی از خروجی‌های Native را انتخاب کنید.",
            show_alert=True,
        )

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        job_id = JobId(callback.data.removeprefix("cancel:"))
        cancellation = await asyncio.to_thread(repository.cancel_job, job_id, callback.from_user.id)
        if not cancellation.accepted:
            await callback.answer(CANNOT_CANCEL_TEXT, show_alert=True)
            return
        abort_result = None
        try:
            abort_result = await queue.abort_job(job_id)
        except Exception as exc:
            await logger.awarning(
                "job_queue_abort_failed",
                job_id=job_id,
                cancel_source="user",
                error_type=type(exc).__name__,
                final_status=JobStatus.CANCELLED.value,
            )
        if isinstance(callback.message, Message) and not cancellation.already_cancelled:
            await callback.message.edit_text(CANCELLED_TEXT)
        if (
            isinstance(callback.message, Message)
            and callback.from_user.id in settings.telegram.admin_ids
        ):
            await callback.message.answer(
                ADMIN_MENU_TEXT,
                reply_markup=build_admin_main_keyboard(),
            )
        await logger.ainfo(
            "job_cancelled",
            job_id=job_id,
            previous_status=(
                cancellation.previous_status.value if cancellation.previous_status else None
            ),
            cancel_requested=True,
            arq_job_status=(
                abort_result.previous_status.value if abort_result is not None else "unknown"
            ),
            cancel_source="user",
            abort_result=(
                abort_result.final_status.value if abort_result is not None else "failed"
            ),
            redis_keys_removed=(abort_result.redis_keys_removed if abort_result is not None else 0),
            final_status=JobStatus.CANCELLED.value,
        )
        await callback.answer("درخواست لغو ثبت شد")

    async def submit_url(
        message: Message,
        invalid_markup: ReplyKeyboardMarkup | None = None,
    ) -> bool:
        if message.from_user is None:
            return False
        await _save_user(users, message)
        try:
            await access_policy.authorize_request(message.from_user.id)
        except MembershipRequiredError as exc:
            await message.answer(
                _membership_text(),
                reply_markup=required_channels_keyboard(exc.channels),
            )
            return False
        except AccessDeniedError:
            await message.answer(ACCESS_DENIED_TEXT, reply_markup=invalid_markup)
            return False
        except UserRateLimitError:
            await message.answer(RATE_LIMIT_TEXT, reply_markup=invalid_markup)
            return False
        except PolicyBackendError:
            await message.answer(SERVICE_UNAVAILABLE_TEXT, reply_markup=invalid_markup)
            return False
        url = extract_first_url(message.text or message.caption)
        if url is None:
            await message.answer(INVALID_URL_TEXT, reply_markup=invalid_markup)
            return False
        try:
            validated = await asyncio.to_thread(url_validator.validate, url)
        except UnsafeUrlError:
            await message.answer(UNSAFE_URL_TEXT, reply_markup=invalid_markup)
            return False
        except InvalidUrlError:
            await message.answer(INVALID_URL_TEXT, reply_markup=invalid_markup)
            return False
        intent = canonicalize_media_url(validated)
        if intent.youtube_video_id is not None:
            await logger.ainfo("youtube_url_canonicalized", **intent.log_fields)
        record, created = await asyncio.to_thread(
            jobs.create_inspection,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            url=intent.canonical_url,
        )
        is_admin = message.from_user.id in settings.telegram.admin_ids
        if not created:
            try:
                await queue.enqueue_inspection(
                    job_id=record.job_id,
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    url=record.url,
                )
            except Exception as exc:
                await message.answer(
                    SERVICE_UNAVAILABLE_TEXT,
                    reply_markup=build_admin_main_keyboard() if is_admin else None,
                )
                await logger.aexception(
                    "inspection_reconcile_failed",
                    job_id=record.job_id,
                    durable_status=record.status.value,
                    error_type=type(exc).__name__,
                )
                return True
            await message.answer(
                INSPECTION_ACTIVE_TEXT,
                reply_markup=build_admin_main_keyboard() if is_admin else None,
            )
            await logger.ainfo(
                "inspection_reconciled",
                job_id=record.job_id,
                durable_status=record.status.value,
                status_message_reused=record.status_message_id is not None,
            )
            return True
        response = await message.answer(
            INSPECTION_QUEUED_TEXT.format(job_id=record.job_id),
        )
        await asyncio.to_thread(repository.set_status_message, record.job_id, response.message_id)
        if is_admin:
            await message.answer(ADMIN_MENU_TEXT, reply_markup=build_admin_main_keyboard())
        await asyncio.to_thread(
            users.record_request,
            message.from_user.id,
            datetime.now(UTC).date(),
        )
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
        return True

    router.include_router(
        build_admin_router(
            settings=settings,
            submit_url=submit_url,
            analytics=usage_analytics,
            chart_renderer=usage_chart_renderer,
            cookie_manager=cookie_manager,
        )
    )

    url_router = Router(name="url")

    @url_router.message()
    async def enqueue_url(message: Message) -> None:
        await submit_url(message)

    router.include_router(url_router)
    return router


def _is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.telegram.admin_ids


async def _save_user(
    users: UserRepository,
    message: Message,
    *,
    started: bool = False,
) -> None:
    user = message.from_user
    if user is None:
        return
    profile = UserProfile(
        user_id=user.id,
        private_chat_id=message.chat.id if message.chat.type == "private" else None,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
        is_premium=user.is_premium,
    )
    await asyncio.to_thread(users.upsert_user, profile, started=started)


async def _save_callback_user(users: UserRepository, callback: CallbackQuery) -> None:
    user = callback.from_user
    private_chat_id = (
        callback.message.chat.id
        if isinstance(callback.message, Message) and callback.message.chat.type == "private"
        else None
    )
    await asyncio.to_thread(
        users.upsert_user,
        UserProfile(
            user_id=user.id,
            private_chat_id=private_chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
            is_premium=user.is_premium,
        ),
    )


def _membership_text() -> str:
    return (
        "برای استفاده از ربات، ابتدا در همهٔ کانال‌های زیر عضو شوید؛ "
        "سپس دکمهٔ «عضو شدم، بررسی مجدد» را بزنید."
    )


def parse_selection_callback(
    data: str,
) -> tuple[SelectionToken, DownloadMode]:
    token, container, _policy, mode = _parse_selection_callback_full(data)
    if container is not None:
        raise ValueError("Container-aware callbacks require the full parser")
    return token, mode


def parse_native_option_callback(data: str) -> tuple[SelectionToken, str]:
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's limit")
    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[0] != "o2"
        or not 10 <= len(parts[1]) <= 32
        or len(parts[2]) != 16
        or any(character not in "0123456789abcdef" for character in parts[2])
    ):
        raise ValueError("Invalid native option callback")
    return SelectionToken(parts[1]), parts[2]


def parse_native_container_callback(
    data: str,
) -> tuple[SelectionToken, OutputContainer]:
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's limit")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "c2" or not 10 <= len(parts[1]) <= 32:
        raise ValueError("Invalid native container callback")
    return SelectionToken(parts[1]), OutputContainer(parts[2])


def parse_navigation_callback(data: str) -> tuple[SelectionToken, str]:
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's limit")
    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[0] != "n2"
        or not 10 <= len(parts[1]) <= 32
        or parts[2] not in {"s", "t"}
    ):
        raise ValueError("Invalid navigation callback")
    return SelectionToken(parts[1]), parts[2]


def _legacy_callback_token(data: str) -> SelectionToken:
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Callback data exceeds Telegram's limit")
    parts = data.split(":")
    if len(parts) < 3 or parts[0] not in {"fmt", "container"} or not 10 <= len(parts[1]) <= 32:
        raise ValueError("Invalid legacy callback")
    return SelectionToken(parts[1])


def _parse_selection_callback_full(
    data: str,
) -> tuple[SelectionToken, OutputContainer | None, ContainerPolicy | None, DownloadMode]:
    parts = data.split(":")
    if len(parts) not in {3, 4, 5} or parts[0] != "fmt" or not 10 <= len(parts[1]) <= 32:
        raise ValueError("Invalid selection callback")
    if len(parts) == 3:
        return SelectionToken(parts[1]), None, None, DownloadMode(parts[2])
    if len(parts) == 4:
        return SelectionToken(parts[1]), OutputContainer(parts[2]), None, DownloadMode(parts[3])
    policy = ContainerPolicy(parts[3])
    if policy is not ContainerPolicy.EXPLICIT_TRANSCODE:
        raise ValueError("Only explicit transcode policy may appear in callback data")
    return SelectionToken(parts[1]), OutputContainer(parts[2]), policy, DownloadMode(parts[4])


def parse_container_callback(data: str) -> tuple[SelectionToken, OutputContainer]:
    token, container, _policy = _parse_container_callback_full(data)
    return token, container


def _parse_container_callback_full(
    data: str,
) -> tuple[SelectionToken, OutputContainer, ContainerPolicy | None]:
    parts = data.split(":")
    if len(parts) not in {3, 4} or parts[0] != "container" or not 10 <= len(parts[1]) <= 32:
        raise ValueError("Invalid container callback")
    if len(parts) == 3:
        return SelectionToken(parts[1]), OutputContainer(parts[2]), None
    policy = ContainerPolicy(parts[3])
    if policy is not ContainerPolicy.EXPLICIT_TRANSCODE:
        raise ValueError("Only explicit transcode policy may appear in callback data")
    return SelectionToken(parts[1]), OutputContainer(parts[2]), policy
