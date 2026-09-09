#!/usr/bin/env bash
# Логин и пароль для входа в handwriting-ocr через Caddy.
#
#     bash deploy/hwocr-auth.sh имя пароль
#
# Пишет handwriting-ocr/caddy/auth.caddy с хэшем пароля (bcrypt): сам пароль
# на диске не хранится. После смены пароля Caddy нужно перезапустить:
#     docker compose -f docker-compose.yml -f docker-compose.https.yml restart caddy
set -euo pipefail

USER_NAME="${1:-}"
PASSWORD="${2:-}"
[ -n "$USER_NAME" ] && [ -n "$PASSWORD" ] || { echo "нужно: $0 имя пароль" >&2; exit 2; }
case "$USER_NAME" in *[!A-Za-z0-9._-]*) echo "в имени — только латиница, цифры, точка, дефис" >&2; exit 2;; esac
[ "${#PASSWORD}" -ge 8 ] || { echo "пароль короче 8 знаков — подберут" >&2; exit 2; }

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$HERE/../handwriting-ocr/caddy/auth.caddy"

# Хэш считает сам Caddy — тот же образ, что и в docker-compose.https.yml,
# так что формат точно совпадёт.
HASH="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$PASSWORD")"
umask 077
printf 'basic_auth {\n\t%s %s\n}\n' "$USER_NAME" "$HASH" > "$TARGET"
echo "записано: $TARGET (пользователь $USER_NAME)"
