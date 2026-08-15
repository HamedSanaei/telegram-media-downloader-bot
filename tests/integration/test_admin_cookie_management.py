from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.infrastructure.cookies.manager import NetscapeCookieManager
from telegram_media_bot.telegram.admin_handlers import build_admin_router

_HEADER = b"# Netscape HTTP Cookie File\n"


class _State:
    def __init__(self) -> None:
        self.cleared = 0

    async def set_state(self, _value: object) -> None:
        return None

    async def clear(self) -> None:
        self.cleared += 1


class _TelegramBot:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def download(self, _document: object, *, destination: object) -> object:
        destination.write(self._content)  # type: ignore[attr-defined]
        return destination


class _Message:
    def __init__(self, user_id: int, *, upload: bytes | None = None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.text = None
        self.caption = None
        self.document = (
            SimpleNamespace(file_size=len(upload), file_name="filename-is-not-trusted.bin")
            if upload is not None
            else None
        )
        self.bot = _TelegramBot(upload or b"")
        self.answers: list[str] = []
        self.documents: list[object] = []

    async def answer(self, text: str, **_kwargs: object) -> object:
        self.answers.append(text)
        return SimpleNamespace()

    async def answer_document(self, document: object, **_kwargs: object) -> None:
        self.documents.append(document)


async def test_private_admin_upload_and_complete_export_use_one_canonical_file(
    settings: Settings,
    tmp_path: Path,
) -> None:
    raw = settings.model_dump()
    raw["telegram"]["admin_ids"] = [99]
    configured = Settings.model_validate(raw)
    youtube = _record(".youtube.com", "SID", "youtube-preserved")
    canonical = tmp_path / "cookies.txt"
    canonical.write_bytes(
        _HEADER + youtube + _record(".instagram.com", "sessionid", "instagram-old")
    )
    canonical.chmod(0o600)
    upload = _HEADER + _record(".instagram.com", "sessionid", "instagram-new")

    async def submit(_message: object, _markup: object) -> bool:
        return True

    router = build_admin_router(
        settings=configured,
        submit_url=submit,
        analytics=None,
        chart_renderer=None,
        cookie_manager=NetscapeCookieManager(canonical),
    )
    upload_message = _Message(99, upload=upload)

    await _handler(router, "receive_cookie_upload")(upload_message, _State())

    assert youtube in canonical.read_bytes()
    assert b"instagram-new" in canonical.read_bytes()
    assert b"instagram-old" not in canonical.read_bytes()

    export_message = _Message(99)
    await _handler(router, "download_combined_cookies")(export_message, _State())

    assert len(export_message.documents) == 1
    assert export_message.documents[0].data == canonical.read_bytes()  # type: ignore[attr-defined]


def _handler(router: object, name: str) -> Any:
    for observer in router.observers.values():  # type: ignore[attr-defined]
        for item in observer.handlers:
            if item.callback.__name__ == name:
                return item.callback
    raise AssertionError(f"handler {name} not found")


def _record(domain: str, name: str, value: str) -> bytes:
    return f"{domain}\tTRUE\t/\tTRUE\t0\t{name}\t{value}\n".encode()
