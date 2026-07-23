from pathlib import Path


def test_external_plugin_scaffold_uses_namespace_package() -> None:
    root = Path("plugins/example_extractor")
    extractor = root / "src/yt_dlp_plugins/extractor/example_public_media.py"
    assert extractor.is_file()
    text = extractor.read_text(encoding="utf-8")
    assert "from yt_dlp.extractor.common import InfoExtractor" in text
    assert "src/telegram_media_bot" not in text
    assert not (root / "src/yt_dlp_plugins/__init__.py").exists()
