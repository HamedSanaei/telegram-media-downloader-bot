from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import Cookie
from pathlib import Path
from threading import Barrier

import pytest

from telegram_media_bot.infrastructure.ytdlp.engine import YoutubeDL


def _write_cookie(path: Path, value: str) -> bytes:
    payload = f"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{value}\n".encode()
    path.write_bytes(payload)
    return payload


def _replacement(value: str) -> Cookie:
    return Cookie(
        version=0,
        name="sessionid",
        value=value,
        port=None,
        port_specified=False,
        domain=".instagram.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )


def test_concurrent_real_ytdlp_contexts_cannot_overwrite_shared_cookie(tmp_path: Path) -> None:
    source = tmp_path / "operator.txt"
    original = _write_cookie(source, "synthetic-operator")
    barrier = Barrier(2)

    def mutate(value: str) -> tuple[str | None, str | None]:
        with YoutubeDL({"cookiefile": str(source), "quiet": True}) as ydl:
            observed = next(iter(ydl.cookiejar)).value
            barrier.wait(timeout=10)
            ydl.cookiejar.set_cookie(_replacement(value))
            ydl.save_cookies()
            return observed, next(iter(ydl.cookiejar)).value

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(mutate, ["synthetic-one", "synthetic-two"]))

    assert results == [
        ("synthetic-operator", "synthetic-one"),
        ("synthetic-operator", "synthetic-two"),
    ]
    assert source.read_bytes() == original


@pytest.mark.parametrize("fail", [False, True])
def test_user_cookie_jars_are_read_only_and_isolated_on_success_or_failure(
    tmp_path: Path, fail: bool
) -> None:
    operator = tmp_path / "operator.txt"
    user = tmp_path / "personal.txt"
    operator_bytes = _write_cookie(operator, "synthetic-operator")
    user_bytes = _write_cookie(user, "synthetic-owner")

    try:
        with YoutubeDL({"cookiefile": str(user), "quiet": True}) as ydl:
            assert next(iter(ydl.cookiejar)).value == "synthetic-owner"
            ydl.cookiejar.set_cookie(_replacement("synthetic-refreshed"))
            if fail:
                raise RuntimeError("synthetic extraction failure")
    except RuntimeError:
        if not fail:
            raise

    assert user.read_bytes() == user_bytes
    assert operator.read_bytes() == operator_bytes
