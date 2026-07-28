import json

import pytest

from telegram_media_bot.infrastructure.ytdlp.native_selection_smoke import main


def test_packaged_native_selection_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert result["mp4_format_ids"] == ["137", "140"]
    assert result["mp4_transcode_required"] is False
    assert result["webm_format_ids"] == ["248", "251"]
    assert result["best_original_policy"] == "native_only"
    assert result["mp4_merger_args"] == [
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
    ]
