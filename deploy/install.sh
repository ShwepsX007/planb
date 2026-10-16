#!/usr/bin/env bash
# =============================================================================
#  PlanB — установка на сервер (Ubuntu/Debian/CentOS/Fedora) под systemd
#
#  Скрипт идемпотентный: повторный запуск обновляет код и перезапускает сервис.
#  Ничего, кроме своего каталога, своего юнита systemd и одного порта, не трогает —
#  можно ставить рядом с другими проектами.
#
#  Быстрый старт:
#      bash install.sh
#
#  Параметры (переменные окружения):
#      PLANB_DIR=/root/planb          каталог установки
#      PLANB_PORT=8015                порт веб-панели
#      PLANB_AUTH_USER=admin          логин панели
#      PLANB_AUTH_PASSWORD=...        пароль (если пусто — сгенерируется)
#      PLANB_REPO=https://github.com/ShwepsX007/planb.git
#      PLANB_BRANCH=auto              auto|main|<ветка>
#      PLANB_PRESET=demo              demo — синтетический рынок (браузер без ключей);
#                                     пусто — реальные данные Gate.io
#      PLANB_AUTOSTART=1              поднимать бота после перезагрузки сервера
#      PLANB_OPEN_FIREWALL=0          1 — автоматически открыть порт в ufw/firewalld
#      PLANB_SOURCE=/путь/к/коду      установить из локального каталога вместо git
#      PLANB_FORCE=0                  1 — ставить, даже если порт занят чужим процессом
#      (скрипт сам подберёт python3.9+ и, при необходимости, поставит python3.11)
#
#  Пример:
#      PLANB_PORT=8015 PLANB_AUTH_PASSWORD='мой-пароль' bash install.sh
# =============================================================================
set -euo pipefail

PLANB_DIR="${PLANB_DIR:-/root/planb}"
PLANB_PORT="${PLANB_PORT:-8015}"
PLANB_AUTH_USER="${PLANB_AUTH_USER:-admin}"
PLANB_AUTH_PASSWORD="${PLANB_AUTH_PASSWORD:-}"
PLANB_REPO="${PLANB_REPO:-https://github.com/ShwepsX007/planb.git}"
PLANB_BRANCH="${PLANB_BRANCH:-auto}"
PLANB_PRESET="${PLANB_PRESET-demo}"
PLANB_AUTOSTART="${PLANB_AUTOSTART:-1}"
PLANB_OPEN_FIREWALL="${PLANB_OPEN_FIREWALL:-0}"
PLANB_SOURCE="${PLANB_SOURCE:-}"
PLANB_FORCE="${PLANB_FORCE:-0}"
SERVICE_NAME="${SERVICE_NAME:-planb}"

