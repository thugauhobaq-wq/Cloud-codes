"""Отправка предложения письмом.

Обычный SMTP из стандартной библиотеки: у малого бизнеса это Яндекс, Mail.ru
или Gmail с паролем приложения, и никакой сервис рассылок для двух писем в
неделю не нужен.

Пароль в черновике не хранится никогда — он живёт в отдельном файле
`~/.proposals/smtp.json` с правами 600 или в переменной окружения. Черновики
пересылают коллегам и кладут в git, и пароль от почты уезжает вместе с ними.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

from .delivery import Delivery
from .models import Proposal
from .money import format_amount
from .template import format_date

CONFIG_NAME = "smtp.json"

# Достаточная проверка адреса: точную грамматику RFC 5322 всё равно проверит
# только сам почтовый сервер, а опечатку вида «ivan@mail» ловим сразу.
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class MailError(RuntimeError):
    """Письмо не отправилось или почта не настроена."""


def valid_email(address: str) -> bool:
    return bool(_EMAIL.match(str(address).strip()))


@dataclass(slots=True)
class SmtpConfig:
    """Куда и от чьего имени отправлять."""

    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    sender: str = ""
    """Адрес в поле «От кого». По умолчанию совпадает с логином."""
    sender_name: str = ""
    ssl_mode: str = "ssl"
    """`ssl` — шифрование сразу (порт 465), `starttls` — после приветствия (587)."""
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_address)

    @property
    def from_address(self) -> str:
        return self.sender or self.user

    def formatted_sender(self, fallback_name: str = "") -> str:
        name = self.sender_name or fallback_name
        return formataddr((name, self.from_address)) if name else self.from_address

    def safe_dict(self) -> dict:
        """Настройки для интерфейса — без пароля."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "sender": self.from_address,
            "sender_name": self.sender_name,
            "ssl_mode": self.ssl_mode,
            "configured": self.configured,
            "has_password": bool(self.password),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SmtpConfig:
        mode = str(raw.get("ssl_mode") or "ssl").strip().lower()
        try:
            port = int(raw.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        return cls(
            host=str(raw.get("host") or "").strip(),
            port=port or (587 if mode == "starttls" else 465),
            user=str(raw.get("user") or "").strip(),
            password=str(raw.get("password") or ""),
            sender=str(raw.get("sender") or "").strip(),
            sender_name=str(raw.get("sender_name") or "").strip(),
            ssl_mode=mode if mode in ("ssl", "starttls", "plain") else "ssl",
            timeout=float(raw.get("timeout") or 30.0),
        )


def config_path(directory: str | Path) -> Path:
    return Path(directory).expanduser() / CONFIG_NAME


def load_config(directory: str | Path) -> SmtpConfig:
    """Настройки из файла, поверх — переменные окружения.

    Окружение сильнее файла: так пароль можно не класть на диск вообще,
    например в systemd-юните или в CI.
    """
    path = config_path(directory)
    raw: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MailError(f"не читается {path}: {error}") from error
        if isinstance(loaded, dict):
            raw = loaded

    for key, variable in (
        ("host", "PROPOSALS_SMTP_HOST"),
        ("port", "PROPOSALS_SMTP_PORT"),
        ("user", "PROPOSALS_SMTP_USER"),
        ("password", "PROPOSALS_SMTP_PASSWORD"),
        ("sender", "PROPOSALS_SMTP_SENDER"),
        ("sender_name", "PROPOSALS_SMTP_SENDER_NAME"),
        ("ssl_mode", "PROPOSALS_SMTP_SSL"),
    ):
        value = os.environ.get(variable)
        if value:
            raw[key] = value
    return SmtpConfig.from_dict(raw)


def save_config(config: SmtpConfig, directory: str | Path) -> Path:
    """Записать настройки, спрятав файл от других пользователей машины."""
    path = config_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "host": config.host,
            "port": config.port,
            "user": config.user,
            "password": config.password,
            "sender": config.sender,
            "sender_name": config.sender_name,
            "ssl_mode": config.ssl_mode,
        },
        ensure_ascii=False,
        indent=2,
    )
    path.write_text(body + "\n", "utf-8")
    # На Windows и сетевых дисках прав может не быть — это не повод падать.
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


# --- текст письма --------------------------------------------------------------


def default_subject(proposal: Proposal) -> str:
    subject = proposal.subject or "сотрудничество"
    return f"Коммерческое предложение № {proposal.number} — {subject}"


