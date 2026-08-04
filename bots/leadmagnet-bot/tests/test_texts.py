from __future__ import annotations

from leadmagnet.config import Settings
from leadmagnet.models import KIND_FILE, Magnet
from leadmagnet.texts import magnet_links, magnets_list, unknown_code, welcome


def magnet(code: str = "чеклист", **kwargs) -> Magnet:
    kwargs.setdefault("title", "Чек-лист")
    kwargs.setdefault("body", "материал")
    return Magnet(id=1, code=code, **kwargs)


def test_welcome_is_built_from_the_magnet_list(settings: Settings):
    text = welcome(settings, [magnet()])
    assert "чеклист" in text
    assert "Чек-лист" in text


def test_custom_welcome_wins(settings: Settings):
    settings.welcome_text = "Своё приветствие"
    assert welcome(settings, [magnet()]) == "Своё приветствие"


def test_welcome_without_ready_magnets_says_so(settings: Settings):
    empty = Magnet(id=1, code="файл", title="Файл", kind=KIND_FILE)
    assert "скоро появятся" in welcome(settings, [empty])


def test_unknown_code_lists_available_ones(settings: Settings):
    assert "чеклист" in unknown_code([magnet()])


def test_unfinished_magnet_is_flagged_in_the_admin_list(settings: Settings):
    unfinished = Magnet(id=1, code="файл", title="Файл", kind=KIND_FILE)
    text = magnets_list([unfinished], settings)
    assert "нечего выдавать" in text


def test_links_include_source_labels(settings: Settings):
    text = magnet_links(magnet(), settings, ["instagram", "vk"])
    assert "https://t.me/testbot?start=чеклист" in text
    assert "https://t.me/testbot?start=чеклист__instagram" in text
    assert "https://t.me/testbot?start=чеклист__vk" in text


def test_start_link_warns_when_username_is_missing(settings: Settings):
    settings.bot_username = ""
    assert "BOT_USERNAME" in settings.start_link("чеклист")


def test_channel_link_is_built_from_username(settings: Settings):
    settings.required_channel = "@my_channel"
    assert settings.channel_link() == "https://t.me/my_channel"

    settings.channel_url = "https://t.me/+privateinvite"
    assert settings.channel_link() == "https://t.me/+privateinvite"


def test_numeric_channel_without_url_has_no_link(settings: Settings):
    """У приватного канала нет публичного адреса — кнопку строить не из чего."""
    settings.required_channel = "-1001234567890"
    assert settings.channel_link() == ""


def test_subscription_check_is_off_without_a_channel(settings: Settings):
    assert not settings.check_subscription
    settings.required_channel = "@my_channel"
    assert settings.check_subscription
