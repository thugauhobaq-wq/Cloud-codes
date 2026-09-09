#!/usr/bin/env bash
# Поставить автообновление на сервер, подготовленный server-setup.sh.
#
#     ssh root@ваш-сервер
#     git clone https://github.com/thugauhobaq-wq/Cloud-codes.git /opt/cloud-codes
#     bash /opt/cloud-codes/deploy/install.sh
#
# Что делает: кладёт скрипт обновления в /usr/local/bin, заводит таймер
# systemd раз в минуту, открывает в фаерволе 80 и 443 для HTTPS, создаёт
# /etc/cloud-codes/env из примера. Повторный запуск безопасен — он же
# обновляет скрипт и юниты после правок в deploy/.
set -euo pipefail

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
skip() { printf '  · %s\n' "$*"; }
warn() { printf '  \033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Запускать от root: sudo bash $0"
command -v docker >/dev/null || die "Нет Docker — сначала server-setup.sh"
docker compose version >/dev/null 2>&1 || die "Нет docker compose — сначала server-setup.sh"
command -v git >/dev/null || die "Нет git"
command -v python3 >/dev/null || die "Нет python3 (нужен для разбора ответа GitHub)"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$HERE/.." && pwd)"
CONFIG_DIR=/etc/cloud-codes

step "Репозиторий"
git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$REPO_DIR — не git-клон"
ok "$REPO_DIR, ветка $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"

step "Настройки в $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
if [ -f "$CONFIG_DIR/env" ]; then
    skip "env уже есть, не трогаю"
else
    sed "s#^REPO_DIR=.*#REPO_DIR=$REPO_DIR#" "$HERE/env.example" > "$CONFIG_DIR/env"
    chmod 600 "$CONFIG_DIR/env"
    ok "создан из deploy/env.example"
fi

step "Скрипт и таймер"
install -m 755 "$HERE/autoupdate.sh" /usr/local/bin/cloud-codes-update
install -m 644 "$HERE/cloud-codes-autoupdate.service" /etc/systemd/system/
install -m 644 "$HERE/cloud-codes-autoupdate.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cloud-codes-autoupdate.timer >/dev/null 2>&1
ok "cloud-codes-update установлен, таймер включён"

step "Фаервол"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
    ok "открыты 80 и 443 (HTTPS через Caddy)"
else
    skip "ufw не активен — проверьте, что 80 и 443 открыты у хостера"
fi

step "Что дальше"
IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
cat <<TXT
  1. Ключи движка и домен:
       cp $REPO_DIR/handwriting-ocr/.env.example $REPO_DIR/handwriting-ocr/.env
       nano $REPO_DIR/handwriting-ocr/.env
     и дописать в него две строки (домен свой или бесплатный по IP):
       HWOCR_DOMAIN=${IP:-1.2.3.4}.sslip.io
       HWOCR_EMAIL=you@example.com
  2. Логин и пароль для входа с телефона:
       bash $HERE/hwocr-auth.sh имя пароль
  3. Отчёты в Telegram (по желанию): TG_BOT_TOKEN и TG_CHAT_ID в $CONFIG_DIR/env
  4. Первый запуск, не дожидаясь таймера:
       cloud-codes-update --force
     Журнал: journalctl -u cloud-codes-autoupdate -f
TXT
