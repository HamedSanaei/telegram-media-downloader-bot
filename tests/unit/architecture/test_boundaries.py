import ast
from pathlib import Path


def test_yt_dlp_import_is_confined_to_adapter() -> None:
    source_root = Path("src/telegram_media_bot")
    allowed = source_root / "infrastructure/ytdlp"
    violations: list[Path] = []

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str]
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(
                name == "yt_dlp" or name.startswith("yt_dlp.") for name in modules
            ) and not path.is_relative_to(allowed):
                violations.append(path)

    assert violations == []


def test_gallery_dl_is_never_imported_into_application_code() -> None:
    violations: list[Path] = []
    for path in Path("src/telegram_media_bot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(name == "gallery_dl" or name.startswith("gallery_dl.") for name in modules):
                violations.append(path)

    assert violations == []


def test_telethon_is_not_imported() -> None:
    source_root = Path("src/telegram_media_bot")
    violations: list[Path] = []

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(name == "telethon" or name.startswith("telethon.") for name in modules):
                violations.append(path)

    assert violations == []
