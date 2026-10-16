#!/usr/bin/env bash
# =============================================================================
#  PlanB — управление доступом к панели без правки файлов вручную.
#
#  Использование:
#     bash deploy/set-auth.sh --show              показать логин/пароль и порт
#     bash deploy/set-auth.sh 'novy-parol123'     задать новый пароль (латиница — надёжнее)
#     bash deploy/set-auth.sh --off               отключить пароль (ПАНЕЛЬ ОТКРЫТА ВСЕМ!)
#     bash deploy/set-auth.sh --user operator 'parol'   сменить и логин
#
#  Скрипт правит только строки PLANB_AUTH_* в .env, сохраняет остальные настройки,
#  перезапускает сервис и сразу проверяет панель. Код и база не затрагиваются.
# =============================================================================
set -euo pipefail

PLANB_DIR="${PLANB_DIR:-/root/planb}"
SERVICE_NAME="${SERVICE_NAME:-planb}"
ENV_FILE="$PLANB_DIR/.env"
PYBIN="$PLANB_DIR/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
die() { printf '%sОшибка: %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }
note() { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_OFF"; }

[ -f "$ENV_FILE" ] || die "не найден $ENV_FILE (сначала: bash $PLANB_DIR/deploy/install.sh)"
command -v "$PYBIN" >/dev/null 2>&1 || die "нет python3"

# читаем текущее состояние
read_env() {
    "$PYBIN" - "$ENV_FILE" "$@" <<'PYEOF'
import re, sys

path = sys.argv[1]
want = sys.argv[2:]
values = {}
for line in open(path, encoding="utf-8"):
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$", line)
    if m:
        values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
for key in want:
    print(values.get(key, ""))
PYEOF
}

write_env() {
    # write_env КЛЮЧ=ЗНАЧЕНИЕ [КЛЮЧ=ЗНАЧЕНИЕ ...]
    "$PYBIN" - "$ENV_FILE" "$@" <<'PYEOF'
import re, sys

path, pairs = sys.argv[1], sys.argv[2:]
updates = {}
for pair in pairs:
    key, _, value = pair.partition("=")
    updates[key] = value

lines = open(path, encoding="utf-8").read().splitlines()
seen = set()
out = []
for line in lines:
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    if m and m.group(1) in updates:
        key = m.group(1)
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PYEOF
}

# ------------------------------------------------------------------ аргументы
MODE="set"
NEW_USER=""
NEW_PASS=""
case "${1:-}" in
    --show|-s) MODE="show" ;;
    --off|-o)  MODE="off" ;;
    --user|-u) MODE="set"; NEW_USER="${2:-}"; NEW_PASS="${3:-}"; [ -n "$NEW_USER" ] || die "укажите логин: --user ЛОГИН ПАРОЛЬ" ;;
    ""|--help|-h)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    *) MODE="set"; NEW_PASS="$1" ;;
esac

PORT="$(read_env PLANB_PORT | head -1)"; PORT="${PORT:-8015}"
CUR_USER="$(read_env PLANB_AUTH_USER | head -1)"; CUR_USER="${CUR_USER:-admin}"
CUR_PASS="$(read_env PLANB_AUTH_PASSWORD | head -1)"

if [ "$MODE" = "show" ]; then
    printf '\n  Панель:  http://%s:%s/\n' "$( (curl -s --max-time 4 https://api.ipify.org || hostname -I | awk '{print $1}') 2>/dev/null)" "$PORT"
    printf '  Логин:   %s\n' "$CUR_USER"
    if [ -n "$CUR_PASS" ]; then
        printf '  Пароль:  %s\n' "$CUR_PASS"
    else
        printf '  Пароль:  %s(не задан — панель без авторизации)%s\n' "$C_WARN" "$C_OFF"
    fi
    printf '  Файл:    %s\n\n' "$ENV_FILE"
    exit 0
fi

if [ "$MODE" = "off" ]; then
    printf '\n%s!!  Отключаю пароль: панель станет доступна всем, кто знает адрес.%s\n' "$C_WARN" "$C_OFF"
    printf '    Порт %s должен быть закрыт файрволом/Security Group, либо используйте nginx с Basic-авторизацией.\n' "$PORT"
    printf '    Вернуть защиту: bash %s/deploy/set-auth.sh %s\n\n' "$PLANB_DIR" "parol123"
    cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    write_env "PLANB_AUTH_PASSWORD="
    TARGET_USER="$CUR_USER"; TARGET_PASS=""
