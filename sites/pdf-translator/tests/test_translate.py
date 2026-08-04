"""Перевод: разбор ответа, пачки, кэш, повторы. Без единого запроса в сеть."""

from __future__ import annotations

import json
import urllib.error

import pytest
from conftest import FakeTranslator

from pdftr.translate.cache import Cache, key_of
from pdftr.translate.google import (
    GoogleTranslator,
    TranslationError,
    split_batches,
    split_long,
)
from pdftr.translate.languages import LANGUAGES, TARGETS, name_of
from pdftr.translate.service import Service, worth_translating


def reply(text: str, detected: str = "en") -> bytes:
    return json.dumps([[[text, "", None, None, 0]], None, detected]).encode("utf-8")


class Recorder:
    """Заглушка сети: отдаёт заготовленные ответы и запоминает запросы."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)
        self.sent: list[bytes] = []

    def __call__(self, payload: bytes) -> bytes:
        self.sent.append(payload)
        answer = self.answers.pop(0) if self.answers else reply("")
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- разбор ответа ---------------------------------------------------------


def test_parses_a_normal_answer():
    engine = GoogleTranslator(rate=0, opener=Recorder([reply("Привет")]))
    assert engine.translate("Hello", "en", "ru") == ("Привет", "en")


def test_glues_answer_split_into_chunks():
    raw = json.dumps([[["Первая ", ""], ["вторая", ""]], None, "en"]).encode()
    engine = GoogleTranslator(rate=0, opener=Recorder([raw]))
    assert engine.translate("x", "en", "ru")[0] == "Первая вторая"


def test_empty_text_is_not_sent():
    recorder = Recorder([])
    engine = GoogleTranslator(rate=0, opener=recorder)
    assert engine.translate("   ", "en", "ru") == ("   ", "")
    assert recorder.sent == []


# --- пачки -----------------------------------------------------------------


def test_batch_goes_in_one_request():
    recorder = Recorder([reply("раз\nдва\nтри")])
    engine = GoogleTranslator(rate=0, opener=recorder)
    out, detected = engine.translate_batch(["one", "two", "three"], "en", "ru")
    assert out == ["раз", "два", "три"]
    assert detected == "en"
    assert len(recorder.sent) == 1


def test_batch_falls_back_to_one_by_one_when_lines_do_not_match():
    # Переводчик склеил строки — пачке нельзя верить, спрашиваем поштучно.
    recorder = Recorder([reply("всё одной строкой"), reply("раз"), reply("два")])
    engine = GoogleTranslator(rate=0, opener=recorder)
    assert engine.translate_batch(["one", "two"], "en", "ru")[0] == ["раз", "два"]
    assert len(recorder.sent) == 3


def test_split_batches_respects_the_limit():
    texts = ["a" * 30 for _ in range(10)]
    batches = split_batches(texts, limit=100)
    assert all(sum(len(texts[i]) + 1 for i in batch) <= 100 for batch in batches)
    assert sorted(index for batch in batches for index in batch) == list(range(10))


def test_overlong_text_travels_alone():
    batches = split_batches(["short", "x" * 500, "short"], limit=100)
    assert [1] in batches


def test_split_long_cuts_on_sentences():
    text = "Первое предложение. Второе предложение. Третье предложение. " * 4
    parts = split_long(text, limit=80)
    assert all(len(part) <= 80 for part in parts)
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


# --- повторы и ошибки ------------------------------------------------------


def test_retries_then_succeeds():
    error = urllib.error.HTTPError("url", 429, "too many", {}, None)
    recorder = Recorder([error, reply("готово")])
    engine = GoogleTranslator(rate=0, attempts=3, opener=recorder)
    # Пауза между попытками настоящая, поэтому попыток мало и они короткие.
    assert engine.translate("x", "en", "ru")[0] == "готово"


def test_gives_up_with_a_clear_error():
    recorder = Recorder([OSError("сеть недоступна")] * 3)
    engine = GoogleTranslator(rate=0, attempts=1, opener=recorder)
    with pytest.raises(TranslationError):
        engine.translate("x", "en", "ru")


def test_hopeless_http_code_is_not_retried():
    recorder = Recorder([urllib.error.HTTPError("url", 403, "no", {}, None)])
    engine = GoogleTranslator(rate=0, attempts=5, opener=recorder)
    with pytest.raises(TranslationError):
        engine.translate("x", "en", "ru")
    assert len(recorder.sent) == 1


# --- кэш -------------------------------------------------------------------


def test_cache_round_trip():
    cache = Cache(None)
    cache.put_many({"hello": "привет"}, "en", "ru")
    assert cache.get_many(["hello"], "en", "ru") == {"hello": "привет"}
    assert cache.get_many(["hello"], "en", "de") == {}  # другая пара языков
    cache.close()


def test_cache_survives_a_reopen(tmp_path):
    path = tmp_path / "cache.sqlite"
    first = Cache(path)
    first.put_many({"hello": "привет"}, "en", "ru")
    first.close()
    second = Cache(path)
    assert second.get_many(["hello"], "en", "ru") == {"hello": "привет"}
    second.close()


def test_cache_trims_old_records():
    cache = Cache(None)
    cache.put_many({f"phrase {index}": "перевод" for index in range(50)}, "en", "ru")
    assert cache.trim(keep=10) == 40
    assert cache.size() == 10
    cache.close()


def test_key_depends_on_everything():
    assert key_of("a", "en", "ru") != key_of("a", "en", "de")
    assert key_of("a", "en", "ru") != key_of("b", "en", "ru")


# --- слой документа --------------------------------------------------------


def test_what_is_worth_translating():
    assert worth_translating("Hello world")
    assert not worth_translating("12.5")
    assert not worth_translating("— 4 —")
    assert not worth_translating("")
    assert not worth_translating("§")


def test_service_keeps_order_and_skips_numbers():
    service = Service(Cache(None), FakeTranslator(), workers=1)
    out = service.translate_all(["Hello", "42", "World"], "en", "ru")
    assert out == ["»Hello", "42", "»World"]


def test_service_asks_once_for_repeated_text():
    engine = FakeTranslator()
    service = Service(Cache(None), engine, workers=1)
    service.translate_all(["Same text"] * 8, "en", "ru")
    assert engine.texts == ["Same text"]


def test_service_uses_the_cache_next_time():
    cache = Cache(None)
    engine = FakeTranslator()
    Service(cache, engine, workers=1).translate_all(["Hello there"], "en", "ru")
    second = FakeTranslator()
    out = Service(cache, second, workers=1).translate_all(["Hello there"], "en", "ru")
    assert out == ["»Hello there"]
    assert second.calls == 0
    cache.close()


def test_service_reports_progress():
    seen: list[tuple[int, int]] = []
    service = Service(Cache(None), FakeTranslator(), workers=1)
    service.translate_all(["one", "two"], "en", "ru", progress=lambda d, t: seen.append((d, t)))
    assert seen[0] == (0, 2)
    assert seen[-1][0] == seen[-1][1]


def test_service_passes_the_error_up():
    service = Service(Cache(None), FakeTranslator(fail_after=0), workers=1)
    with pytest.raises(TranslationError):
        service.translate_all(["anything at all"], "en", "ru")


def test_language_lists_are_consistent():
    assert "auto" in LANGUAGES and "auto" not in TARGETS
    assert name_of("ru") == "Русский"
    assert name_of("xx") == "xx"
