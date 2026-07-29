import json

import pytest

from telegram_media_bot.telegram.native_ui_smoke import main


def test_packaged_native_ui_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["mp4_heights"] == [2160, 1080]
    assert payload["mp4_codecs"] == ["av01.0.12M.08", "avc1.640028"]
    assert payload["webm_heights"] == [2160]
    assert payload["hidden_transcode_options"] == 0
    assert payload["generic_video_buttons"] is False
    assert payload["all_video_options_native"] is True
