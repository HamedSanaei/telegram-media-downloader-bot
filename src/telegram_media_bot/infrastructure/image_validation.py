from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from telegram_media_bot.bootstrap.config import ImageValidationSection
from telegram_media_bot.domain.errors import ImageFormatUnsupportedError, ImageValidationError

_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "AVIF"}


def validate_image(path: Path, settings: ImageValidationSection) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise ImageValidationError("Image is missing, empty, or not a regular file")
    with path.open("rb") as stream:
        header = stream.read(512).lstrip().lower()
    if header.startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ImageValidationError("Downloaded image contains markup")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in _FORMATS:
                    raise ImageFormatUnsupportedError("Image format is unsupported")
                if (
                    width <= 0
                    or height <= 0
                    or width > settings.max_width
                    or height > settings.max_height
                    or width * height > settings.max_pixels
                ):
                    raise ImageValidationError("Image dimensions exceed the configured limit")
                image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ImageValidationError("Image signature or contents are invalid") from exc
    return image_format.casefold()
