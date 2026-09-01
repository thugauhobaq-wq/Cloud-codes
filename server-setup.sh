#!/usr/bin/env bash
# Подготовка чистого сервера под ботов из этого репозитория.
#
#     ssh root@ваш-сервер
#     curl -fsSL https://raw.githubusercontent.com/thugauhobaq-wq/Cloud-codes/main/server-setup.sh -o setup.sh
#     less setup.sh          # прочитайте, прежде чем запускать чужой скрипт от root
#     bash setup.sh
#
# Что делает: заводит рабочего пользователя, ставит Docker, закрывает вход по
# паролю, включает фаервол и автообновления безопасности, добавляет подкачку.
# Повторный запуск безопасен: каждый шаг проверяет, не сделан ли он уже.
set -euo pipefail

APP_USER="${APP_USER:-bots}"
SSH_PORT="${SSH_PORT:-22}"
LOG="${LOG:-/var/log/server-setup.log}"

# ── вывод ─────────────────────────────────────────────────────────────────

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
skip() { printf '  · %s\n' "$*"; }
warn() { printf '  \033[33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── проверки до того, как что-то менять ───────────────────────────────────

[ "$(id -u)" = "0" ] || die "Запускать от root: sudo bash $0"

command -v apt-get >/dev/null || die "Нужна Ubuntu или Debian: apt-get не найден"

# systemd есть не везде: часть дешёвых VPS — это контейнеры OpenVZ или LXC.
HAS_SYSTEMD=no
[ -d /run/systemd/system ] && HAS_SYSTEMD=yes

VIRT="$(systemd-detect-virt 2>/dev/null || echo unknown)"
case "$VIRT" in
    openvz|lxc|lxc-libvirt)
        warn "Виртуализация $VIRT — Docker на таких серверах часто не работает."
        warn "Если установка Docker упадёт, берите тариф с KVM: он есть у всех хостеров."
        ;;
esac

step "Сервер"
ok "$(. /etc/os-release && echo "$PRETTY_NAME"), $(uname -m), виртуализация: $VIRT"
ok "память: $(free -m | awk '/Mem:/ {print $2}') МБ, свободно на диске: $(df -h / | awk 'NR==2 {print $4}')"

# ── пользователь ──────────────────────────────────────────────────────────

step "Рабочий пользователь «$APP_USER»"
if id "$APP_USER" >/dev/null 2>&1; then
    skip "уже есть"
else
    # useradd, а не adduser: последнего нет в урезанных образах системы.
    useradd --create-home --shell /bin/bash "$APP_USER"
    passwd --lock "$APP_USER" >/dev/null
    ok "создан, вход по паролю ему запрещён"
fi
usermod -aG sudo "$APP_USER"
ok "может выполнять sudo"

# Ключ входа копируется от root: вы уже вошли по нему, значит он рабочий.
KEYS_SRC="/root/.ssh/authorized_keys"
KEYS_DST="/home/$APP_USER/.ssh/authorized_keys"
HAS_KEY=no
if [ -s "$KEYS_SRC" ]; then
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$KEYS_SRC" "$KEYS_DST"
    HAS_KEY=yes
    ok "ssh-ключ скопирован от root"
elif [ -s "$KEYS_DST" ]; then
    HAS_KEY=yes
    skip "ssh-ключ уже на месте"
else
    warn "ssh-ключей нет — вы вошли по паролю"
fi

# ── доступ по ssh ─────────────────────────────────────────────────────────

step "Доступ по SSH"
CONF=/etc/ssh/sshd_config.d/99-server-setup.conf
if [ "$HAS_KEY" = "yes" ]; then
    mkdir -p /etc/ssh/sshd_config.d
    cat > "$CONF" <<EOF
# Вход только по ключу: пароль перебирают роботы круглосуточно.
PasswordAuthentication no
PermitRootLogin prohibit-password
EOF
    SSHD_BIN="$(command -v sshd || echo /usr/sbin/sshd)"
    # sshd -t отказывается работать без своего служебного каталога. Его
    # создаёт служба при старте, но на остановленном ssh его может не быть —
    # и тогда проверка провалилась бы на пустом месте.
    mkdir -p /run/sshd
    if [ ! -x "$SSHD_BIN" ]; then
        rm -f "$CONF"
        warn "sshd на сервере не найден — настраивать нечего"
    elif "$SSHD_BIN" -t 2>/dev/null; then
        if [ "$HAS_SYSTEMD" = "yes" ]; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
        fi
        ok "вход по паролю закрыт, работает только ключ"
    else
        # Лучше остаться с паролем, чем с сервером, который не пускает никого.
        rm -f "$CONF"
        warn "sshd не принял настройку — оставил как было"
    fi
