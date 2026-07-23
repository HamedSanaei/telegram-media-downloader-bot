from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, cast

import structlog
from aiogram import Bot
from aiogram.types import FSInputFile

from telegram_media_bot.application.services.download_service import DownloadService
from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.domain.errors import MediaBotError
from telegram_media_bot.domain.models import DownloadMode, JobId

logger = structlog.get_logger(__name__)


async def process_download_job(
    ctx: dict[str, Any],
    *,
    chat_id: int,
    user_id: int,
    url: str,
    mode: str,
) -> str:
    settings = cast(Settings, ctx["settings"])
    bot = cast(Bot, ctx["bot"])
    service = cast(DownloadService, ctx["download_service"])
    arq_job_id = str(ctx.get("job_id") or "unknown")
    job_id = JobId(arq_job_id)
    output_directory = settings.storage.downloads_path() / arq_job_id

    await logger.ainfo("download_started", job_id=arq_job_id, user_id=user_id, chat_id=chat_id)
    try:
        result = await asyncio.to_thread(
            service.download,
            job_id=job_id,
            url=url,
            mode=DownloadMode(mode),
            output_directory=output_directory,
        )
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(result.file_path, filename=result.file_path.name),
            caption=result.title[:1024],
        )
        await logger.ainfo(
            "download_completed",
            job_id=arq_job_id,
            source=result.source,
            file_size_bytes=result.file_size_bytes,
        )
        return arq_job_id
    except MediaBotError as exc:
        await logger.awarning(
            "download_controlled_failure",
            job_id=arq_job_id,
            error_type=type(exc).__name__,
        )
        await bot.send_message(
            chat_id=chat_id,
            text="پردازش این لینک ممکن نبود. محتوا ممکن است حذف، خصوصی، محدود یا بیش از حد مجاز باشد.",
        )
        raise
    except Exception:
        await logger.aexception("download_unexpected_failure", job_id=arq_job_id)
        await bot.send_message(chat_id=chat_id, text="پردازش این لینک با خطا مواجه شد.")
        raise
    finally:
        if settings.storage.delete_after_upload:
            await asyncio.to_thread(_safe_remove, output_directory)


def _safe_remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
