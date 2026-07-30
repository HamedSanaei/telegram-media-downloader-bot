from __future__ import annotations

from telegram_media_bot.infrastructure.analytics import usage_chart_doctor


def test_usage_chart_doctor_checks_font_and_renderer() -> None:
    assert usage_chart_doctor.check_usage_chart_runtime() == {
        "usage_chart_font": (True, "bundled Noto Sans decoded with required glyphs"),
        "usage_chart_renderer": (True, "in-memory PNG smoke passed"),
    }


def test_usage_chart_doctor_reports_actionable_font_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def fail() -> None:
        raise ValueError("fixture failure")

    monkeypatch.setattr(usage_chart_doctor, "validate_chart_font", fail)

    checks = usage_chart_doctor.check_usage_chart_runtime()

    assert checks["usage_chart_font"][0] is False
    assert "reinstall" in checks["usage_chart_font"][1]
    assert checks["usage_chart_renderer"] == (False, "font validation failed")
