"""Движки без сети: фабрика, определение формата, оба адаптера."""

from __future__ import annotations

import base64
import json
import sys

import pytest
from conftest import (
    HEIC,
    JPEG,
    PNG,
    WEBP,
    APIConnectionError,
    ApiError,
    Block,
    FakeClaudeClient,
    Recorder,
    Response,
    StopDetails,
    http_error,
    text_response,
)

from hwocr.recognize import ConfigError, RecognitionError, build_recognizer, sniff_image
from hwocr.recognize.claude import (
    DEFAULT_MODEL,
    FALLBACK_BETA,
    MAX_IMAGE_BYTES,
    TRUNCATED_MARK,
    ClaudeRecognizer,
)
from hwocr.recognize.yandex import ENDPOINT, YandexRecognizer

# --- фабрика ---------------------------------------------------------------


@pytest.fixture()
def fake_sdk(monkeypatch):
    """Модуль anthropic без сети и без установки: фабрика лишь создаёт клиент."""
    import types

    created = []
    module = types.SimpleNamespace(Anthropic=lambda **kwargs: created.append(kwargs) or object())
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return created


def test_default_engine_is_yandex():
    engine = build_recognizer({"YC_API_KEY": "k", "YC_FOLDER_ID": "f"})
    assert engine.name == "yandex" and engine.languages == ["ru"]


def test_yandex_languages_come_from_env():
    engine = build_recognizer(
        {"OCR_ENGINE": "yandex", "YC_API_KEY": "k", "YC_FOLDER_ID": "f", "YC_LANGUAGES": "ru, EN"}
    )
    assert engine.languages == ["ru", "en"]


def test_claude_engine_is_chosen_explicitly(fake_sdk):
    engine = build_recognizer({"OCR_ENGINE": "claude", "ANTHROPIC_API_KEY": "sk-test"})
    assert fake_sdk[0]["api_key"] == "sk-test"
    assert engine.name == "claude" and engine.model == DEFAULT_MODEL


def test_claude_model_and_fallbacks_come_from_env(fake_sdk):
    engine = build_recognizer(
        {
            "OCR_ENGINE": "claude",
            "ANTHROPIC_API_KEY": "sk",
            "CLAUDE_MODEL": "claude-sonnet-5",
            "CLAUDE_FALLBACKS": "0",
        }
    )
    assert engine.model == "claude-sonnet-5" and engine.fallbacks is False


def test_yandex_engine_needs_both_settings():
    engine = build_recognizer({"OCR_ENGINE": "yandex", "YC_API_KEY": "k", "YC_FOLDER_ID": "f"})
    assert engine.name == "yandex"
    with pytest.raises(ConfigError, match="YC_FOLDER_ID"):
        build_recognizer({"OCR_ENGINE": "yandex", "YC_API_KEY": "k"})
    with pytest.raises(ConfigError, match="YC_API_KEY и YC_FOLDER_ID"):
        build_recognizer({"OCR_ENGINE": "yandex"})


def test_missing_claude_key_names_the_variable():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_recognizer({"OCR_ENGINE": "Claude", "ANTHROPIC_API_KEY": "  "})


def test_unknown_engine_lists_the_options():
    with pytest.raises(ConfigError, match="claude, yandex"):
        build_recognizer({"OCR_ENGINE": "tesseract"})


# --- формат по байтам ------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (WEBP, "image/webp"),
        (HEIC, "image/heic"),
        (b"%PDF-1.4", None),
        (b"", None),
        (b"\xff", None),
    ],
)
def test_sniff_image(data, expected):
    assert sniff_image(data[:16]) == expected


# --- Claude ----------------------------------------------------------------


def claude(outcome, **kwargs) -> tuple[ClaudeRecognizer, FakeClaudeClient]:
    client = FakeClaudeClient(outcome)
    return ClaudeRecognizer(client=client, **kwargs), client


def test_claude_request_carries_the_photo_and_the_rules():
    engine, client = claude(text_response("строка"), model="claude-opus-5")
    assert engine.recognize(JPEG, "image/jpeg") == "строка"
    (call,) = client.calls
    assert call["model"] == "claude-opus-5" and call["max_tokens"] >= 4096
    assert "[?]" in call["system"] and "ПУСТО" in call["system"]
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    image, text = call["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/jpeg"
    assert base64.standard_b64decode(image["source"]["data"]) == JPEG
    assert text["type"] == "text"


def test_claude_without_fallbacks_uses_plain_messages():
    engine, client = claude(text_response("ok"), fallbacks=False)
    engine.recognize(PNG, "image/png")
    assert "betas" not in client.calls[0] and "fallbacks" not in client.calls[0]


def test_claude_takes_only_text_blocks():
    response = Response([Block("thinking", "думаю…"), Block("text", "раз, "), Block("text", "два")])
    engine, _ = claude(response)
    assert engine.recognize(JPEG, "image/jpeg") == "раз, два"


def test_claude_refusal_is_explained():
    engine, _ = claude(Response([], "refusal", StopDetails("не могу")))
    with pytest.raises(RecognitionError, match=r"отказался.*не могу"):
        engine.recognize(JPEG, "image/jpeg")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ApiError(401), "ANTHROPIC_API_KEY"),
        (ApiError(403), "ANTHROPIC_API_KEY"),
        (ApiError(429), "слишком часто"),
        (ApiError(400, "image too large"), "image too large"),
        (ApiError(529), "неполадки"),
        (APIConnectionError("boom"), "нет связи"),
        (RuntimeError("странное"), "странное"),
    ],
)
def test_claude_errors_become_readable(error, expected):
    engine, _ = claude(error)
    with pytest.raises(RecognitionError, match=expected):
        engine.recognize(JPEG, "image/jpeg")


