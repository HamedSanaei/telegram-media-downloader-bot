from __future__ import annotations

from datetime import datetime
from typing import Protocol

from telegram_media_bot.domain.analytics import UsageActivity, UsageReport


class UsageAnalyticsRepository(Protocol):
    def load_activity(self, start_at: datetime, end_at: datetime) -> tuple[UsageActivity, ...]: ...


class UsageChartRenderer(Protocol):
    def render(self, report: UsageReport) -> bytes: ...
