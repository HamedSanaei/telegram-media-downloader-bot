from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from datetime import UTC, datetime
from urllib.parse import urlsplit

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from telegram_media_bot.application.ports.audit import TelegramSourceResolver
from telegram_media_bot.application.ports.cookie_management import CookieManager
from telegram_media_bot.application.ports.job_queue import JobQueue
from telegram_media_bot.application.ports.job_repository import JobRepository
from telegram_media_bot.application.ports.usage_analytics import UsageChartRenderer
from telegram_media_bot.application.ports.user_repository import UserRepository
from telegram_media_bot.application.services.access_policy import AccessPolicyService
from telegram_media_bot.application.services.audit_destination_admin import (
    LoggerDestinationAdminService,
)
from telegram_media_bot.application.services.cookie_health_service import CookieHealthService
from telegram_media_bot.application.services.effect_ledger import EffectLedgerService
from telegram_media_bot.application.services.instagram_connection import InstagramConnectionService
from telegram_media_bot.application.services.instagram_delivery import (
    instagram_default_bundle_option,
    requires_instagram_image_confirmation,
)
from telegram_media_bot.application.services.job_recovery_service import JobRecoveryService
from telegram_media_bot.application.services.job_service import JobService
from telegram_media_bot.application.services.logger_privacy import (
    LOGGER_PRIVACY_NOTICE_FA,
    LoggerPrivacyService,
)
from telegram_media_bot.application.services.native_options import (
    build_native_option_catalog,
    is_native_video_option,
    native_video_codec,
)
from telegram_media_bot.application.services.submission_audit import (
    AcceptedSubmissionAuditService,
)
from telegram_media_bot.application.services.url_canonicalization import canonicalize_media_url
from telegram_media_bot.application.services.usage_analytics import UsageAnalyticsService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.audit import TelegramSourceReference
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
    JobRecord,
    JobStatus,
    MediaFormatOption,
    MediaInfo,
    OutputContainer,
    SelectionToken,
    StoryDeliveryMode,
    UserProfile,
)
from telegram_media_bot.infrastructure.security.url_safety import PublicUrlValidator
from telegram_media_bot.telegram.admin_handlers import ADMIN_MENU_TEXT, build_admin_router
from telegram_media_bot.telegram.admin_menu import build_admin_main_keyboard
from telegram_media_bot.telegram.instagram_ux import (
    render_connect_prompt,
    render_connection_status,
    render_disconnect_confirmation,
    render_instagram_unavailable,
)
from telegram_media_bot.telegram.middleware import CorrelationMiddleware
from telegram_media_bot.telegram.texts import (
    ACCESS_DENIED_TEXT,
    CANCELLED_TEXT,
    CANNOT_CANCEL_TEXT,
    HIGHLIGHT_TRAY_QUEUED_TEXT,
    INSPECTION_ACTIVE_TEXT,
    INSPECTION_QUEUED_TEXT,
    INVALID_URL_TEXT,
    LOGGER_PRIVACY_ACKNOWLEDGED_TEXT,
    LOGGER_PRIVACY_UNAVAILABLE_TEXT,
    QUEUED_TEXT,
    RATE_LIMIT_TEXT,
    SELECTION_EXPIRED_TEXT,
    SELECTION_INVALID_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    START_TEXT,
    UNSAFE_URL_TEXT,
)
from telegram_media_bot.telegram.ui import (
    STORY_DELIVERY_MODE_PROMPT,
    cancellation_keyboard,
    container_keyboard,
    highlight_tray_keyboard,
    instagram_image_delivery_keyboard,
    logger_privacy_acknowledgement_keyboard,
    render_highlight_tray,
    render_instagram_image_delivery_prompt,
    render_media_info,
    required_channels_keyboard,
    selection_keyboard,
    story_delivery_mode_keyboard,
)
from telegram_media_bot.telegram.url_extractor import extract_first_url

