"""Статусы доставки, журнал и воронка."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from proposals.delivery import (
    ACCEPTED,
    DECLINED,
    DRAFT,
    SENT,
    VIEWED,
    Delivery,
    Event,
    parse_time,
    summarize,
)


def test_new_delivery_is_a_draft():
    state = Delivery()
    assert state.status == DRAFT
    assert state.label == "черновик"
    assert not state.is_decided
    assert state.link("http://example.com") == "", "без токена ссылки быть не может"


def test_mark_sent_issues_a_token_and_logs():
    state = Delivery()
    state.mark_sent("client@example.com")

    assert state.status == SENT
    assert len(state.token) >= 32
    assert state.recipient == "client@example.com"
    assert state.sent_at is not None
    assert [event.kind for event in state.events] == ["sent"]


def test_second_send_is_logged_as_resend():
    state = Delivery()
    state.mark_sent("a@example.com")
    first_sent = state.sent_at
    state.mark_sent("b@example.com")

    assert [event.kind for event in state.events] == ["sent", "resent"]
    assert state.sent_at == first_sent, "дата первой отправки не должна съезжать"
    assert state.recipient == "b@example.com"


def test_token_survives_resend():
    """Иначе ссылка из первого письма перестала бы работать."""
    state = Delivery()
    state.mark_sent("a@example.com")
    token = state.token
    state.mark_sent("a@example.com")
    assert state.token == token


def test_viewed_is_logged_once():
    state = Delivery()
    state.mark_sent("a@example.com")

    assert state.mark_viewed() is True
    assert state.mark_viewed() is False
    assert [event.kind for event in state.events].count("viewed") == 1
    assert state.status == VIEWED


def test_view_after_decision_does_not_downgrade_status():
    """Заказчик легко откроет ссылку ещё раз уже после того, как согласился."""
    state = Delivery()
    state.mark_sent("a@example.com")
    state.decide(True)
    state.mark_viewed()
    assert state.status == ACCEPTED


def test_decide_can_be_changed():
    state = Delivery()
    state.mark_sent("a@example.com")
    state.decide(False, "дорого")
    assert state.status == DECLINED
    assert state.comment == "дорого"

    state.decide(True, "договорились")
    assert state.status == ACCEPTED
    assert state.comment == "договорились"
    assert [event.kind for event in state.events][-2:] == [DECLINED, ACCEPTED]


def test_reset_invalidates_the_link():
    state = Delivery()
    state.mark_sent("a@example.com")
    old_token = state.token
    state.reset()

    assert state.status == DRAFT
    assert state.token == ""
    assert not state.matches(old_token)
    assert state.events[-1].kind == "reset"


def test_matches_rejects_wrong_and_empty_tokens():
    state = Delivery()
    state.ensure_token()
    assert state.matches(state.token)
    assert not state.matches("")
    assert not state.matches(state.token + "x")
    assert not Delivery().matches("что угодно")


def test_link():
    state = Delivery()
    token = state.ensure_token()
    assert state.link("https://kp.example.com/") == f"https://kp.example.com/p/{token}"


def test_public_dict_hides_the_token_and_recipient():
    """Страница заказчика не должна выдавать ни секрет ссылки, ни чужой адрес."""
    state = Delivery()
    state.mark_sent("client@example.com")
    public = state.public_dict()

    assert "token" not in public
    assert "recipient" not in public
    assert public["status"] == SENT


def test_roundtrip_through_dict():
    state = Delivery()
    state.mark_sent("a@example.com")
    state.mark_viewed()
    state.decide(True, "берём")

    restored = Delivery.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()
    assert restored.status == ACCEPTED
    assert restored.sent_at == state.sent_at


def test_from_dict_survives_garbage():
    state = Delivery.from_dict(
        {"status": "магия", "sent_at": "вчера", "events": ["не словарь", {"kind": "sent"}]}
    )
    assert state.status == DRAFT
    assert state.sent_at is None
    assert [event.kind for event in state.events] == ["sent"]


def test_from_dict_of_nothing():
    assert Delivery.from_dict(None).status == DRAFT


def test_times_are_timezone_aware():
    """Черновик переезжает между машинами — «18:00» без пояса потом не понять."""
    state = Delivery()
    state.mark_sent("a@example.com")
    assert state.sent_at.tzinfo is not None
    assert "+00:00" in state.to_dict()["sent_at"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-28T10:00:00+00:00", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        ("2026-07-28T10:00:00Z", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        # Без пояса считаем UTC: так писали ранние версии файла.
        ("2026-07-28T10:00:00", datetime(2026, 7, 28, 10, tzinfo=UTC)),
        ("", None),
        ("вчера", None),
    ],
)
def test_parse_time(raw, expected):
    assert parse_time(raw) == expected


def test_event_label_falls_back_to_the_kind():
    assert Event("sent").label == "отправлено"
    assert Event("нечто").label == "нечто"


# --- воронка -------------------------------------------------------------------


def test_summarize_counts_and_conversion():
    summary = summarize(
        [
            (DRAFT, Decimal("100"), "RUB"),
            (SENT, Decimal("200"), "RUB"),
            (VIEWED, Decimal("300"), "RUB"),
            (ACCEPTED, Decimal("400"), "RUB"),
            (DECLINED, Decimal("500"), "RUB"),
        ]
    )
    assert summary["total"] == 5
    assert summary["delivered"] == 4, "черновик не считается отправленным"
    assert summary["conversion"] == 25.0
    assert summary["counts"][ACCEPTED] == 1

    money = summary["money"]["RUB"]
    assert money["accepted"] == "400"
    assert money["delivered"] == "1400"
    assert money["open"] == "500", "ждут ответа только отправленные и просмотренные"


def test_summarize_keeps_currencies_apart():
    """Складывать рубли с евро нельзя, а курс — не забота генератора КП."""
    summary = summarize([(ACCEPTED, Decimal("100"), "RUB"), (ACCEPTED, Decimal("50"), "EUR")])
    assert summary["money"]["RUB"]["accepted"] == "100"
    assert summary["money"]["EUR"]["accepted"] == "50"


def test_summarize_of_nothing():
    summary = summarize([])
    assert summary["total"] == 0
    assert summary["conversion"] == 0.0
    assert summary["money"] == {}


def test_summarize_treats_unknown_status_as_draft():
    summary = summarize([("магия", Decimal("10"), "RUB")])
    assert summary["counts"][DRAFT] == 1
    assert summary["delivered"] == 0
