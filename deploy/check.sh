#!/usr/bin/env bash
# =============================================================================
#  PlanB — проверка установки. Запускается на сервере: bash deploy/check.sh
#  Отвечает на вопрос «почему панель не открывается в браузере».
# =============================================================================
set -uo pipefail

SERVICE_NAME="${SERVICE_NAME:-planb}"
PLANB_DIR="${PLANB_DIR:-/root/planb}"
ENV_FILE="$PLANB_DIR/.env"

ok()   { printf '  \033[1;32m[ok]\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[1;31m[!!]\033[0m   %s\n' "$*"; }
info() { printf '  \033[2m[..]\033[0m   %s\n' "$*"; }
head_() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }

PROBLEMS=0
note_problem() { PROBLEMS=$((PROBLEMS + 1)); bad "$*"; }

# ------------------------------------------------------------------ 1. сервис
head_ "1. Сервис systemd"
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    ok "служба $SERVICE_NAME активна"
    RESTARTS="$(systemctl show "$SERVICE_NAME" -p NRestarts --value 2>/dev/null | head -1 || true)"
    [ -n "$RESTARTS" ] || RESTARTS="$(systemctl show "$SERVICE_NAME" -p NRestarts 2>/dev/null | cut -d= -f2)"
    case "$RESTARTS" in ''|*[!0-9]*) RESTARTS=0 ;; esac
    if [ -n "$RESTARTS" ] && [ "$RESTARTS" != "0" ]; then
        info "служба перезапускалась $RESTARTS раз(а) — если порт ниже не слушается, смотрите причину в разделе 3"
    fi
else
    note_problem "служба $SERVICE_NAME не активна — смотрите: systemctl status $SERVICE_NAME; journalctl -u $SERVICE_NAME -n 50"
fi
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    ok "автозапуск при загрузке включён"
else
    info "автозапуск не включён: systemctl enable $SERVICE_NAME"
fi

# -------------------------------------------------------------------- 2. .env
head_ "2. Конфигурация $ENV_FILE"
if [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
    PORT="${PLANB_PORT:-8000}"
    AUTH_USER="${PLANB_AUTH_USER:-}"
    AUTH_PASS="${PLANB_AUTH_PASSWORD:-}"
    ok "порт из .env: $PORT, база: ${PLANB_DB:-не задана}"
    if [ -n "$AUTH_PASS" ]; then
        ok "панель защищена паролем (логин: ${AUTH_USER:-admin}); пароль: ${AUTH_PASS}"
        if printf '%s' "$AUTH_PASS" | LC_ALL=C grep -q '[^ -~]'; then
            info "в пароле есть не-ASCII символы. Работает, но надёжнее задать пароль из латиницы:"
            info "  PLANB_AUTH_PASSWORD='латиница-цифры' bash $PLANB_DIR/deploy/install.sh"
        fi
    else
        info "пароль не задан — панель открыта без авторизации"
    fi
    AUTOSTART="${PLANB_AUTOSTART:-1}"
    info "PLANB_AUTOSTART=$AUTOSTART (1 — бот продолжает работу после перезагрузки сервера)"
else
    note_problem "нет файла $ENV_FILE — установка не завершена"
    PORT="${PLANB_PORT:-8015}"
    AUTH_USER="admin"; AUTH_PASS=""
fi

AUTH_CURL=()
[ -n "${AUTH_PASS:-}" ] && AUTH_CURL=(-u "${AUTH_USER:-admin}:${AUTH_PASS}")

# ------------------------------------------------------------------- 3. порт
head_ "3. Порт $PORT"
if ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | grep -Eq "[:.]${PORT}\$"; then
    WHO="$(ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}\s" | head -1)"
    ok "порт слушается: $WHO"
    case "$WHO" in
        *"$SERVICE_NAME"*|*python*) ok "слушает python (наш бот)" ;;
        *) info "порт занят не нашим процессом — проверьте, что бот запущен" ;;
    esac