else
    [ -n "$NEW_PASS" ] || die "укажите новый пароль: bash $0 'novy-parol123'"
    [ "$NEW_USER" = "" ] || CUR_USER="$NEW_USER"
    if printf '%s' "$NEW_PASS" | LC_ALL=C grep -q '[^ -~]'; then
        printf '%s!!  В пароле есть не-ASCII символы. Работает, но если браузер капризничает — используйте латиницу и цифры.%s\n' "$C_WARN" "$C_OFF"
    fi
    cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    write_env "PLANB_AUTH_USER=$CUR_USER" "PLANB_AUTH_PASSWORD=$NEW_PASS"
    TARGET_USER="$CUR_USER"; TARGET_PASS="$NEW_PASS"
    note "пароль обновлён в $ENV_FILE (резервная копия рядом)"
fi

# ------------------------------------------------------------- перезапуск
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}\.service"; then
    systemctl restart "$SERVICE_NAME"
    note "служба $SERVICE_NAME перезапущена"
else
    printf '%s    служба %s не найдена в systemd — перезапустите бота вручную%s\n' "$C_DIM" "$SERVICE_NAME" "$C_OFF"
fi

# --------------------------------------------------------------- проверка
AUTH=()
[ -n "$TARGET_PASS" ] && AUTH=(-u "${TARGET_USER}:${TARGET_PASS}")
CODE_PAGE=""; CODE_API=""; CODE_WRONG=""; CODE_NOAUTH=""
for _ in $(seq 1 20); do
    CODE_PAGE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "${AUTH[@]}" "http://127.0.0.1:${PORT}/" || true)"
    [ "$CODE_PAGE" = "200" ] && break
    sleep 1
done
CODE_ASSET="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/static/app.js" || true)"
if [ -n "$TARGET_PASS" ]; then
    CODE_NOAUTH="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PORT}/" || true)"
    CODE_WRONG="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 -u "${TARGET_USER}:zavedomo-ne-tot" "http://127.0.0.1:${PORT}/" || true)"
fi
CODE_API="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "${AUTH[@]}" "http://127.0.0.1:${PORT}/api/state" || true)"

printf '\n'
if [ "$CODE_PAGE" = "200" ] && [ "$CODE_ASSET" = "200" ] && [ "$CODE_API" = "200" ]; then
    printf '%sПанель отвечает и работает.%s\n' "$C_OK" "$C_OFF"
    if [ -n "$TARGET_PASS" ]; then
        printf '   ввод логина/пароля проверен (200), без пароля: %s, с неверным паролем: %s\n' \
               "${CODE_NOAUTH:-—}" "${CODE_WRONG:-—}"
    else
        printf '   авторизация отключена — страница открывается без пароля: %s\n' "$CODE_NOAUTH"
    fi
else
    printf '%sПанель отвечает неверно: страница=%s, статика=%s, api=%s%s\n' \
           "$C_ERR" "$CODE_PAGE" "$CODE_ASSET" "$CODE_API" "$C_OFF"
    printf '   диагностика: bash %s/deploy/check.sh\n' "$PLANB_DIR"
    exit 1
fi

IP="$( (curl -s --max-time 4 https://api.ipify.org || hostname -I | awk '{print $1}') 2>/dev/null)"
printf '\n  Открывайте в браузере (лучше в приватном окне, чтобы браузер не подставил старый пароль):\n'
printf '     http://%s:%s/\n' "${IP:-СЕРВЕР}" "$PORT"
if [ -n "$TARGET_PASS" ]; then
    printf '  Пароль: %s\n' "$TARGET_PASS"
    CURL_AUTH="-u ${TARGET_USER}:${TARGET_PASS}"
else
    printf '  Пароль: (авторизация отключена)\n'
    CURL_AUTH=""
fi
printf '\n  Тот же адрес можно проверить с рабочего компьютера:\n'
printf '     curl -I %s http://%s:%s/\n\n' "$CURL_AUTH" "${IP:-СЕРВЕР}" "$PORT"
