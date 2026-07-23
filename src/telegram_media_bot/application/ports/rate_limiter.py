from typing import Protocol


class RateLimiter(Protocol):
    async def allow(self, user_id: int, limit: int) -> bool: ...

    async def close(self) -> None: ...
