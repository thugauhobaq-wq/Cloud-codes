#!/usr/bin/env bash
# Автообновление сервера из main этого репозитория.
#
# Запускается таймером раз в минуту (см. install.sh). Делает ровно одно:
# если в origin/main появился новый коммит и CI на нём зелёный — забирает его
# и пересобирает те проекты из списка, чьи файлы изменились. Отчёт уходит
# владельцу в Telegram, если в настройках есть токен; без него — только в
# журнал (journalctl -u cloud-codes-autoupdate).
#
# Секреты (.env проектов, токен для отчётов) лежат только на сервере; в
# репозитории их нет и быть не должно — он публичный.
#
#     cloud-codes-update            # обычный тик
#     cloud-codes-update --force    # пересобрать всё, даже без новых коммитов
set -euo pipefail

CONFIG="${CLOUD_CODES_CONFIG:-/etc/cloud-codes/env}"
[ -f "$CONFIG" ] && . "$CONFIG"

REPO_DIR="${REPO_DIR:-/opt/cloud-codes}"
BRANCH="${BRANCH:-main}"
# Проекты через пробел; после двоеточия — compose-файлы через запятую.
PROJECTS="${PROJECTS:-handwriting-ocr:docker-compose.yml,docker-compose.https.yml}"
GITHUB_REPO="${GITHUB_REPO:-thugauhobaq-wq/Cloud-codes}"
REQUIRE_CI="${REQUIRE_CI:-1}"
STATE_DIR="${STATE_DIR:-/var/lib/cloud-codes}"
TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
TG_CHAT_ID="${TG_CHAT_ID:-}"
TG_API="${TG_API:-https://api.telegram.org}"
GITHUB_API="${GITHUB_API:-https://api.github.com}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/lock"
# Сборка образа может идти дольше минуты — второй тик не должен начинать
# ту же работу параллельно.
flock -n 9 || { echo "предыдущий запуск ещё идёт"; exit 0; }

HOST="$(hostname -s 2>/dev/null || echo server)"

log() { printf '%s\n' "$*"; }

report() {
    # В Telegram — только если настроен; в журнал — всегда.
    log "$*"
    [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ] || return 0
    curl -fsS -m 20 -o /dev/null "$TG_API/bot$TG_BOT_TOKEN/sendMessage" \
        --data-urlencode "chat_id=$TG_CHAT_ID" \
        --data-urlencode "text=[$HOST] $*" \
        --data-urlencode "disable_web_page_preview=1" || log "отчёт в Telegram не ушёл"
}

ci_status() {
    # success | pending | failure | none — по проверкам GitHub на коммите.
    # Публичный репозиторий читается без токена; лимит 60 запросов в час, а
    # мы спрашиваем только когда появился новый коммит.
    local sha="$1" json
    if [ -n "${CI_JSON_FILE:-}" ]; then
        json="$(cat "$CI_JSON_FILE")"
    else
        json="$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
            "$GITHUB_API/repos/$GITHUB_REPO/commits/$sha/check-runs?per_page=100")" || { echo pending; return; }
    fi
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin).get("check_runs", [])
except Exception:
    print("pending"); sys.exit()
if not runs:
    print("none"); sys.exit()
if any(r.get("status") != "completed" for r in runs):
    print("pending"); sys.exit()
bad = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
print("failure" if bad else "success")
'
}

deploy_project() {
    # Пересобрать один проект; возвращает код compose, вывод — в журнал.
    local name="$1" files="$2" dir="$REPO_DIR/$1" args=() f out
    [ -d "$dir" ] || { log "$name: каталога нет"; return 1; }
    IFS=, read -r -a list <<<"$files"
    for f in "${list[@]}"; do args+=(-f "$f"); done
    if [ ! -f "$dir/.env" ] && [ -f "$dir/.env.example" ]; then
        log "$name: нет .env — пропускаю (скопируйте .env.example и заполните)"
        return 2
    fi
    ( cd "$dir" && docker compose "${args[@]}" up -d --build --remove-orphans ) 2>&1 | tail -n 40
    return "${PIPESTATUS[0]}"
}

cd "$REPO_DIR"
git fetch -q origin "$BRANCH"
current="$(git rev-parse HEAD)"
target="$(git rev-parse "origin/$BRANCH")"
deployed="$(cat "$STATE_DIR/deployed" 2>/dev/null || true)"
failed="$(cat "$STATE_DIR/failed" 2>/dev/null || true)"

if [ "$FORCE" = 0 ]; then
    if [ "$target" = "$deployed" ]; then exit 0; fi
    if [ "$target" = "$failed" ]; then exit 0; fi   # уже сообщали, ждём новый коммит
    if [ "$REQUIRE_CI" = 1 ]; then
        case "$(ci_status "$target")" in
            pending) log "CI на ${target:0:7} ещё идёт"; exit 0 ;;
            failure)
                echo "$target" > "$STATE_DIR/failed"
                report "❌ CI на ${target:0:7} красный — не обновляю: https://github.com/$GITHUB_REPO/commit/$target"
                exit 0 ;;
        esac
    fi
fi

if [ "$current" != "$target" ]; then
    # Только быстрая перемотка: если на сервере кто-то правил файлы руками,
    # лучше остановиться и сказать об этом, чем молча затереть.
    if ! git merge -q --ff-only "origin/$BRANCH" 2>"$STATE_DIR/git.err"; then
        echo "$target" > "$STATE_DIR/failed"
        report "❌ не могу обновить $REPO_DIR: $(head -c 300 "$STATE_DIR/git.err")"
        exit 0
    fi
fi

subject="$(git log -1 --pretty=%s "$target")"

# Что пересобирать: всё при первом запуске и по --force, иначе только те
# проекты, чьи файлы менялись между прошлым развёрнутым коммитом и новым.
changed=""
if [ -n "$deployed" ] && [ "$FORCE" = 0 ] && git cat-file -e "$deployed" 2>/dev/null; then
    changed="$(git diff --name-only "$deployed" "$target" | cut -d/ -f1 | sort -u)"
fi

done_list=""; fail_list=""; skip_list=""
for entry in $PROJECTS; do
    name="${entry%%:*}"
    files="${entry#*:}"; [ "$files" = "$entry" ] && files="docker-compose.yml"
    if [ -n "$deployed" ] && [ "$FORCE" = 0 ] && ! grep -qx "$name" <<<"$changed"; then
        continue
    fi
    log "▸ $name"
    rc=0; deploy_project "$name" "$files" || rc=$?
    case "$rc" in
        0) done_list="$done_list $name" ;;
        2) skip_list="$skip_list $name" ;;
        *) fail_list="$fail_list $name" ;;
    esac
done

# Старые образы после пересборки копятся и съедают диск маленького VPS.
docker image prune -f >/dev/null 2>&1 || true

if [ -n "$fail_list" ]; then
    echo "$target" > "$STATE_DIR/failed"
    report "❌ ${target:0:7} «$subject»: не собрались:$fail_list. Подробности: journalctl -u cloud-codes-autoupdate"
    exit 0
fi
echo "$target" > "$STATE_DIR/deployed"
rm -f "$STATE_DIR/failed"
if [ -n "$done_list" ]; then
    report "✅ ${target:0:7} «$subject»: обновлено —$done_list${skip_list:+; без .env:$skip_list}"
else
    log "${target:0:7} «$subject»: проекты из списка не менялись"
fi
