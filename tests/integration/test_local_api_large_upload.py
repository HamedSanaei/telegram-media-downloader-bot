from __future__ import annotations

import os
from pathlib import Path

import pytest
from aiogram.types import FSInputFile

from telegram_media_bot.bootstrap.config import load_settings
from telegram_media_bot.telegram.bot_factory import create_telegram_runtime


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
    message = None
    try:
        message = await runtime.bot.send_document(
            chat_id=settings.telegram.admin_ids[0],
            document=FSInputFile(payload),
            caption="Local Bot API >200 MB integration test",
            request_timeout=settings.telegram.upload_timeout_seconds,
        )
        assert message.document is not None
        assert message.document.file_size is not None
        assert message.document.file_size > 200 * 1024 * 1024
    finally:
        if message is not None:
            await runtime.bot.delete_message(
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        await runtime.bot.session.close()