logger = structlog.get_logger(__name__)
_SAFE_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")


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
    cookie_health_service: CookieHealthService | None = None,
    effects: EffectLedgerService | None = None,
    connection: InstagramConnectionService | None = None,
    audit_admin: LoggerDestinationAdminService | None = None,
    submission_audit: AcceptedSubmissionAuditService | None = None,
    source_resolver: TelegramSourceResolver | None = None,
    logger_privacy: LoggerPrivacyService | None = None,
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

    @router.callback_query(F.data.startswith("privacy:ack:"))
    async def acknowledge_logger_privacy(callback: CallbackQuery) -> None:
        if callback.from_user is None or logger_privacy is None or callback.data is None:
            await callback.answer(LOGGER_PRIVACY_UNAVAILABLE_TEXT, show_alert=True)
            return
        expected = f"privacy:ack:{logger_privacy.policy_version}"
        if callback.data != expected:
            await callback.answer(
                "نسخهٔ این تأیید منقضی شده است؛ لینک را دوباره بفرستید.", show_alert=True
            )
            return
        try:
            await asyncio.to_thread(logger_privacy.acknowledge, callback.from_user.id)
        except Exception:
            await logger.aexception(
                "logger_privacy_acknowledgement_failed",
                error_type="PrivacyAcknowledgementError",
            )
            await callback.answer(LOGGER_PRIVACY_UNAVAILABLE_TEXT, show_alert=True)
            return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(LOGGER_PRIVACY_ACKNOWLEDGED_TEXT)
        await callback.answer("تأیید ثبت شد")

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

    @router.message(Command("instagram"))
    async def instagram_command(message: Message) -> None:
        """Owner-bound Instagram connect/reconnect/disconnect/status entry point (T018)."""
        if message.from_user is None:
            return
        owner = message.from_user.id
        if connection is None:
            await message.answer(render_instagram_unavailable())
            return
        parts = (message.text or "/instagram").split()[1:]
        action = parts[0].casefold() if parts else "status"
        try:
            if action == "connect":
                link = await asyncio.to_thread(connection.create_connect_link, owner)
                await message.answer(render_connect_prompt(link))
            elif action == "disconnect":
                await asyncio.to_thread(connection.disconnect, owner)
                await message.answer(render_disconnect_confirmation())
            else:
                view = await asyncio.to_thread(connection.status, owner)
                await message.answer(render_connection_status(view))
        except Exception:
            await message.answer(render_instagram_unavailable())

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

    @router.callback_query(F.data.startswith("s2:"))
    async def choose_story_action(
        callback: CallbackQuery,
        durable_update_id: int | None = None,
    ) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            _prefix, raw_token, action = callback.data.split(":", maxsplit=2)
            selection = await asyncio.to_thread(
                repository.get_selection,
                SelectionToken(raw_token),
                callback.from_user.id,
            )
            story_url = canonicalize_media_url(selection.media.webpage_url)
            if story_url.instagram_kind != "story":
                raise SelectionOwnershipError("Story action was not offered")
            if action == "all":
                # Ask the user how the entire active-story batch should be delivered
                # before creating the bulk job. The chosen mode is persisted on the
                # durable JobRecord so it survives restart and recovery. The prompt is
                # a replay-sensitive effect: a replayed callback must not stack prompts.
                username = _story_username(selection.media.webpage_url)
                if username is None:
                    raise SelectionOwnershipError("Story account is missing")
                if isinstance(callback.message, Message):
                    prompt_message = callback.message

                    async def show_prompt() -> int:
                        await prompt_message.edit_text(
                            STORY_DELIVERY_MODE_PROMPT,
                            reply_markup=story_delivery_mode_keyboard(selection),
                        )
                        return prompt_message.message_id

                    async def reuse_prompt(message_id: int) -> None:
                        if prompt_message.bot is None:
                            return
                        with suppress(Exception):
                            await prompt_message.bot.edit_message_text(
                                STORY_DELIVERY_MODE_PROMPT,
                                chat_id=prompt_message.chat.id,
                                message_id=message_id,
                                reply_markup=story_delivery_mode_keyboard(selection),
                            )

                    if effects is not None and durable_update_id is not None:
                        await effects.send_or_reuse(
                            effect_key=f"update:{durable_update_id}:story_delivery_mode_prompt",
                            effect_type="story_delivery_mode_prompt",
                            update_id=durable_update_id,
                            chat_id=selection.chat_id,
                            send=show_prompt,
                            edit=reuse_prompt,
                        )
                    else:
                        await show_prompt()
                await callback.answer()
                return
            if action != "single":
                raise SelectionOwnershipError("Unknown story action")
            option = _single_story_option(selection.media)
            if option is None:
                raise SelectionOwnershipError("Story plan is unavailable")
            if option.mode is DownloadMode.IMAGE_ORIGINAL and requires_instagram_image_confirmation(
                selection.media
            ):
                if isinstance(callback.message, Message):
                    await callback.message.edit_text(
                        render_instagram_image_delivery_prompt(selection.media),
                        reply_markup=instagram_image_delivery_keyboard(selection),
                    )
                await callback.answer()
                return
            record, created = await asyncio.to_thread(
                jobs.create_download,
                chat_id=selection.chat_id,
                user_id=selection.owner_user_id,
                url=selection.media.webpage_url,
                mode=option.mode,
                selected_format_ids=option.selected_format_ids,
            )
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    QUEUED_TEXT.format(job_id=record.job_id),
                    reply_markup=cancellation_keyboard(record.job_id),
                )
                await asyncio.to_thread(
                    repository.set_status_message, record.job_id, callback.message.message_id
                )
            if created:
                await _enqueue_download_or_fail(
                    queue=queue,
                    repository=repository,
                    users=users,
                    record=record,
                    callback=callback,
                )
            await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
        except AccessDeniedError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        except UserRateLimitError:
            await callback.answer(RATE_LIMIT_TEXT, show_alert=True)
        except PolicyBackendError:
            await callback.answer(SERVICE_UNAVAILABLE_TEXT, show_alert=True)

    @router.callback_query(F.data.startswith("s3:"))
    async def choose_all_stories_delivery_mode(callback: CallbackQuery) -> None:
        """Create the all-active-Stories job after the user picks a delivery mode."""
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        try:
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            _prefix, raw_token, raw_mode = callback.data.split(":", maxsplit=2)
            mode = StoryDeliveryMode(raw_mode)
            selection = await asyncio.to_thread(
                repository.get_selection,
                SelectionToken(raw_token),
                callback.from_user.id,
            )
            story_url = canonicalize_media_url(selection.media.webpage_url)
            if story_url.instagram_kind != "story":
                raise SelectionOwnershipError("Story action was not offered")
            username = _story_username(selection.media.webpage_url)
            if username is None:
                raise SelectionOwnershipError("Story account is missing")
            record, created = await asyncio.to_thread(
                jobs.create_download,
                chat_id=selection.chat_id,
                user_id=selection.owner_user_id,
                url=f"https://www.instagram.com/stories/{username}/",
                mode=DownloadMode.INSTAGRAM_ALL_STORIES,
                story_delivery_mode=mode,
            )
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    QUEUED_TEXT.format(job_id=record.job_id),
                    reply_markup=cancellation_keyboard(record.job_id),
                )
                await asyncio.to_thread(
                    repository.set_status_message, record.job_id, callback.message.message_id
                )
            if created:
                await _enqueue_download_or_fail(
                    queue=queue,
                    repository=repository,
                    users=users,
                    record=record,
                    callback=callback,
                )
            await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
        except AccessDeniedError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        except UserRateLimitError:
            await callback.answer(RATE_LIMIT_TEXT, show_alert=True)
        except PolicyBackendError:
            await callback.answer(SERVICE_UNAVAILABLE_TEXT, show_alert=True)

    @router.callback_query(F.data.startswith("h2:"))
    async def highlight_tray_navigation(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        await _save_callback_user(users, callback)
        parts = callback.data.split(":", maxsplit=3)
        try:
            await access_policy.authorize_request(callback.from_user.id, consume_rate_limit=False)
            if len(parts) == 3 and parts[1] == "open":
                username = parts[2]
                _validate_instagram_username(username)
                record, created = await asyncio.to_thread(
                    jobs.create_highlight_tray,
                    chat_id=callback.message.chat.id
                    if isinstance(callback.message, Message)
                    else callback.from_user.id,
                    user_id=callback.from_user.id,
                    url=f"https://www.instagram.com/{username}/highlights/",
                    username=username,
                )
                if isinstance(callback.message, Message):
                    await callback.message.edit_text(
                        HIGHLIGHT_TRAY_QUEUED_TEXT,
                        reply_markup=None,
                    )
                    await asyncio.to_thread(
                        repository.set_status_message, record.job_id, callback.message.message_id
                    )
                if created:
                    await queue.enqueue_highlight_tray(
                        job_id=record.job_id,
                        chat_id=record.chat_id,
                        user_id=record.user_id,
                        url=record.url,
                        username=username,
                    )
                await callback.answer("در حال دریافت فهرست هایلایت‌ها…")  # noqa: RUF001
                return
            if len(parts) != 4 or parts[0] != "h2":
                raise SelectionOwnershipError("Invalid highlight callback")
            token, action, payload = parts[1], parts[2], parts[3]
            tray = await asyncio.to_thread(
                repository.get_highlight_tray,
                SelectionToken(token),
                callback.from_user.id,
            )
            if action == "close":
                if isinstance(callback.message, Message):
                    await callback.message.edit_text("فهرست هایلایت‌ها بسته شد.")  # noqa: RUF001
                await callback.answer()
                return
            if action == "page":
                page = int(payload)
                if isinstance(callback.message, Message):
                    await callback.message.edit_text(
                        render_highlight_tray(tray, page=page),
                        reply_markup=highlight_tray_keyboard(tray, page=page),
                    )
                await callback.answer()
                return
            if action != "pick":
                raise SelectionOwnershipError("Unknown highlight action")
            if payload not in {item.highlight_id for item in tray.highlights}:
                raise SelectionOwnershipError("Highlight was not offered")
            record, created = await asyncio.to_thread(
                jobs.create_download,
                chat_id=tray.chat_id,
                user_id=tray.owner_user_id,
                url=f"https://www.instagram.com/stories/highlights/{payload}/",
                mode=DownloadMode.INSTAGRAM_HIGHLIGHT,
            )
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    QUEUED_TEXT.format(job_id=record.job_id),
                    reply_markup=cancellation_keyboard(record.job_id),
                )
                await asyncio.to_thread(
                    repository.set_status_message, record.job_id, callback.message.message_id
                )
            if created:
                await _enqueue_download_or_fail(
                    queue=queue,
                    repository=repository,
                    users=users,
                    record=record,
                    callback=callback,
                )
            await callback.answer("ثبت شد" if created else "این دانلود از قبل فعال است")
        except SelectionExpiredError:
            await callback.answer(SELECTION_EXPIRED_TEXT, show_alert=True)
        except SelectionOwnershipError, ValueError:
            await callback.answer(SELECTION_INVALID_TEXT, show_alert=True)
        except MembershipRequiredError as exc:
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _membership_text(),
                    reply_markup=required_channels_keyboard(exc.channels),
                )
            await callback.answer("ابتدا در کانال‌های الزامی عضو شوید.", show_alert=True)
        except AccessDeniedError:
            await callback.answer(ACCESS_DENIED_TEXT, show_alert=True)
        except UserRateLimitError:
            await callback.answer(RATE_LIMIT_TEXT, show_alert=True)
        except PolicyBackendError:
            await callback.answer(SERVICE_UNAVAILABLE_TEXT, show_alert=True)

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
        *,
        durable_update_id: int | None = None,
    ) -> bool:
        if message.from_user is None:
            return False
        if submission_audit is not None and message.media_group_id is not None:
            try:
                await asyncio.to_thread(
                    submission_audit.observe_media_group_member,
                    TelegramSourceReference(
                        chat_id=message.chat.id,
                        message_ids=(message.message_id,),
                        media_group_id=message.media_group_id,
                    ),
                )
            except Exception:
                await logger.aexception(
                    "submission_audit_group_extension_failed",
                    update_id=durable_update_id,
                    error_type="AuditExtensionError",
                )
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
        if logger_privacy is not None:
            try:
                privacy_required = await asyncio.to_thread(
                    logger_privacy.requires_acknowledgement,
                    message.from_user.id,
                )
            except Exception:
                # Fail the mirror closed, but never fail ordinary download acceptance.
                privacy_required = False
                await logger.aexception(
                    "logger_privacy_check_failed",
                    error_type="PrivacyCheckError",
                )
            if privacy_required:
                await message.answer(
                    LOGGER_PRIVACY_NOTICE_FA,
                    reply_markup=logger_privacy_acknowledgement_keyboard(
                        logger_privacy.policy_version
                    ),
                )
                return False
        if intent.youtube_video_id is not None:
            await logger.ainfo("youtube_url_canonicalized", **intent.log_fields)
        record, created = await asyncio.to_thread(
            jobs.create_inspection,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            url=intent.canonical_url,
        )
        if submission_audit is not None:
            try:
                source = await asyncio.to_thread(
                    _submission_source_reference,
                    message,
                    source_resolver,
                )
                await asyncio.to_thread(
                    submission_audit.record_accepted,
                    source=source,
                    telegram_user_id=message.from_user.id,
                    update_id=durable_update_id,
                    job_id=str(record.job_id),
                    content_type=_submission_content_type(message),
                    provider=_submission_provider(intent.canonical_url),
                    occurred_at=message.date.astimezone(UTC),
                )
            except Exception:
                # Audit durability is secondary: a logger fault must never change acceptance.
                await logger.aexception(
                    "submission_audit_emit_failed",
                    job_id=record.job_id,
                    update_id=durable_update_id,
                    error_type="AuditEmitError",
                )
        is_admin = message.from_user.id in settings.telegram.admin_ids
        effect_key = (
            f"update:{durable_update_id}:inspection_status"
            if durable_update_id is not None
            else None
        )
        admin_markup = build_admin_main_keyboard() if is_admin else None

        async def send_inspection_status(
            text: str,
            *,
            reply_markup: InlineKeyboardMarkup
            | ReplyKeyboardMarkup
            | ReplyKeyboardRemove
            | ForceReply
            | None = None,
        ) -> int | None:
            """Replay-safe inspection status: reuse/skip when already sent for this update."""
            if effects is None or effect_key is None:
                response = await message.answer(text, reply_markup=reply_markup)
                return response.message_id

            async def send_fresh() -> int:
                response = await message.answer(text, reply_markup=reply_markup)
                return response.message_id

            async def edit_existing(message_id: int) -> None:
                if message.bot is None:
                    return
                with suppress(Exception):
                    await message.bot.edit_message_text(
                        text,
                        chat_id=message.chat.id,
                        message_id=message_id,
                    )

            outcome = await effects.send_or_reuse(
                effect_key=effect_key,
                effect_type="inspection_status",
                update_id=durable_update_id,
                chat_id=message.chat.id,
                send=send_fresh,
                edit=edit_existing,
            )
            return outcome.message_id

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
                    reply_markup=admin_markup,
                )
                await logger.aexception(
                    "inspection_reconcile_failed",
                    job_id=record.job_id,
                    durable_status=record.status.value,
                    error_type=type(exc).__name__,
                )
                return True
            status_message_id = await send_inspection_status(
                INSPECTION_ACTIVE_TEXT, reply_markup=admin_markup
            )
            if status_message_id is not None:
                await asyncio.to_thread(
                    repository.set_status_message, record.job_id, status_message_id
                )
            await logger.ainfo(
                "inspection_reconciled",
                job_id=record.job_id,
                durable_status=record.status.value,
                status_message_reused=record.status_message_id is not None,
            )
            return True
        status_message_id = await send_inspection_status(
            INSPECTION_QUEUED_TEXT.format(job_id=record.job_id)
        )
        if status_message_id is not None:
            await asyncio.to_thread(repository.set_status_message, record.job_id, status_message_id)
        if is_admin:
            await message.answer(ADMIN_MENU_TEXT, reply_markup=admin_markup)
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
            if status_message_id is not None and message.bot is not None:
                with suppress(Exception):
                    await message.bot.edit_message_text(
                        "ثبت کار در صف ممکن نشد؛ دوباره تلاش کنید.",
                        chat_id=message.chat.id,
                        message_id=status_message_id,
                    )
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
            cookie_health_service=cookie_health_service,
            audit_admin=audit_admin,
            recovery_service=JobRecoveryService(
                repository,
                queue,
                max_attempts=settings.recovery.max_recovery_attempts,
                max_age_days=settings.recovery.max_recoverable_age_days,
                remediation_batch_size=settings.recovery.remediation_batch_size,
                queue_pressure_threshold=settings.recovery.effective_queue_pressure_threshold(
                    settings.queue.max_jobs
                ),
                max_recovery_per_user=settings.recovery.max_recovery_per_user,
                queue_depth_probe=queue.queue_depth,
            ),
        )
    )

    url_router = Router(name="url")

    @url_router.message()
    async def enqueue_url(message: Message, durable_update_id: int | None = None) -> None:
        await submit_url(message, durable_update_id=durable_update_id)

    router.include_router(url_router)
    return router