def default_body(proposal: Proposal, link: str = "") -> str:
    """Короткое письмо, которое не стыдно отправить как есть."""
    who = proposal.client.person or proposal.client.name
    greeting = f"Здравствуйте, {who}!" if who else "Здравствуйте!"
    total = format_amount(proposal.totals().total, proposal.currency)

    lines = [greeting, ""]
    if proposal.subject:
        lines.append(f"Направляю коммерческое предложение: {proposal.subject}.")
    else:
        lines.append("Направляю коммерческое предложение.")
    lines.append(f"Итого к оплате: {total}.")
    if proposal.lead_time:
        lines.append(f"Срок выполнения: {proposal.lead_time}.")
    if proposal.valid_days > 0:
        lines.append(f"Предложение действительно до {format_date(proposal.valid_until)}.")
    lines.append("")
    if link:
        lines.append("Посмотреть и ответить одним нажатием:")
        lines.append(link)
        lines.append("")
        lines.append("Тот же документ приложен к письму в PDF.")
    else:
        lines.append("Документ приложен к письму в PDF.")
    lines.append("")
    lines.append("Готов ответить на вопросы.")
    signature = proposal.sender.person or proposal.sender.name
    if signature:
        lines += ["", "--", signature]
        if proposal.sender.person and proposal.sender.name:
            lines.append(proposal.sender.name)
        lines += proposal.sender.contacts
    return "\n".join(lines)


def default_reminder_subject(proposal: Proposal) -> str:
    return f"Напоминание: коммерческое предложение № {proposal.number}"


def default_reminder_body(proposal: Proposal, link: str = "", days: int = 0) -> str:
    """Вежливое напоминание.

    Тон намеренно мягкий и короткий: письмо-напоминание легко превращается в
    навязчивость, а задача — просто вернуть предложение в поле зрения.
    """
    who = proposal.client.person or proposal.client.name
    lines = [f"Здравствуйте, {who}!" if who else "Здравствуйте!", ""]

    subject = f" по теме «{proposal.subject}»" if proposal.subject else ""
    when = f" {days} дней назад" if days else ""
    lines.append(f"Напоминаю о коммерческом предложении{subject}, которое отправлял{when}.")
    lines.append("Если вопрос ещё в работе — просто скажите, сколько нужно времени.")

    if proposal.valid_days > 0:
        lines.append(f"Предложение действительно до {format_date(proposal.valid_until)}.")
    lines.append("")
    if link:
        lines.append("Посмотреть и ответить одним нажатием:")
        lines.append(link)
    else:
        lines.append("Документ приложен к письму в PDF.")
    lines.append("")
    lines.append("Если предложение больше не актуально, тоже дайте знать — я не буду напоминать.")

    signature = proposal.sender.person or proposal.sender.name
    if signature:
        lines += ["", "--", signature]
        if proposal.sender.person and proposal.sender.name:
            lines.append(proposal.sender.name)
        lines += proposal.sender.contacts
    return "\n".join(lines)


def _html_body(text: str, link: str) -> str:
    """HTML-версия того же письма: только абзацы и одна кнопка."""
    paragraphs = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or (link and block == link) or block.startswith("Посмотреть и ответить"):
            continue
        paragraphs.append("<p>" + "<br>".join(_escape(line) for line in block.split("\n")) + "</p>")

    button = ""
    if link:
        button = (
            f'<p style="margin:24px 0"><a href="{_escape(link)}" '
            'style="display:inline-block;padding:12px 22px;background:#1f5eff;color:#fff;'
            'border-radius:8px;text-decoration:none;font-weight:600">'
            "Посмотреть предложение</a></p>"
        )

    body = "\n".join(paragraphs[:1]) + button + "\n".join(paragraphs[1:])
    return (
        '<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif;'
        'color:#16181d;max-width:560px">' + body + "</div>"
    )


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_message(
    config: SmtpConfig,
    proposal: Proposal,
    pdf: bytes,
    *,
    to: str,
    subject: str = "",
    body: str = "",
    link: str = "",
) -> EmailMessage:
    """Собрать письмо: текст, HTML-версия и PDF во вложении."""
    if not valid_email(to):
        raise MailError(f"непохожий на почту адрес: {to!r}")

    text = body.strip() or default_body(proposal, link)
    message = EmailMessage()
    message["Subject"] = subject.strip() or default_subject(proposal)
    message["From"] = config.formatted_sender(proposal.sender.name)
    message["To"] = to.strip()
    reply_to = proposal.sender.email.strip()
    if reply_to and valid_email(reply_to) and reply_to != config.from_address:
        # Отправляем через свой ящик, а отвечать заказчик должен исполнителю.
        message["Reply-To"] = reply_to

    message.set_content(text)
    message.add_alternative(_html_body(text, link), subtype="html")
    message.add_attachment(
        pdf, maintype="application", subtype="pdf", filename=proposal.filename
    )
    return message


