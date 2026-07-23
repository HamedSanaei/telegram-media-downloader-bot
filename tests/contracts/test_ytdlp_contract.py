import os

import pytest

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.ytdlp.engine import YtDlpEngine


@pytest.mark.contract
def test_operator_supplied_public_url_inspection(settings: Settings) -> None:
    if os.environ.get("RUN_CONTRACT_TESTS") != "1":
        pytest.skip("Set RUN_CONTRACT_TESTS=1 to enable external contract tests")
    url = os.environ.get("CONTRACT_MEDIA_URL")
    if not url:
        pytest.skip("Set CONTRACT_MEDIA_URL to an operator-selected safe public media URL")

    info = YtDlpEngine(settings).inspect(url)
    assert info.media_id
    assert info.title
    assert info.source
