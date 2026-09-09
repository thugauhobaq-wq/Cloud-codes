"""Распознавание через Claude (Anthropic API).

Языковая модель с картинкой читает пропись лучше классических OCR: она
опирается на смысл фразы, а не только на форму букв, и потому вытягивает
неразборчивые места. Плата за это — фото уходит на серверы Anthropic за
рубеж; для личного пользования это приемлемо, для продажи услуги — см. README.

Клиент `anthropic` импортируется лениво и только здесь: движок Яндекса и все
тесты должны работать без него. В тестах вместо клиента подставляется
заглушка с тем же методом `messages.create`.
"""

from __future__ import annotations

import base64

from .base import RecognitionError

DEFAULT_MODEL = "claude-opus-5"
# Предел API на одну картинку. Телефон и так сжимает фото до ~0,5 МБ, но
# «Поделиться» может прислать оригинал — лучше отказать до отправки.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
EMPTY_MARK = "ПУСТО"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
Ты — аккуратный переписчик рукописных текстов на русском языке. На фотографии —
рукопись (скоропись). Перепиши её печатными буквами ровно так, как написано.

Правила:
1. Сохраняй разбивку: одна строка рукописи — одна строка ответа, между абзацами —
   пустая строка.
2. Ничего не исправляй и не дописывай: ошибки, сокращения, устаревшие написания
   и знаки препинания оставляй как в оригинале.
3. Если фрагмент не читается, поставь на его месте [?]. Если слово читается
   с сомнением, напиши его и поставь [?] сразу после него.
4. Не добавляй заголовков, пояснений, комментариев, кавычек вокруг текста и
   разметки markdown. В ответе — только переписанный текст.
5. Если рукописного текста на фотографии нет, ответь одним словом: ПУСТО.
"""
USER_PROMPT = "Перепиши текст с этой фотографии."
TRUNCATED_MARK = "[?] текст не поместился целиком"


class ClaudeRecognizer:
    """Одна фотография — один запрос к модели."""

    name = "claude"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8192,
        timeout: float = 120.0,
        fallbacks: bool = True,
        client: object | None = None,
    ) -> None:
        self.model = model
        # Кириллица токенизируется дороже латиницы, а на фото может оказаться
        # два листа сразу — 4096 бывает мало, обрезанный текст хуже долгого ответа.
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self._client = client or self._make_client(api_key, timeout)

    @staticmethod
    def _make_client(api_key: str, timeout: float) -> object:
        try:
            import anthropic
        except ImportError as error:
            raise RecognitionError(
                "библиотека anthropic не установлена: pip install 'handwriting-ocr[claude]'"
            ) from error
        # base_url не задаём: SDK сам читает ANTHROPIC_BASE_URL, если он нужен.
        return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    # --- запрос ------------------------------------------------------------

    def _build_request(self, image: bytes, media_type: str) -> dict:
        """Тело запроса отдельно от отправки: его проверяют тесты."""
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": USER_PROMPT},
                    ],
                }
            ],
        }

    def _create(self, request: dict) -> object:
        if self.fallbacks:
            # Серверный запасной маршрут: если модель откажется отвечать по
            # соображениям безопасности, API сам повторит запрос на другой.
            # На фото рукописи это редкость, но обходится бесплатно.
            return self._client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **request
            )
        return self._client.messages.create(**request)

    def recognize(self, image: bytes, media_type: str) -> str:
        if len(image) > MAX_IMAGE_BYTES:
            limit = MAX_IMAGE_BYTES // (1024 * 1024)
            raise RecognitionError(
                f"фото больше {limit} МБ — Claude такое не принимает, "
                "сожмите его или снимите заново из приложения"
            )
        request = self._build_request(image, media_type)
        try:
            response = self._create(request)
        except RecognitionError:
            raise
        except Exception as error:
            raise RecognitionError(_explain(error)) from error

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            why = getattr(details, "explanation", "") or ""
            raise RecognitionError(
                "Claude отказался распознавать это фото" + (f": {why}" if why else "")
            )

        text = _strip_fences(_text_of(response)).strip()
        if not text or text.upper() == EMPTY_MARK:
            raise RecognitionError("на фото не нашлось рукописного текста")
        if stop == "max_tokens":
            text += "\n\n" + TRUNCATED_MARK
        return text


def _text_of(response: object) -> str:
    """Склеить текстовые блоки ответа; блоки размышлений и прочие — мимо."""
    parts = []
    for block in getattr(response, "content", None) or ():
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _strip_fences(text: str) -> str:
    """Снять ```-обрамление, если модель всё же его поставила."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) > 6:
        inner = stripped[3:-3]
        first, _, rest = inner.partition("\n")
        # Первая строка после ``` — подсказка языка, если в ней нет пробелов.
        if first and " " not in first.strip() and rest:
            inner = rest
        return inner
    return text


def _explain(error: Exception) -> str:
    """Человеческое объяснение ошибки SDK.

    Классы `anthropic.*` здесь не импортируются: у ошибок со статусом есть
    поле `status_code`, сетевые узнаются по имени класса. Так модуль и тесты
    работают без установленной библиотеки.
    """
    code = getattr(error, "status_code", None)
    kind = type(error).__name__
    if code in (401, 403):
        return "ключ Anthropic не подошёл — проверьте ANTHROPIC_API_KEY в .env"
    if code == 429:
        return "Claude отвечает «слишком часто» — подождите минуту и отправьте снова"
    if code == 400:
        return f"Claude не принял запрос: {_message(error)}"
    if isinstance(code, int) and code >= 500:
        return "у Claude временные неполадки, попробуйте позже"
    if kind in ("APIConnectionError", "APITimeoutError"):
        return "нет связи с api.anthropic.com — проверьте интернет на сервере"
    return f"ошибка Claude: {_message(error)}"


def _message(error: Exception) -> str:
    return str(getattr(error, "message", "") or error)


__all__ = ["DEFAULT_MODEL", "SYSTEM_PROMPT", "ClaudeRecognizer"]
