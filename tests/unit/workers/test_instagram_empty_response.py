from telegram_media_bot.domain.errors import GalleryDlEmptyResponseError
from telegram_media_bot.telegram.texts import (
    GALLERY_EXTRACTION_TEXT,
    INSTAGRAM_EMPTY_RESPONSE_TEXT,
)
from telegram_media_bot.workers.jobs import _controlled_failure_text


def test_empty_instagram_response_explains_uncertainty_not_deletion() -> None:
    error = GalleryDlEmptyResponseError("no result", extractor="instagram")
    message = _controlled_failure_text(error)
    assert message == INSTAGRAM_EMPTY_RESPONSE_TEXT
    assert "تأیید نشده" in message
    assert "کوکی" in message


def test_empty_other_provider_uses_generic_extraction_message() -> None:
    error = GalleryDlEmptyResponseError("no result", extractor="pinterest")
    assert _controlled_failure_text(error) == GALLERY_EXTRACTION_TEXT
