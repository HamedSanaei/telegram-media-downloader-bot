from __future__ import annotations

import os
from pathlib import Path

import pytest

from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.domain.models import (
    DeliveryProgressEvent,
    DeliveryStage,
    DownloadResult,
    JobId,
    MediaKind,
)
from telegram_media_bot.telegram.bot_factory import create_telegram_runtime
from telegram_media_bot.telegram.delivery import TelegramDeliveryGateway


@pytest.mark.integration
@pytest.mark.large_file
async def test_real_local_api_upload_larger_than_200_mb(tmp_path: Path) -> None:
    if os.environ.get("RUN_LOCAL_API_LARGE_FILE_TEST") != "1":
        pytest.skip("set RUN_LOCAL_API_LARGE_FILE_TEST=1 for the destructive opt-in test")
    settings = load_settings(require_token=True)
    if not settings.telegram.local_bot_api.enabled:
        pytest.skip("Local Bot API is not enabled")
    if not settings.telegram.admin_ids:
        pytest.skip("telegram.admin_ids must contain the private test chat ID")
    runtime = create_telegram_runtime(settings, manage_lifecycle=False)
    if runtime.endpoint != "local":
        pytest.skip("the migration state is not local")

    payload = tmp_path / "local-api-201mb.bin"
    with payload.open("wb") as stream:
        stream.seek(201 * 1024 * 1024 - 1)
        stream.write(b"\0")
    receipt = None
    progress: list[DeliveryProgressEvent] = []
    try:
        gateway = TelegramDeliveryGateway(runtime.bot, settings)
        receipt = await gateway.deliver(
            chat_id=settings.telegram.admin_ids[0],
            result=DownloadResult(
                job_id=JobId("local-api-large-file-contract"),
                media_id="large-file-contract",
                title="Local API large file contract",
                source="integration",
                kind=MediaKind.UNKNOWN,
                file_path=payload,
                file_size_bytes=payload.stat().st_size,
                mime_type="application/octet-stream",
            ),
            caption="Local Bot API >200 MB integration test",
            progress=progress.append,
        )
        assert any(event.stage is DeliveryStage.UPLOADING for event in progress)
        assert any(event.stage is DeliveryStage.FINALIZING for event in progress)
        assert (
            max(
                event.transferred_bytes
                for event in progress
                if event.stage is DeliveryStage.UPLOADING
            )
            > 200 * 1024 * 1024
        )
    finally:
        if receipt is not None:
            await runtime.bot.delete_message(
                chat_id=settings.telegram.admin_ids[0],
                message_id=receipt.message_id,
            )
        await runtime.bot.session.close()