@pytest.mark.parametrize("answer", ["", "   ", "ПУСТО", "пусто\n"])
def test_claude_empty_answer_is_an_error(answer):
    engine, _ = claude(text_response(answer))
    with pytest.raises(RecognitionError, match="не нашлось"):
        engine.recognize(JPEG, "image/jpeg")


def test_claude_strips_code_fences():
    engine, _ = claude(text_response("```text\nпервая\nвторая\n```"))
    assert engine.recognize(JPEG, "image/jpeg") == "первая\nвторая"
    engine, _ = claude(text_response("```\nодна\n```"))
    assert engine.recognize(JPEG, "image/jpeg") == "одна"


def test_claude_marks_truncated_answer():
    engine, _ = claude(text_response("начало", stop_reason="max_tokens"))
    result = engine.recognize(JPEG, "image/jpeg")
    assert result.startswith("начало") and result.endswith(TRUNCATED_MARK)


def test_claude_rejects_huge_photo_before_sending():
    engine, client = claude(text_response("x"))
    with pytest.raises(RecognitionError, match="МБ"):
        engine.recognize(b"\xff" * (MAX_IMAGE_BYTES + 1), "image/jpeg")
    assert client.calls == []


def test_claude_without_sdk_says_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(RecognitionError, match="pip install"):
        ClaudeRecognizer(api_key="sk")


# --- Yandex ----------------------------------------------------------------


def yandex_ok(text: str) -> bytes:
    return json.dumps({"result": {"textAnnotation": {"fullText": text}}}).encode()


def yandex(*outcomes, **kwargs) -> tuple[YandexRecognizer, Recorder]:
    recorder = Recorder(*outcomes)
    engine = YandexRecognizer("key-1", "folder-1", opener=recorder, **kwargs)
    return engine, recorder


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("hwocr.recognize.yandex.time.sleep", lambda _s: None)


def test_yandex_request_shape():
    engine, recorder = yandex(yandex_ok("текст\n"), languages=("ru", "en"))
    assert engine.recognize(PNG, "image/png") == "текст"
    (request,) = recorder.requests
    assert request.full_url == ENDPOINT and request.get_method() == "POST"
    assert request.get_header("Authorization") == "Api-Key key-1"
    assert request.get_header("X-folder-id") == "folder-1"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["model"] == "handwritten" and body["languageCodes"] == ["ru", "en"]
    assert body["mimeType"] == "PNG", "сервис ждёт короткое имя формата, а не MIME-тип"
    assert base64.standard_b64decode(body["content"]) == PNG


def test_yandex_sends_jpeg_as_JPEG():
    engine, recorder = yandex(yandex_ok("ок"))
    engine.recognize(JPEG, "image/jpeg")
    assert json.loads(recorder.requests[0].data)["mimeType"] == "JPEG"


def test_yandex_refuses_webp_before_sending():
    engine, recorder = yandex(yandex_ok("ок"))
    with pytest.raises(RecognitionError, match="JPEG и PNG"):
        engine.recognize(WEBP, "image/webp")
    assert recorder.requests == []


def test_yandex_bad_key_fails_at_once():
    engine, recorder = yandex(http_error(401), attempts=3)
    with pytest.raises(RecognitionError, match="YC_API_KEY"):
        engine.recognize(JPEG, "image/jpeg")
    assert len(recorder.requests) == 1


def test_yandex_bad_request_shows_the_reason():
    engine, _ = yandex(http_error(400, b'{"message": "unsupported mime"}'))
    with pytest.raises(RecognitionError, match="unsupported mime"):
        engine.recognize(JPEG, "image/jpeg")


def test_yandex_retries_after_429():
    engine, recorder = yandex(http_error(429), yandex_ok("готово"), attempts=2)
    assert engine.recognize(JPEG, "image/jpeg") == "готово"
    assert len(recorder.requests) == 2


def test_yandex_gives_up_after_repeated_5xx():
    engine, recorder = yandex(http_error(503), attempts=3)
    with pytest.raises(RecognitionError, match="503"):
        engine.recognize(JPEG, "image/jpeg")
    assert len(recorder.requests) == 3


@pytest.mark.parametrize(
    "raw",
    [b"<html>", b'{"result": {}}', b'{"result": {"textAnnotation": {"fullText": 5}}}'],
)
def test_yandex_strange_answer_is_an_error(raw):
    engine, _ = yandex(raw)
    with pytest.raises(RecognitionError, match="непонятный ответ"):
        engine.recognize(JPEG, "image/jpeg")


def test_yandex_empty_text_is_an_error():
    engine, _ = yandex(yandex_ok("  \n"))
    with pytest.raises(RecognitionError, match="не нашлось"):
        engine.recognize(JPEG, "image/jpeg")


def test_yandex_rejects_huge_photo_before_sending():
    engine, recorder = yandex(yandex_ok("x"))
    with pytest.raises(RecognitionError, match="МБ"):
        engine.recognize(b"\xff" * (10 * 1024 * 1024 + 1), "image/jpeg")
    assert recorder.requests == []
