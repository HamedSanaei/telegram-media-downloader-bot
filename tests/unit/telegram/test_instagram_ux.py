from telegram_media_bot.telegram.instagram_ux import render_vip_dashboard


def test_vip_dashboard_is_safe_when_provider_is_unavailable() -> None:
    text = render_vip_dashboard(
        active=False,
        authorized_until=None,
        plans=(),
        credential=None,
        payment_available=False,
    )
    assert "خرید VIP در حال حاضر در دسترس نیست." in text
    assert "⭐ VIP" in text
