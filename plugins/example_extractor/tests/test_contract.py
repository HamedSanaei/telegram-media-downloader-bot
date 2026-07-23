import os

import pytest
from yt_dlp import YoutubeDL


@pytest.mark.contract
def test_operator_owned_plugin_fixture() -> None:
    url = os.environ.get("PLUGIN_CONTRACT_URL")
    if os.environ.get("RUN_PLUGIN_CONTRACT_TESTS") != "1" or not url:
        pytest.skip("Set RUN_PLUGIN_CONTRACT_TESTS=1 and PLUGIN_CONTRACT_URL")
    with YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    assert info is not None
    assert info.get("id")
    assert info.get("title")