else
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        note_problem "служба $SERVICE_NAME активна, но порт $PORT не слушается — процесс не смог запуститься"
    else
        note_problem "порт $PORT никто не слушает — бот не запущен"
    fi
    # сразу показываем причину: последние строки журнала, счётчик перезапусков и
    # самопроверку кода — чтобы не гадать, почему сервис молчит
    info "последние события службы (journalctl):"
    journalctl -u "$SERVICE_NAME" -n 25 --no-pager 2>/dev/null | sed 's/^/        /' \
        || info " journalctl недоступен"
    SHOW="$(systemctl show "$SERVICE_NAME" -p NRestarts -p Result -p ExecMainStatus -p ActiveState -p SubState 2>/dev/null | tr '\n' ' ')"
    [ -n "$SHOW" ] && info "systemd: $SHOW"
    if [ -x "$PLANB_DIR/.venv/bin/python" ]; then
        if IMPORT_OUT="$(cd "$PLANB_DIR" && timeout 60 ./.venv/bin/python -c 'import app.main; print("импорт кода ок")' 2>&1)"; then
            info "код бота импортируется без ошибок ($IMPORT_OUT)"
            info "если в журнале выше видно 'биржа не ответила' — это таймаут сети, бот просто не стартовал"
        else
            note_problem "код бота не импортируется — ошибка в самом приложении:"
            printf '%s\n' "$IMPORT_OUT" | tail -12 | sed 's/^/        /'
            info "лечение:  cd $PLANB_DIR && git pull && .venv/bin/pip install -r requirements.txt"
        fi
    else
        info "нет $PLANB_DIR/.venv/bin/python — окружение не создано (переустановите: bash deploy/install.sh)"
    fi
    info "перезапустить:  systemctl restart $SERVICE_NAME    (после перезапуска подождите 5-10 секунд)"
    info "смотреть живьём: journalctl -u $SERVICE_NAME -f"
fi

# --------------------------------------------------------------- 4. локально
head_ "4. Ответ на самом сервере (127.0.0.1:$PORT)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)"
[ -z "$CODE" ] && CODE=000
if [ "$CODE" = "200" ]; then
    ok "/api/health -> 200 ($(curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/health"))"
else
    note_problem "/api/health -> $CODE (ожидалось 200)"
fi
CODE_PAGE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${AUTH_CURL[@]}" "http://127.0.0.1:${PORT}/" 2>/dev/null || true)"
[ -z "$CODE_PAGE" ] && CODE_PAGE=000
if [ "$CODE_PAGE" = "200" ]; then
    ok "главная страница отдаётся (200)"
else
    note_problem "главная страница -> $CODE_PAGE"
fi
for asset in static/styles.css static/app.js static/vendor/lightweight-charts.standalone.production.js; do
    C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/$asset" 2>/dev/null || true)"
    [ -z "$C" ] && C=000
    [ "$C" = "200" ] && ok "$asset -> 200" || note_problem "$asset -> $C"
done

# ------------------------------------------------------------ 5. из интернета
head_ "5. Доступ из интернета"
EXT_IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I 2>/dev/null | awk '{print $1}')"
info "внешний IP этого сервера: ${EXT_IP:-неизвестен}"
CODE_EXT="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://${EXT_IP:-127.0.0.1}:${PORT}/api/health" 2>/dev/null || true)"
[ -z "$CODE_EXT" ] && CODE_EXT=000
if [ "$CODE_EXT" = "200" ]; then
    ok "локально по внешнему адресу отвечает: http://${EXT_IP}:${PORT}/api/health"
    info "важно: эта проверка идёт с самого сервера и НЕ доказывает, что порт открыт"
    info "наружу (Security Group облака может блокировать внешних клиентов)."
else
    note_problem "по внешнему адресу ответа нет ($CODE_EXT). Проверьте файрвол и Security Group облака:"
    info "  ufw: ufw status; открыть: ufw allow ${PORT}/tcp"
    info "  firewalld: firewall-cmd --state; открыть: firewall-cmd --permanent --add-port=${PORT}/tcp && firewall-cmd --reload"
    info "  iptables: iptables -S | grep ${PORT}"
    info "  облако: правило Ingress TCP ${PORT} в Security Group (панель провайдера)"
fi

# реальная проверка снаружи через независимый сервис (если есть доступ в интернет)
if command -v python3 >/dev/null 2>&1 && [ -n "${EXT_IP:-}" ]; then
    info "проверяю доступность ${PORT} с внешнего узла (check-host.net)…"
    REQ_ID="$(curl -s --max-time 8 -H 'Accept: application/json' \
        "https://check-host.net/check-tcp?host=${EXT_IP}:${PORT}&max_nodes=3" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin).get("request_id",""))' 2>/dev/null || true)"
    if [ -n "$REQ_ID" ]; then
        sleep 8
        RESULT="$(curl -s --max-time 8 -H 'Accept: application/json' \
            "https://check-host.net/check-result/${REQ_ID}" || true)"
        STATS="$(printf '%s' "$RESULT" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("0 0"); raise SystemExit
ok = bad = 0
for value in (data or {}).values():
    if value is None:
        continue
    if isinstance(value, list) and value and isinstance(value[0], dict) and value[0].get("time") is not None:
        ok += 1
    else:
        bad += 1
