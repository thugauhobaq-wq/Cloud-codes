#!/usr/bin/env bash
# Проверка autoupdate.sh без сервера: локальный «origin», подменённые docker
# и curl, состояние во временном каталоге. Каждый сценарий — свой вопрос:
# что делает скрипт, когда коммита нет, когда CI красный, когда сборка
# упала, когда изменился только один проект.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../autoupdate.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

pass=0; fail=0
none() { ! "$@"; }
check() {  # check "описание" условие... (none — «команда должна провалиться»)
    local what="$1"; shift
    if "$@"; then pass=$((pass + 1)); printf '  ✓ %s\n' "$what"
    else fail=$((fail + 1)); printf '  ✗ %s\n' "$what"; fi
}

# --- заглушки ----------------------------------------------------------------
mkdir -p "$T/bin"
cat > "$T/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $* [$(basename "$PWD")]" >> "$STUB_LOG"
if [ "$1" = "compose" ] && [ -n "${FAIL_IN:-}" ] && [ "$(basename "$PWD")" = "$FAIL_IN" ]; then
    echo "ERROR: build failed" >&2; exit 1
fi
exit 0
STUB
cat > "$T/bin/curl" <<'STUB'
#!/usr/bin/env bash
# Единственный сетевой вызов — отчёт в Telegram: запоминаем текст.
for a in "$@"; do case "$a" in text=*) echo "${a#text=}" >> "$STUB_LOG";; esac; done
exit 0
STUB
chmod +x "$T/bin/docker" "$T/bin/curl"
export PATH="$T/bin:$PATH" STUB_LOG="$T/stub.log"

# --- репозиторий: origin с двумя проектами и клон «сервера» -------------------
git init -q -b main "$T/origin"
gc() { git -C "$T/origin" -c user.name=t -c user.email=t@t commit -q -m "$1"; }
mkdir -p "$T/origin/alpha" "$T/origin/beta"
printf 'services: {}\n' > "$T/origin/alpha/docker-compose.yml"
printf 'services: {}\n' > "$T/origin/alpha/docker-compose.https.yml"
printf 'services: {}\n' > "$T/origin/beta/docker-compose.yml"
printf 'X=1\n' > "$T/origin/beta/.env.example"
git -C "$T/origin" add -A && gc "первый"
git clone -q "$T/origin" "$T/server"
printf 'KEY=1\n' > "$T/server/alpha/.env"

cat > "$T/env" <<CFG
REPO_DIR=$T/server
PROJECTS="alpha:docker-compose.yml,docker-compose.https.yml beta"
STATE_DIR=$T/state
TG_BOT_TOKEN=token
TG_CHAT_ID=42
REQUIRE_CI=0
CFG
run() { CLOUD_CODES_CONFIG="$T/env" bash "$SCRIPT" "$@" > "$T/out.log" 2>&1; }
ci_json() { printf '{"check_runs": [%s]}' "$1" > "$T/ci.json"; }

echo "▸ первый запуск разворачивает всё, что есть с .env"
: > "$STUB_LOG"; run
check "alpha собран с обоими compose-файлами" grep -q 'compose -f docker-compose.yml -f docker-compose.https.yml up -d --build --remove-orphans \[alpha\]' "$STUB_LOG"
check "beta без .env пропущен" none grep -q '\[beta\]' "$STUB_LOG"
check "отчёт: обновлено alpha, beta без .env" grep -q '✅.*alpha.*без .env: beta' "$STUB_LOG"
check "запомнен развёрнутый коммит" test "$(cat "$T/state/deployed")" = "$(git -C "$T/origin" rev-parse HEAD)"

echo "▸ повторный тик без новых коммитов ничего не делает"
: > "$STUB_LOG"; run
check "docker не вызывался" none grep -q docker "$STUB_LOG"
check "отчётов нет" none grep -q '✅\|❌' "$STUB_LOG"

echo "▸ коммит только в beta пересобирает только beta"
printf 'X=2\n' > "$T/server/beta/.env"
printf 'services: {a: 1}\n' > "$T/origin/beta/docker-compose.yml"; git -C "$T/origin" add -A && gc "beta: правка"
: > "$STUB_LOG"; run
check "beta собран" grep -q 'up -d --build --remove-orphans \[beta\]' "$STUB_LOG"
check "alpha не трогали" none grep -q '\[alpha\]' "$STUB_LOG"
check "отчёт с темой коммита" grep -q '✅.*«beta: правка».*beta' "$STUB_LOG"
check "сервер на новом коммите" test "$(git -C "$T/server" rev-parse HEAD)" = "$(git -C "$T/origin" rev-parse HEAD)"

