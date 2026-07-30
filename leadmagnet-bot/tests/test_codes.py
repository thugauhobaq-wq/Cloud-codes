from __future__ import annotations

import pytest

from leadmagnet.codes import (
    MAX_CODE_LENGTH,
    looks_like_code,
    normalize,
    normalize_source,
    parse_start_payload,
)


@pytest.mark.parametrize(
    "raw",
    ["чеклист", "Чеклист", "ЧЕКЛИСТ", "чек-лист", "чеклист!", "/чеклист", "  чеклист  ", "чёклист"],
)
def test_all_the_ways_people_type_a_code_word_lead_to_one(raw: str):
    """Человеку из поста сказали «напишите ЧЕКЛИСТ» — он пришлёт что угодно похожее."""
    assert normalize(raw) == "чеклист"


def test_normalize_drops_spaces_inside():
    assert normalize("чек лист") == "чеклист"


def test_normalize_handles_empty():
    assert normalize("") == ""
    assert normalize("!!!") == ""


def test_normalize_truncates_absurd_input():
    assert len(normalize("а" * 200)) == MAX_CODE_LENGTH


def test_latin_codes_work_too():
    assert normalize("Checklist") == "checklist"


@pytest.mark.parametrize("raw", ["чеклист", "хочу чек лист", "дай гайд"])
def test_short_messages_are_treated_as_codes(raw: str):
    assert looks_like_code(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Здравствуйте, а расскажите подробнее про ваши услуги и цены",
        "а" * 100,
    ],
)
def test_long_messages_are_conversation_not_codes(raw: str):
    assert not looks_like_code(raw)


def test_parse_start_payload_without_source():
    assert parse_start_payload("чеклист") == ("чеклист", "")


def test_parse_start_payload_with_source():
    assert parse_start_payload("чеклист__instagram") == ("чеклист", "instagram")


def test_parse_start_payload_empty():
    assert parse_start_payload("") == ("", "")


def test_source_is_sanitised():
    assert normalize_source("Instagram Stories!") == "instagram-stories"
    assert normalize_source("  ") == ""
