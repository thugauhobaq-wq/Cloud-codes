"""Сборка письма и настройки SMTP. Наружу ничего не отправляем."""

from __future__ import annotations

import json
from datetime import date

import pytest

from proposals import mailer
from proposals.delivery import Delivery
from proposals.mailer import MailError, SmtpConfig, build_message, deliver
from proposals.models import LineItem, Party, Proposal
from proposals.sample import demo_proposal


@pytest.fixture
def config() -> SmtpConfig:
    return SmtpConfig(host="smtp.example.com", user="me@example.com", password="секрет")


@pytest.fixture
def proposal() -> Proposal:
    return demo_proposal(date(2026, 7, 28))


def part_types(message) -> list[str]:
    return [part.get_content_type() for part in message.walk()]


# --- письмо --------------------------------------------------------------------


def test_message_has_text_html_and_pdf(config, proposal):
    message = build_message(config, proposal, b"%PDF-1.7 fake", to="client@example.com")

    assert "text/plain" in part_types(message)
    assert "text/html" in part_types(message)
    assert "application/pdf" in part_types(message)

    attachment = next(
        part for part in message.walk() if part.get_content_type() == "application/pdf"
    )
    assert attachment.get_filename() == proposal.filename
    assert attachment.get_payload(decode=True) == b"%PDF-1.7 fake"


def test_subject_and_sender(config, proposal):
    message = build_message(config, proposal, b"pdf", to="client@example.com")
    assert message["Subject"] == (
        "Коммерческое предложение № 47 — Разработка сайта-каталога "
        "и подключение онлайн-оплаты"
    )
    assert message["To"] == "client@example.com"
    assert "me@example.com" in message["From"]
    assert proposal.sender.name in message["From"]


def test_reply_to_points_at_the_sender_of_the_proposal(config, proposal):
    """Отправляем через свой ящик, а отвечать заказчик должен исполнителю."""
    message = build_message(config, proposal, b"pdf", to="client@example.com")
    assert message["Reply-To"] == proposal.sender.email


def test_no_reply_to_when_it_matches_the_smtp_account(proposal):
    config = SmtpConfig(host="smtp.example.com", user=proposal.sender.email)
    message = build_message(config, proposal, b"pdf", to="client@example.com")
    assert message["Reply-To"] is None


def test_default_body_mentions_the_essentials(config, proposal):
    message = build_message(
        config, proposal, b"pdf", to="client@example.com", link="https://kp.example/p/tok"
    )
    text = message.get_body(("plain",)).get_content()

    assert "Игорь Лапин" in text, "обращение по имени контакта"
    assert "318 060,00" in text.replace(" ", " ")
    assert "https://kp.example/p/tok" in text
    assert "11 августа 2026" in text


def test_body_without_link_points_at_the_attachment(config, proposal):
    text = build_message(config, proposal, b"pdf", to="c@example.com").get_body(
        ("plain",)
    ).get_content()
    assert "приложен к письму" in text
    assert "http" not in text.split("--")[0]


def test_custom_subject_and_body_win(config, proposal):
    message = build_message(
        config, proposal, b"pdf", to="c@example.com", subject="Привет", body="Мой текст"
    )
    assert message["Subject"] == "Привет"
    assert message.get_body(("plain",)).get_content().strip() == "Мой текст"


def test_html_part_has_a_button_with_the_link(config, proposal):
    message = build_message(
        config, proposal, b"pdf", to="c@example.com", link="https://kp.example/p/tok"
    )
    html = message.get_body(("html",)).get_content()
    assert 'href="https://kp.example/p/tok"' in html
    assert "Посмотреть предложение" in html


def test_html_escapes_dangerous_text(config):
    """Тему и имена пишет пользователь, а HTML-часть письма собираем строками."""
    proposal = Proposal(
        subject="<script>alert(1)</script>",
        sender=Party(name="Студия"),
        client=Party(name="Клиент", person="Пётр & Ко"),
        items=[LineItem(title="Работа", price=100)],
    )
    html = build_message(config, proposal, b"pdf", to="c@example.com").get_body(
        ("html",)
    ).get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Пётр &amp; Ко" in html