echo "▸ коммит вне проектов не пересобирает ничего, но отмечается развёрнутым"
printf '# docs\n' > "$T/origin/README.md"; git -C "$T/origin" add -A && gc "документация"
: > "$STUB_LOG"; run
check "docker compose не вызывался" none grep -q 'compose' "$STUB_LOG"
check "коммит отмечен развёрнутым" test "$(cat "$T/state/deployed")" = "$(git -C "$T/origin" rev-parse HEAD)"

echo "▸ упавшая сборка: отчёт об ошибке, повтор только с новым коммитом"
printf 'services: {b: 2}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: ломаем"
: > "$STUB_LOG"; FAIL_IN=alpha run
check "отчёт ❌ с именем проекта" grep -q '❌.*не собрались: alpha' "$STUB_LOG"
check "коммит помечен как неудачный" test "$(cat "$T/state/failed")" = "$(git -C "$T/origin" rev-parse HEAD)"
: > "$STUB_LOG"; FAIL_IN=alpha run
check "второй тик молчит и не пересобирает" none grep -q 'compose\|❌' "$STUB_LOG"
printf 'services: {b: 3}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: чиним"
: > "$STUB_LOG"; run
check "новый коммит собирается и отчёт ✅" grep -q '✅.*«alpha: чиним».*alpha' "$STUB_LOG"
check "отметка о неудаче снята" test ! -f "$T/state/failed"

echo "▸ проверка CI"
sed -i 's/REQUIRE_CI=0/REQUIRE_CI=1/' "$T/env"
printf 'services: {c: 1}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: ждём CI"
ci_json '{"status":"in_progress","conclusion":null}'
: > "$STUB_LOG"; CI_JSON_FILE="$T/ci.json" run
check "CI идёт — ждём, ничего не собираем" none grep -q 'compose' "$STUB_LOG"
check "сервер остался на старом коммите" test "$(git -C "$T/server" rev-parse HEAD)" != "$(git -C "$T/origin" rev-parse HEAD)"
ci_json '{"status":"completed","conclusion":"failure"}'
: > "$STUB_LOG"; CI_JSON_FILE="$T/ci.json" run
check "CI красный — отчёт и без сборки" grep -q '❌ CI' "$STUB_LOG"
check "красный коммит не тянется" test "$(git -C "$T/server" rev-parse HEAD)" != "$(git -C "$T/origin" rev-parse HEAD)"
printf 'services: {c: 2}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: CI зелёный"
ci_json '{"status":"completed","conclusion":"success"},{"status":"completed","conclusion":"skipped"}'
: > "$STUB_LOG"; CI_JSON_FILE="$T/ci.json" run
check "зелёный CI — собираем" grep -q 'up -d --build --remove-orphans \[alpha\]' "$STUB_LOG"
ci_json ''
printf 'services: {c: 3}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: без проверок"
: > "$STUB_LOG"; CI_JSON_FILE="$T/ci.json" run
check "коммит без проверок (пути не под CI) — собираем" grep -q '\[alpha\]' "$STUB_LOG"

echo "▸ --force пересобирает всё без новых коммитов"
: > "$STUB_LOG"; run --force
check "alpha и beta собраны" bash -c "grep -q '\[alpha\]' '$STUB_LOG' && grep -q '\[beta\]' '$STUB_LOG'"

echo "▸ правки руками на сервере останавливают обновление"
printf 'services: {local: 1}\n' > "$T/server/alpha/docker-compose.yml"
git -C "$T/server" -c user.name=t -c user.email=t@t commit -qam "локальная правка"
printf 'services: {c: 4}\n' > "$T/origin/alpha/docker-compose.yml"; git -C "$T/origin" add -A && gc "alpha: ещё"
sed -i 's/REQUIRE_CI=1/REQUIRE_CI=0/' "$T/env"
: > "$STUB_LOG"; run
check "отчёт о невозможности перемотки" grep -q '❌ не могу обновить' "$STUB_LOG"
check "ничего не собиралось" none grep -q 'compose' "$STUB_LOG"

echo
echo "прошло: $pass, провалилось: $fail"
[ "$fail" = 0 ]