print(f"{ok} {bad}")
' 2>/dev/null || echo "0 0")"
        OK_NODES="${STATS%% *}"; BAD_NODES="${STATS##* }"
        if [ "${OK_NODES:-0}" -gt 0 ] 2>/dev/null; then
            ok "порт $PORT доступен из интернета (успешных проверок: $OK_NODES)"
        elif [ "${BAD_NODES:-0}" -gt 0 ] 2>/dev/null; then
            note_problem "из интернета порт $PORT недоступен (неудачных проверок: $BAD_NODES)"
            info "  это работающий порт на сервере, но внешние подключения не доходят —"
            info "  почти всегда это Security Group облака (правило Ingress TCP $PORT)."
            info "  вариант без открытия порта — публикация через уже работающий nginx:"
            info "    bash $PLANB_DIR/deploy/setup-nginx.sh     # адрес вида http://ДОМЕН/planb/"
        else
            info "внешний сервис не дал результат — проверьте вручную с домашнего компьютера:"
            info "  curl -v --max-time 8 http://${EXT_IP}:${PORT}/api/health"
        fi
    else
        info "внешний сервис недоступен с этого сервера — проверьте вручную с домашнего компьютера:"
        info "  curl -v --max-time 8 http://${EXT_IP}:${PORT}/api/health"
    fi
fi
if command -v ufw >/dev/null 2>&1; then
    info "ufw: $(ufw status 2>/dev/null | head -1 || echo 'недоступен')"
fi
if command -v firewall-cmd >/dev/null 2>&1; then
    info "firewalld: $(firewall-cmd --state 2>/dev/null || echo 'не активен')"
fi
if command -v iptables >/dev/null 2>&1; then
    info "iptables-правила по порту: $(iptables -S 2>/dev/null | grep -c "${PORT}")"
fi

# ------------------------------------------------------------------ 6. nginx
head_ "6. nginx (если панель нужна через 80/443)"
if command -v nginx >/dev/null 2>&1; then
    ok "nginx установлен: $(nginx -v 2>&1)"
    if grep -rqs "127.0.0.1:${PORT}\|localhost:${PORT}" /etc/nginx 2>/dev/null; then
        ok "в конфигах nginx есть прокси на порт $PORT"
        info "адреса:"
        grep -rEs "server_name|proxy_pass .*:${PORT}" /etc/nginx 2>/dev/null | sed 's/^/      /' | head -12
    else
        info "nginx не проксирует порт $PORT. Готовый скрипт:"
        info "  bash $PLANB_DIR/deploy/setup-nginx.sh            # префикс /planb/ у текущего сайта"
        info "  DOMAIN=bot.example.com bash $PLANB_DIR/deploy/setup-nginx.sh   # отдельный поддомен"
    fi
else
    info "nginx не установлен — панель доступна по прямому порту $PORT"
fi

# ------------------------------------------------------------------- 7. бот
head_ "7. Состояние бота"
STATE_JSON="$(curl -s --max-time 5 "${AUTH_CURL[@]}" "http://127.0.0.1:${PORT}/api/state" || true)"
if [ -n "$STATE_JSON" ]; then
    PY=python3
    [ -x "$PLANB_DIR/.venv/bin/python" ] && PY="$PLANB_DIR/.venv/bin/python"
    PLANB_STATE_PAYLOAD="$STATE_JSON" "$PY" - <<'PYEOF' || info "не удалось разобрать ответ /api/state"
import json, os
raw = os.environ.get("PLANB_STATE_PAYLOAD") or "{}"
s = json.loads(raw)
e = s.get("engine") or {}
status = s.get("status")
source = s.get("data_source")
source_label = s.get("data_source_label") or source
real = s.get("data_real")
mode = s.get("trade_mode")
print("      статус: %s | режим: %s" % (status, mode))
print("      данные: %s (%s)" % (source_label, "РЕАЛЬНЫЕ, с биржи" if real else "выдуманные (синтетика)"))
feed = s.get("feed") or {}
if feed.get("last_error"):
    print("      ОШИБКА ДАННЫХ: %s" % feed.get("last_error"))
if feed.get("last_candle_ts"):
    import time as _t
    print("      последняя свеча: %s UTC"
          % _t.strftime("%d.%m.%Y %H:%M", _t.gmtime(feed["last_candle_ts"] / 1000)))
print("      пара: %s %s | цена: %s" % (s.get("symbol"), s.get("timeframe"), s.get("price")))
print("      профит: %s $ | циклов: %s | сигналов: %s"
      % (e.get("realized_pnl"), e.get("cycles_total"), e.get("signals_found")))