def send(config: SmtpConfig, message: EmailMessage) -> None:
    """Отдать письмо серверу. Все сбои SMTP приводим к одному исключению."""
    if not config.configured:
        raise MailError(
            "почта не настроена: заполните хост и адрес отправителя "
            "(команда «proposals smtp» или раздел «Отправка» в форме)"
        )
    context = ssl.create_default_context()
    try:
        if config.ssl_mode == "ssl":
            server = smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            )
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
        with server:
            if config.ssl_mode == "starttls":
                server.starttls(context=context)
            if config.user and config.password:
                server.login(config.user, config.password)
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise MailError(
            "почтовый сервер не принял логин или пароль. Для Яндекса, Mail.ru и "
            "Gmail нужен отдельный пароль приложения, а не пароль от аккаунта."
        ) from error
    except smtplib.SMTPRecipientsRefused as error:
        raise MailError(f"сервер отказался доставлять на этот адрес: {error}") from error
    except (smtplib.SMTPException, ssl.SSLError, OSError) as error:
        raise MailError(f"не отправилось: {error}") from error


def check(config: SmtpConfig) -> str:
    """Проверить связь и вход, ничего не отправляя."""
    if not config.configured:
        raise MailError("почта не настроена")
    context = ssl.create_default_context()
    try:
        if config.ssl_mode == "ssl":
            server = smtplib.SMTP_SSL(
                config.host, config.port, timeout=config.timeout, context=context
            )
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
        with server:
            if config.ssl_mode == "starttls":
                server.starttls(context=context)
            if config.user and config.password:
                server.login(config.user, config.password)
                return f"{config.host}: соединение и вход в порядке"
            return f"{config.host}: соединение в порядке, вход не проверялся (нет логина)"
    except smtplib.SMTPAuthenticationError as error:
        raise MailError("логин или пароль не подошли (нужен пароль приложения)") from error
    except (smtplib.SMTPException, ssl.SSLError, OSError) as error:
        raise MailError(f"нет связи с сервером: {error}") from error


def deliver(
    config: SmtpConfig,
    proposal: Proposal,
    delivery: Delivery,
    pdf: bytes,
    *,
    to: str,
    base_url: str,
    subject: str = "",
    body: str = "",
    dry_run: bool = False,
) -> EmailMessage:
    """Отправить предложение и отметить это в доставке.

    Токен выдаётся до отправки — ссылка обязана быть в том самом письме.
    При `dry_run` письмо собирается и возвращается, но никуда не уходит и
    статус не меняется: удобно посмотреть текст перед первой отправкой.
    """
    to = (to or "").strip() or proposal.client.email.strip()
    if not to:
        raise MailError("не указан адрес заказчика")

    delivery.ensure_token()
    message = build_message(
        config,
        proposal,
        pdf,
        to=to,
        subject=subject,
        body=body,
        link=delivery.link(base_url) if base_url else "",
    )
    if dry_run:
        return message
    send(config, message)
    delivery.mark_sent(parseaddr(to)[1] or to)
    return message


def remind(
    config: SmtpConfig,
    proposal: Proposal,
    delivery: Delivery,
    pdf: bytes,
    *,
    base_url: str,
    subject: str = "",
    body: str = "",
    dry_run: bool = False,
) -> EmailMessage:
    """Напомнить о предложении, которое ушло, но осталось без ответа.

    Отправляем на тот же адрес, что и в первый раз: напоминание неизвестно
    кому — это уже рассылка, а не напоминание.
    """
    if delivery.status not in ("sent", "viewed"):
        raise MailError("напоминать не о чем: предложение ещё не отправлено или уже решено")
    to = delivery.recipient
    if not to:
        raise MailError("не сохранился адрес, на который отправляли")

    delivery.ensure_token()
    message = build_message(
        config,
        proposal,
        pdf,
        to=to,
        subject=subject or default_reminder_subject(proposal),
        body=body or default_reminder_body(proposal, delivery.link(base_url) if base_url else "",
                                           delivery.silent_days() or 0),
        link=delivery.link(base_url) if base_url else "",
    )
    if dry_run:
        return message
    send(config, message)
    delivery.mark_reminded()
    return message