def _is_admin(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.telegram.admin_ids


def _submission_source_reference(
    message: Message,
    resolver: TelegramSourceResolver | None,
) -> TelegramSourceReference:
    message_ids: tuple[int, ...] = (message.message_id,)
    if message.media_group_id is not None and resolver is not None:
        resolved = resolver.media_group_message_ids(message.chat.id, message.media_group_id)
        message_ids = tuple(sorted({*resolved, message.message_id}))
    return TelegramSourceReference(
        chat_id=message.chat.id,
        message_ids=message_ids,
        media_group_id=message.media_group_id,
    )


def _submission_content_type(message: Message) -> str:
    for field in ("photo", "video", "document", "audio", "animation"):
        if getattr(message, field, None) is not None:
            return field
    return "text"


def _submission_provider(url: str) -> str | None:
    hostname = (urlsplit(url).hostname or "").casefold()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname if _SAFE_PROVIDER.fullmatch(hostname) else None


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


async def _enqueue_download_or_fail(
    *,
    queue: JobQueue,
    repository: JobRepository,
    users: UserRepository,
    record: JobRecord,
    callback: CallbackQuery,
) -> bool:
    try:
        await queue.enqueue_download(
            job_id=record.job_id,
            chat_id=record.chat_id,
            user_id=record.user_id,
            url=record.url,
            mode=record.mode or DownloadMode.BEST,
            container=record.container,
            container_policy=record.container_policy,
            native_video_codec=record.native_video_codec,
            selected_format_ids=record.selected_format_ids,
            image_delivery_mode=record.image_delivery_mode,
            story_delivery_mode=record.story_delivery_mode,
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
        return False
    return True


def _story_username(webpage_url: str) -> str | None:
    intent = canonicalize_media_url(webpage_url)
    if intent.instagram_kind != "story":
        return None
    parts = [part for part in urlsplit(intent.canonical_url).path.split("/") if part]
    # canonical: /stories/USERNAME/MEDIA_ID/
    if len(parts) >= 3 and parts[0] == "stories":
        return parts[1]
    return None


def _single_story_option(media: MediaInfo) -> MediaFormatOption | None:
    single = {
        DownloadMode.IMAGE_ORIGINAL,
        DownloadMode.VIDEO_ORIGINAL,
    }
    return next((option for option in media.format_options if option.mode in single), None)


def _validate_instagram_username(username: str) -> None:
    if not 1 <= len(username) <= 64:
        raise ValueError("Invalid Instagram username")
    if not all(
        character.isascii() and (character.isalnum() or character in "._") for character in username
    ):
        raise ValueError("Invalid Instagram username")


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
