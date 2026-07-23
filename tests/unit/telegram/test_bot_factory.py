from telegram_media_bot.bootstrap.config import Settings
from telegram_media_bot.telegram.bot_factory import create_bot


async def test_bot_factory_supports_official_and_local_api(settings: Settings) -> None:
    official = create_bot(settings)
    assert not official.session.api.is_local
    await official.session.close()

    raw = settings.model_dump()
    raw["telegram"]["local_api_base_url"] = "http://telegram-bot-api:8081"
    raw["telegram"]["local_api_is_local"] = True
    configured = Settings.model_validate(raw)
    local = create_bot(configured)
    assert local.session.api.is_local
    await local.session.close()