C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
log()  { printf '\n%s==> %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn() { printf '\n%s!!  %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
die()  { printf '\n%sОшибка: %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }
note() { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_OFF"; }

[ "$(id -u)" = "0" ] || die "запустите скрипт от root: sudo -i, затем bash install.sh"
[ -n "$PLANB_PORT" ] || die "PLANB_PORT пуст"

# ---------------------------------------------------------------- 0. что занято
log "Свободные/занятые порты (чтобы не мешать другим проектам)"
if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | head -20 || true
else
    note "ss недоступен — пропускаю проверку"
fi

port_busy() { ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -Eq "[:.]${PLANB_PORT}\$"; }
if port_busy; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        log "порт $PLANB_PORT занят нашим сервисом $SERVICE_NAME — он будет перезапущен"
    elif [ "$PLANB_FORCE" = "1" ]; then
        warn "порт $PLANB_PORT занят, но PLANB_FORCE=1 — продолжаю"
    else
        warn "порт $PLANB_PORT уже занят другим процессом:"
        ss -ltnp 2>/dev/null | grep -E "[:.]${PLANB_PORT}\s" || true
        die "выберите свободный порт, например: PLANB_PORT=8016 bash install.sh"
    fi
else
    note "порт $PLANB_PORT свободен — займём его"
fi

# ------------------------------------------------------------------ 1. пакеты
log "Проверяю системные пакеты (python3, venv, git, curl)"
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-pip git curl ca-certificates >/dev/null
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3 python3-pip git curl ca-certificates >/dev/null
else
    warn "незнакомый пакетный менеджер — убедитесь, что есть python3 (>=3.9), git, curl"
fi

# выбираем самый свежий доступный интерпретатор (>= 3.9)
pick_python() {
    local c
    for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$c" >/dev/null 2>&1 && \
           "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            command -v "$c"; return 0
        fi
    done
    return 1
}
PYBIN="$(pick_python || true)"
if [ -z "$PYBIN" ]; then
    warn "нет Python 3.9+ — пробую поставить python3.11 из пакетов"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get install -y -qq python3.11 python3.11-venv >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q python3.11 >/dev/null 2>&1 || true
    fi
    PYBIN="$(pick_python || true)"
fi
[ -n "$PYBIN" ] || die "нужен Python 3.9+; установите его (например: apt-get install python3.11 python3.11-venv)"
PYVER="$("$PYBIN" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"
note "Python $PYVER ($PYBIN)"

# ------------------------------------------------------------------- 2. код
log "Устанавливаю код в $PLANB_DIR"
mkdir -p "$PLANB_DIR"

USED_SOURCE=0
if [ -n "$PLANB_SOURCE" ]; then
    [ -d "$PLANB_SOURCE" ] || die "PLANB_SOURCE=$PLANB_SOURCE не найден"
    [ -f "$PLANB_SOURCE/app/main.py" ] || die "в $PLANB_SOURCE нет app/main.py"
    command -v rsync >/dev/null 2>&1 || apt-get install -y -qq rsync >/dev/null 2>&1 || true
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'data' \
              --exclude '__pycache__' --exclude 'node_modules' "$PLANB_SOURCE"/ "$PLANB_DIR"/
    else
        # tar есть всегда; исключения те же, чтобы не затирать базу и venv
        tar -C "$PLANB_SOURCE" \
            --exclude='./.git' --exclude='./.venv' --exclude='./data' \
            --exclude='./__pycache__' --exclude='*/__pycache__' --exclude='*/node_modules' \
            -cf - . | tar -C "$PLANB_DIR" -xf -
    fi
    USED_SOURCE=1
    note "код скопирован из $PLANB_SOURCE (без .git, .venv и data)"
elif [ -d "$PLANB_DIR/.git" ]; then
    git -C "$PLANB_DIR" fetch --all --prune --tags >/dev/null 2>&1 || warn "не удалось обновить из git"
    note "репозиторий уже склонирован — обновляю"
else
    git clone --quiet "$PLANB_REPO" "$PLANB_DIR" 2>/dev/null \
        || die "не удалось склонировать $PLANB_REPO (проверьте доступ в интернет)"
fi

if [ "$USED_SOURCE" = "0" ] && [ -d "$PLANB_DIR/.git" ]; then
    BRANCH="$PLANB_BRANCH"
    if [ "$BRANCH" = "auto" ]; then
        if git -C "$PLANB_DIR" cat-file -e "origin/main:app/main.py" 2>/dev/null; then
            BRANCH="main"
        elif git -C "$PLANB_DIR" cat-file -e "origin/arena/00ac38c1-planb:app/main.py" 2>/dev/null; then
            BRANCH="arena/00ac38c1-planb"
        else
            die "в репозитории не найден код бота (ветки main и arena/00ac38c1-planb)"
        fi
    fi
    git -C "$PLANB_DIR" checkout --quiet "$BRANCH" 2>/dev/null || true
    git -C "$PLANB_DIR" reset --hard --quiet "origin/$BRANCH" 2>/dev/null || true
    note "ветка: $BRANCH ($(git -C "$PLANB_DIR" log -1 --format='%h %s' | cut -c1-60))"
fi

[ -f "$PLANB_DIR/app/main.py" ] || die "в $PLANB_DIR нет app/main.py — проверьте ветку/каталог"

# ------------------------------------------------------------------ 3. venv
log "Создаю виртуальное окружение и ставлю зависимости"
if [ ! -x "$PLANB_DIR/.venv/bin/python" ]; then
    "$PYBIN" -m venv "$PLANB_DIR/.venv" \
        || die "не удалось создать venv (установите пакет python3-venv / python3.11-venv)"
fi
# если venv создан старым интерпретатором, а появился более новый — пересоздаём
if ! "$PLANB_DIR/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    warn "пересоздаю venv: версия Python в нём младше 3.9"
    rm -rf "$PLANB_DIR/.venv"
    "$PYBIN" -m venv "$PLANB_DIR/.venv" || die "не удалось пересоздать venv"
fi
"$PLANB_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PLANB_DIR/.venv/bin/pip" install --quiet -r "$PLANB_DIR/requirements.txt"
"$PLANB_DIR/.venv/bin/python" -c "import fastapi, uvicorn, ccxt; print('    зависимости ok, ccxt', ccxt.__version__)"

mkdir -p "$PLANB_DIR/data"

# ------------------------------------------------------------------- 4. .env
log "Пишу конфигурацию $PLANB_DIR/.env"
if [ -z "$PLANB_AUTH_PASSWORD" ]; then
    if [ -f "$PLANB_DIR/.env" ] && grep -q '^PLANB_AUTH_PASSWORD=' "$PLANB_DIR/.env"; then
        PLANB_AUTH_PASSWORD="$(grep '^PLANB_AUTH_PASSWORD=' "$PLANB_DIR/.env" | head -1 | cut -d= -f2-)"
        note "пароль панели оставлен прежним (из .env)"
    else
        PLANB_AUTH_PASSWORD="$( (command -v openssl >/dev/null 2>&1 && openssl rand -hex 8) \
            || python3 -c 'import secrets;print(secrets.token_hex(8))' )"
        note "сгенерирован пароль панели"
    fi
fi

if [ -f "$PLANB_DIR/.env" ]; then
    cp -a "$PLANB_DIR/.env" "$PLANB_DIR/.env.bak.$(date +%Y%m%d-%H%M%S)"
fi
cat > "$PLANB_DIR/.env" <<EOF
# PlanB — конфигурация сервиса (создано install.sh $(date -u +%Y-%m-%dT%H:%M:%SZ))
PLANB_HOST=0.0.0.0
PLANB_PORT=$PLANB_PORT
PLANB_DB=$PLANB_DIR/data/planb.sqlite3
PLANB_AUTH_USER=$PLANB_AUTH_USER
PLANB_AUTH_PASSWORD=$PLANB_AUTH_PASSWORD
PLANB_AUTOSTART=$PLANB_AUTOSTART
EOF
if [ -n "$PLANB_PRESET" ]; then
    echo "PLANB_PRESET=$PLANB_PRESET" >> "$PLANB_DIR/.env"
fi
chmod 600 "$PLANB_DIR/.env"
note "порт $PLANB_PORT, база $PLANB_DIR/data/planb.sqlite3"
if printf '%s' "$PLANB_AUTH_PASSWORD" | LC_ALL=C grep -q '[^ -~]'; then
    warn "в пароле есть не-ASCII символы (например кириллица). Он поддерживается, но если
      браузер капризничает — задайте пароль из латиницы и цифр:
      PLANB_AUTH_PASSWORD='novy-parol123' bash $PLANB_DIR/deploy/install.sh"
fi
[ -n "$PLANB_PRESET" ] && note "пресет данных: $PLANB_PRESET (демо-рынок; реальные данные включаются в панели)"

# ---------------------------------------------------------------- 5. systemd
log "Настраиваю systemd-сервис $SERVICE_NAME"
UNIT="/etc/systemd/system/$SERVICE_NAME.service"
cat > "$UNIT" <<EOF
[Unit]
Description=PlanB — торговый бот на коррекции Фибоначчи (веб-панель, порт $PLANB_PORT)
Documentation=$PLANB_REPO
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
WorkingDirectory=$PLANB_DIR
EnvironmentFile=$PLANB_DIR/.env
ExecStart=$PLANB_DIR/.venv/bin/python run.py
Restart=always
RestartSec=5
TimeoutStartSec=30
SyslogIdentifier=$SERVICE_NAME
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

# ---------------------------------------------------------------- 6. проверка
log "Проверяю, что сервис поднялся"
OK=0
for i in $(seq 1 30); do
    if curl -fsS --max-time 2 -u "${PLANB_AUTH_USER}:${PLANB_AUTH_PASSWORD}" \
            "http://127.0.0.1:${PLANB_PORT}/api/health" >/dev/null 2>&1; then
        OK=1; break
    fi
    sleep 1
done

if [ "$OK" != "1" ]; then
    warn "сервис не ответил за 30 секунд. Диагностика:"
    systemctl status "$SERVICE_NAME" --no-pager -l | head -20 || true
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    die "проверьте ошибки выше (частая причина — занятый порт или нет доступа в интернет)"
fi

HEALTH="$(curl -fsS "http://127.0.0.1:${PLANB_PORT}/api/health" || true)"

# страница панели и её ресурсы — именно то, что открывает браузер
PANEL_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -u "${PLANB_AUTH_USER}:${PLANB_AUTH_PASSWORD}" "http://127.0.0.1:${PLANB_PORT}/" || echo 000)"
ASSET_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:${PLANB_PORT}/static/app.js" || echo 000)"
if [ "$PANEL_CODE" = "200" ] && [ "$ASSET_CODE" = "200" ]; then
    note "страница панели отдаётся (200), статика отдаётся (200)"
else
    warn "проверка страницы не прошла: / -> $PANEL_CODE, /static/app.js -> $ASSET_CODE"
    note "диагностика: bash $PLANB_DIR/deploy/check.sh"
fi
IP="$( (hostname -I 2>/dev/null | awk '{print $1}') || echo 'IP_сервера')"

# --------------------------------------------------------------- 7. firewall
log "Файрвол"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    if [ "$PLANB_OPEN_FIREWALL" = "1" ]; then
        ufw allow "$PLANB_PORT"/tcp >/dev/null && note "порт открыт в ufw"
    else
        warn "ufw включён — порт закрыт. Открыть: ufw allow ${PLANB_PORT}/tcp"
    fi
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    if [ "$PLANB_OPEN_FIREWALL" = "1" ]; then
        firewall-cmd --permanent --add-port="$PLANB_PORT"/tcp >/dev/null && firewall-cmd --reload >/dev/null && note "порт открыт в firewalld"
    else
        warn "firewalld активен. Открыть: firewall-cmd --permanent --add-port=${PLANB_PORT}/tcp && firewall-cmd --reload"
    fi
else
    note "локальный файрвол не активен — проверьте, что порт $PLANB_PORT открыт в панели облачного провайдера (Security Group / Firewall)"
fi

# ------------------------------------------------------------------ 8. итоги
cat <<EOF

${C_OK}Готово.${C_OFF}  Сервис: $SERVICE_NAME   Порт: $PLANB_PORT   Каталог: $PLANB_DIR

  Панель:      http://${IP}:${PLANB_PORT}/
  Логин:       $PLANB_AUTH_USER
  Пароль:      $PLANB_AUTH_PASSWORD        ${C_DIM}(лежит в $PLANB_DIR/.env)${C_OFF}
  Health:      $HEALTH

  Что дальше:
    1. Откройте панель, вкладка «Настройки» — при желании смените пароль/порт (см. ниже),
       введите API-ключи Gate.io (Futures/Spot Trade, без права вывода).
    2. Нажмите «Проверить подключение», затем вкладка «Бэктест» — прогон параметров.
    3. Для реальной торговли: trade_mode = live, «Сохранить». Затем кнопка «Старт».
       Бот продолжит работу после перезагрузки сервера (PLANB_AUTOSTART=$PLANB_AUTOSTART).

  Управление сервисом:
    systemctl status $SERVICE_NAME          состояние и аптайм
    systemctl restart $SERVICE_NAME         перезапуск (бот поднимется сам)
    systemctl stop $SERVICE_NAME            остановить
    journalctl -u $SERVICE_NAME -f          логи в реальном времени
    journalctl -u $SERVICE_NAME -n 100      последние 100 строк

  Обновление до новой версии:
    cd $PLANB_DIR && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart $SERVICE_NAME
    ${C_DIM}или просто снова: bash $PLANB_DIR/deploy/install.sh${C_OFF}

  Проверка вручную:
    curl -u $PLANB_AUTH_USER:$PLANB_AUTH_PASSWORD http://127.0.0.1:$PLANB_PORT/api/health

  Если браузер показывает пустой экран:
    1) сначала: bash $PLANB_DIR/deploy/check.sh   — скрипт проверит всё по шагам;
    2) наберите адрес строго по http:// и с портом: http://<IP>:${PLANB_PORT}/ ;
    3) проверьте с рабочего компьютера: curl -v --max-time 6 http://<IP>:${PLANB_PORT}/api/health
       — если виснет, порт закрыт в файрволе или Security Group облака;
    4) если порт закрыть нельзя — опубликуйте панель через уже работающий nginx:
       bash $PLANB_DIR/deploy/setup-nginx.sh        (адрес вида http://ДОМЕН/planb/)

  Работа рядом с другими проектами:
    • занят только порт $PLANB_PORT (проверить: ss -ltnp | grep ${PLANB_PORT});
    • отдельный юнит $SERVICE_NAME, данные — в $PLANB_DIR/data;
    • nginx и другие сервисы не изменялись. Если нужен домен/HTTPS — пример конфига в README
      (панель работает и за префиксом вида https://домен/planb/).

EOF
