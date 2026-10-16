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
    note_problem "порт $PORT никто не слушает — бот не запущен (journalctl -u $SERVICE_NAME -n 50)"
fi

# --------------------------------------------------------------- 4. локально
head_ "4. Ответ на самом сервере (127.0.0.1:$PORT)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/api/health" || echo 000)"
if [ "$CODE" = "200" ]; then
    ok "/api/health -> 200 ($(curl -s --max-time 5 "http://127.0.0.1:${PORT}/api/health"))"
else
    note_problem "/api/health -> $CODE (ожидалось 200)"
fi
CODE_PAGE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${AUTH_CURL[@]}" "http://127.0.0.1:${PORT}/" || echo 000)"
if [ "$CODE_PAGE" = "200" ]; then
    ok "главная страница отдаётся (200)"
else
    note_problem "главная страница -> $CODE_PAGE"
fi
for asset in static/styles.css static/app.js static/vendor/lightweight-charts.standalone.production.js; do
    C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PORT}/$asset" || echo 000)"
    [ "$C" = "200" ] && ok "$asset -> 200" || note_problem "$asset -> $C"
done

# ------------------------------------------------------------ 5. из интернета
head_ "5. Доступ из интернета"
EXT_IP="$(curl -s --max-time 5 https://api.ipify.org || hostname -I 2>/dev/null | awk '{print $1}')"
info "внешний IP этого сервера: ${EXT_IP:-неизвестен}"
CODE_EXT="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://${EXT_IP:-127.0.0.1}:${PORT}/api/health" || echo 000)"
if [ "$CODE_EXT" = "200" ]; then
    ok "по внешнему адресу отвечает: http://${EXT_IP}:${PORT}/api/health"
else
    note_problem "по внешнему адресу ответа нет ($CODE_EXT). Проверьте файрвол и Security Group облака:"
    info "  ufw: ufw status; открыть: ufw allow ${PORT}/tcp"
    info "  firewalld: firewall-cmd --state; открыть: firewall-cmd --permanent --add-port=${PORT}/tcp && firewall-cmd --reload"
    info "  iptables: iptables -S | grep ${PORT}"
    info "  облако: правило Ingress TCP ${PORT} в Security Group (панель провайдера)"
    info "  проверка с вашего компьютера: curl -v --max-time 6 http://${EXT_IP}:${PORT}/api/health"
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
mode = s.get("trade_mode")
print("      статус: %s | данные: %s | режим: %s" % (status, source, mode))
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

# ------------------------------------------------------------------- итоги
head_ "Итог"
if [ "$PROBLEMS" -eq 0 ]; then
    printf '  \033[1;32mВсё в порядке.\033[0m Панель: http://%s:%s/  (логин: %s, пароль: %s)\n\n' \
        "${EXT_IP:-СЕРВЕР}" "$PORT" "${AUTH_USER:-admin}" "${AUTH_PASS:-без пароля}"
    cat <<EOF
  Если в браузере «белый экран», проверьте по порядку:
    1) адрес набран по http:// (не https://) и с портом:  http://${EXT_IP:-СЕРВЕР}:${PORT}/
    2) браузер должен спросить логин и пароль. Пароль лежит в $ENV_FILE
       (переменная PLANB_AUTH_PASSWORD). Если окно не появляется — приватное окно браузера.
    3) откройте http://${EXT_IP:-СЕРВЕР}:${PORT}/api/health — должно быть {"ok":true,...}
    4) если /api/health открывается, а панель нет — пришлите текст ошибки с экрана:
       панель теперь пишет причину прямо на странице.
EOF
else
    printf '  \033[1;33mНайдено проблем: %s.\033[0m Смотрите пункты, отмеченные [!!].\n\n' "$PROBLEMS"
    echo "  Частые причины «белого экрана» в браузере:"
    echo "    • порт закрыт в файрволе/Security Group — тогда соединение просто зависает;"
    echo "    • браузер пытается открыть https на http-порту — наберите адрес как http://..."
    echo "    • не введён логин/пароль панели (браузер показывает окно с паролем)."
fi
exit 0