c = e.get("cycle")
if c:
    pos = c.get("positions") or []
    print("      открытый цикл: %s, позиций %s, средняя %.2f, цель %.2f"
          % (c.get("side"), len(pos), float(c.get("avg_entry") or 0), float(c.get("target") or 0)))
print("      последняя проверка: %s" % e.get("last_check"))
PYEOF
else
    info "не удалось получить /api/state (нужны логин/пароль или бот не отвечает)"
fi

# --------------------------------------------------------- 8. связь с биржей
head_ "8. Реальные данные с биржи (Gate.io)"
GATE_URL='https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BTC_USDT&interval=5m&limit=2'
GATE_CODE="$(curl -s -o /tmp/planb_gate.json -w '%{http_code}' --max-time 15 "$GATE_URL" 2>/dev/null || true)"
[ -z "$GATE_CODE" ] && GATE_CODE=000
if [ "$GATE_CODE" = "200" ]; then
    ok "сервер видит биржу Gate.io (HTTP 200) — реальные свечи будут приходить"
    GATE_PRICE="$(python3 - <<'PYEOF' 2>/dev/null || true
import json
try:
    rows = json.load(open("/tmp/planb_gate.json"))
    print(rows[-1][2])
except Exception:
    pass
PYEOF
)"
    [ -n "$GATE_PRICE" ] && info "последняя цена BTC/USDT с биржи: $GATE_PRICE (значит данные настоящие)"
    if grep -q 'PLANB_PRESET=synthetic' "$ENV_FILE" 2>/dev/null; then
        bad "в .env стоит PLANB_PRESET=synthetic — бот работает на выдуманных свечах"
        info "чтобы работать на реальных данных: уберите строку PLANB_PRESET (или поставьте demo) и"
        info "перезапустите сервис, либо выберите источник «Gate.io» на вкладке «Настройки» в панели"
    fi
elif [ "$GATE_CODE" = "000" ]; then
    note_problem "с сервера нет связи с api.gateio.ws — реальные свечи получить не удастся"
    info "проверьте интернет/DNS:  curl -v --max-time 15 'https://api.gateio.ws/api/v4/spot/time'"
    info "если биржа закрыта для вашей страны — используйте синтетику или прокси/другой сервер"
else
    note_problem "биржа ответила кодом $GATE_CODE на публичный запрос свечей"
    info "попробуйте вручную:  curl -s -m 15 '$GATE_URL' | head -c 300"
fi

# ------------------------------------------------------------------- итоги
head_ "Итог"
if [ "$PROBLEMS" -eq 0 ]; then
    printf '  \033[1;32mВсё в порядке.\033[0m Панель: http://%s:%s/  (логин: %s, пароль: %s)\n\n' \
        "${EXT_IP:-СЕРВЕР}" "$PORT" "${AUTH_USER:-admin}" "${AUTH_PASS:-без пароля}"
    cat <<EOF
  Как открыть панель в браузере:
    1) адрес панели — КОРЕНЬ, без пути:   http://${EXT_IP:-СЕРВЕР}:${PORT}/
       (адрес .../api/health открывать не нужно — там служебный JSON; если откроете его
        в браузере, страница сама предложит ссылку на панель)
    2) лучше в приватном окне — чтобы браузер не подставил сохранённый старый пароль;
    3) логин: ${AUTH_USER:-admin}, пароль: ${AUTH_PASS:-не задан} (файл $ENV_FILE);
    4) сменить пароль:  bash $PLANB_DIR/deploy/set-auth.sh 'novy-parol123'
       отключить пароль: bash $PLANB_DIR/deploy/set-auth.sh --off
       посмотреть доступ: bash $PLANB_DIR/deploy/set-auth.sh --show
    5) если не открывается — пришлите текст с экрана: панель пишет причину прямо на странице.

  Реальные данные (виртуальные сделки на настоящих свечах):
    • вкладка «Настройки» → «Источник данных» = Gate.io → «Сохранить» (API-ключи НЕ нужны);
    • кнопка «Проверить подключение» покажет цену и задержку ответа биржи;
    • бэктест на реальной истории: вкладка «Бэктест» → выбрать пару, период и нажать
      «Скачать и запустить» — свечи сервер загрузит сам, ничего искать не нужно.
EOF
else
    printf '  \033[1;33mНайдено проблем: %s.\033[0m Смотрите пункты, отмеченные [!!].\n\n' "$PROBLEMS"
    echo "  Частые причины «белого экрана» в браузере:"
    echo "    • порт закрыт в файрволе/Security Group — тогда соединение просто зависает;"
    echo "    • браузер пытается открыть https на http-порту — наберите адрес как http://..."
    echo "    • не введён логин/пароль панели (браузер показывает окно с паролем)."
fi
exit 0
