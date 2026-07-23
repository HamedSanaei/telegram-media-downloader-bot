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
            if any(name == "yt_dlp" or name.startswith("yt_dlp.") for name in modules):
                if not path.is_relative_to(allowed):
                    violations.append(path)

    assert violations == []
