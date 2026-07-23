from typing import Protocol


class UrlValidator(Protocol):
    def validate(self, url: str) -> str:
        """Return the normalized URL or raise a project-owned validation error."""
        ...