else
    # Закрыть пароль, когда ключа нет, — значит запереть себя снаружи.
    warn "вход по паролю оставлен: иначе вы потеряете доступ к серверу"
    warn "поставьте ключ (ssh-copy-id $APP_USER@адрес) и запустите скрипт снова"
fi

# ── пакеты ────────────────────────────────────────────────────────────────

step "Обновление системы"
export DEBIAN_FRONTEND=noninteractive
# Подробности — в лог: на экране должны быть видны шаги, а не вывод dpkg.
: > "$LOG"
apt-get update -qq >>"$LOG" 2>&1
apt-get upgrade -y -qq >>"$LOG" 2>&1
ok "пакеты обновлены (подробности: $LOG)"

apt-get install -y -qq ca-certificates curl git ufw unattended-upgrades >>"$LOG" 2>&1
ok "поставлены git, ufw, автообновления"

# ── docker ────────────────────────────────────────────────────────────────

step "Docker"
if command -v docker >/dev/null 2>&1; then
    skip "уже стоит: $(docker --version)"
else
    install -m 0755 -d /etc/apt/keyrings
    . /etc/os-release
    if ! curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc; then
        die "не скачался ключ Docker — проверьте, что у сервера есть интернет"
    fi
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq >>"$LOG" 2>&1
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >>"$LOG" 2>&1 \
        || die "не установился Docker — смотрите $LOG"
    ok "установлен: $(docker --version)"
fi

usermod -aG docker "$APP_USER"
ok "$APP_USER может запускать docker без sudo"

if [ "$HAS_SYSTEMD" = "yes" ]; then
    systemctl enable --now docker >/dev/null 2>&1 || warn "не удалось запустить docker"
    docker info >/dev/null 2>&1 && ok "демон работает" || warn "демон не отвечает — проверьте KVM"
else
    skip "без systemd демон не запустить отсюда"
fi

# Логи контейнеров без ограничения съедают диск за недели работы.
if [ ! -f /etc/docker/daemon.json ]; then
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
    [ "$HAS_SYSTEMD" = "yes" ] && systemctl restart docker >/dev/null 2>&1 || true
    ok "логи контейнеров ограничены 30 МБ"
else
    skip "daemon.json уже настроен — не трогаю"
fi

# ── фаервол ───────────────────────────────────────────────────────────────

step "Фаервол"
# Боты работают на long polling: входящих соединений им не нужно вовсе,
# наружу открыт только ssh.
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow "$SSH_PORT/tcp" >/dev/null
if ufw --force enable >/dev/null 2>&1; then
    ok "закрыто всё, кроме ssh на порту $SSH_PORT"
else
    warn "ufw не включился (бывает в контейнерных VPS) — проверьте фаервол хостера"
fi

# ── автообновления ────────────────────────────────────────────────────────

step "Автообновления безопасности"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
ok "заплатки ставятся сами"

# ── подкачка ──────────────────────────────────────────────────────────────

step "Подкачка"
RAM_MB=$(free -m | awk '/Mem:/ {print $2}')
if swapon --show 2>/dev/null | grep -q .; then
    skip "уже есть"
elif [ "$RAM_MB" -ge 2048 ]; then
    skip "памяти ${RAM_MB} МБ — хватает без подкачки"
elif fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none 2>/dev/null; then
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    if swapon /swapfile 2>/dev/null; then
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
        ok "2 ГБ подкачки: сборка образа на сервере с ${RAM_MB} МБ иначе падает"
    else
        rm -f /swapfile
        warn "подкачку включить не удалось (обычно так в контейнерных VPS)"
    fi
else
    warn "не хватило места на диске под файл подкачки"
fi

# ── что дальше ────────────────────────────────────────────────────────────

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────
Сервер готов.

Заходите теперь так (не под root):

    ssh $APP_USER@${IP:-адрес-сервера}

И ставьте бота:

    git clone https://github.com/thugauhobaq-wq/Cloud-codes.git
    cd Cloud-codes/booking-bot      # или shop-bot, leadmagnet-bot, giveaway-bot
    cp .env.example .env
    nano .env                       # BOT_TOKEN и OWNER_ID обязательны
    docker compose up -d --build
    docker compose logs -f

Проверьте, что вход по ключу работает, НЕ закрывая это окно: откройте второе
и зайдите под $APP_USER. Если что-то не так, у вас останется рабочая сессия,
чтобы починить.
EOF