@pytest.mark.parametrize("address", ["", "не почта", "ivan@mail", "a@b.c d"])
def test_bad_address_is_rejected(config, proposal, address):
    with pytest.raises(MailError, match="адрес"):
        build_message(config, proposal, b"pdf", to=address)


# --- deliver -------------------------------------------------------------------


def test_deliver_dry_run_changes_nothing(config, proposal):
    state = Delivery()
    message = deliver(
        config, proposal, state, b"pdf",
        to="c@example.com", base_url="https://kp.example", dry_run=True,
    )
    assert message["To"] == "c@example.com"
    assert state.status == "draft", "пробный прогон не должен отмечать отправку"
    assert state.token, "но токен уже выдан — ссылка в письме обязана быть настоящей"


def test_deliver_falls_back_to_the_client_email(config, proposal):
    message = deliver(
        config, proposal, Delivery(), b"pdf", to="", base_url="", dry_run=True
    )
    assert message["To"] == proposal.client.email


def test_deliver_without_any_address(config):
    proposal = Proposal(sender=Party(name="Я"), client=Party(name="Клиент"))
    with pytest.raises(MailError, match="не указан адрес"):
        deliver(config, proposal, Delivery(), b"pdf", to="", base_url="", dry_run=True)


def test_deliver_marks_sent(config, proposal, monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: sent.append(msg))

    state = Delivery()
    deliver(config, proposal, state, b"pdf", to="c@example.com", base_url="https://kp.example")

    assert len(sent) == 1
    assert state.status == "sent"
    assert state.recipient == "c@example.com"
    assert state.link("https://kp.example") in sent[0].get_body(("plain",)).get_content()


def test_send_without_config_explains_what_to_do(proposal):
    with pytest.raises(MailError, match="не настроена"):
        mailer.send(SmtpConfig(), build_message(
            SmtpConfig(host="h", sender="a@b.ru"), proposal, b"pdf", to="c@example.com"
        ))


# --- настройки -----------------------------------------------------------------


def test_config_defaults_port_by_encryption():
    assert SmtpConfig.from_dict({"ssl_mode": "starttls"}).port == 587
    assert SmtpConfig.from_dict({"ssl_mode": "ssl"}).port == 465
    assert SmtpConfig.from_dict({"port": "2525"}).port == 2525


def test_config_survives_garbage():
    config = SmtpConfig.from_dict({"port": "много", "ssl_mode": "магия"})
    assert config.port == 465
    assert config.ssl_mode == "ssl"


def test_safe_dict_never_leaks_the_password():
    config = SmtpConfig(host="h", user="u@e.ru", password="секрет")
    dumped = config.safe_dict()
    assert "секрет" not in json.dumps(dumped, ensure_ascii=False)
    assert dumped["has_password"] is True


def test_save_and_load_config(tmp_path):
    config = SmtpConfig(host="smtp.example.com", user="me@e.ru", password="пароль", port=587)
    path = mailer.save_config(config, tmp_path)

    assert path.name == "smtp.json"
    loaded = mailer.load_config(tmp_path)
    assert loaded.host == "smtp.example.com"
    assert loaded.password == "пароль"


def test_saved_config_is_not_world_readable(tmp_path):
    """В файле лежит пароль от почты — соседям по машине он ни к чему."""
    path = mailer.save_config(SmtpConfig(host="h", password="p"), tmp_path)
    assert path.stat().st_mode & 0o077 == 0


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    mailer.save_config(SmtpConfig(host="from-file", password="старый"), tmp_path)
    monkeypatch.setenv("PROPOSALS_SMTP_HOST", "from-env")
    monkeypatch.setenv("PROPOSALS_SMTP_PASSWORD", "новый")

    config = mailer.load_config(tmp_path)
    assert config.host == "from-env"
    assert config.password == "новый"


def test_load_config_of_missing_directory(tmp_path):
    config = mailer.load_config(tmp_path / "нет")
    assert not config.configured


def test_broken_config_file_is_reported(tmp_path):
    (tmp_path / "smtp.json").write_text("{не json", "utf-8")
    with pytest.raises(MailError, match="не читается"):
        mailer.load_config(tmp_path)


def test_configured_requires_host_and_sender():
    assert not SmtpConfig(host="h").configured
    assert not SmtpConfig(sender="a@b.ru").configured
    assert SmtpConfig(host="h", sender="a@b.ru").configured
