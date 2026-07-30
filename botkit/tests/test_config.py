from __future__ import annotations

import pytest

from botkit import BotSettings


def make(**kwargs) -> BotSettings:
    kwargs.setdefault("bot_token", "test")
    return BotSettings(**kwargs)


def test_owner_is_always_an_admin():
    assert make(owner_id=42).admins() == {42}


def test_extra_admins_are_parsed():
    settings = make(owner_id=1, admin_ids="2, 3;4")
    assert settings.admins() == {1, 2, 3, 4}


def test_garbage_in_admin_ids_is_ignored():
    """Заказчик копирует id откуда придётся — на мусоре падать нельзя."""
    settings = make(owner_id=1, admin_ids="2, @vasya, , не число, 3")
    assert settings.admins() == {1, 2, 3}


def test_channel_style_negative_ids_are_kept():
    assert -1001234567890 in make(owner_id=1, admin_ids="-1001234567890").admins()


def test_is_admin():
    settings = make(owner_id=1, admin_ids="2")
    assert settings.is_admin(2)
    assert not settings.is_admin(3)
    assert not settings.is_admin(None)


def test_require_reports_every_missing_field():
    settings = BotSettings(bot_token="", owner_id=0)
    with pytest.raises(SystemExit) as exc:
        settings.require()
    assert "BOT_TOKEN" in str(exc.value)
    assert "OWNER_ID" in str(exc.value)


def test_require_accepts_custom_fields():
    class Settings(BotSettings):
        channel: str = ""

    settings = Settings(bot_token="test", owner_id=1)
    with pytest.raises(SystemExit) as exc:
        settings.require("bot_token", "channel")
    assert "CHANNEL" in str(exc.value)
    assert "BOT_TOKEN" not in str(exc.value)


def test_require_passes_when_configured():
    make(owner_id=1).require()
