import re

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_first_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,;!?)\"]}") if match else None
