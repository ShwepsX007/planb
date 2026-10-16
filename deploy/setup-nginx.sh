#!/usr/bin/env bash
# =============================================================================
#  PlanB — публикация панели через уже установленный nginx (порты 80/443).
#
#  Нужно, если прямой порт закрыт облачным файрволом (Security Group) или если
#  хочется домен и HTTPS. Скрипт не трогает чужие проекты: он изменяет только
#  один выбранный файл, делает резервную копию и откатывает правку, если
#  `nginx -t` не проходит.
#
#  Режим 1 (по умолчанию) — префикс на текущем сайте:
#      bash deploy/setup-nginx.sh
#      -> http://<ваш_домен_или_IP>/planb/
#
#  Режим 2 — отдельный поддомен:
#      DOMAIN=planb.example.com bash deploy/setup-nginx.sh
#      -> http://planb.example.com/planb/   (+ certbot --nginx -d planb.example.com)
#
#  Параметры:
#      PLANB_PORT=8015        порт бота (127.0.0.1:8015)
#      PREFIX=/planb/         префикс пути
#      DOMAIN=                домен (включает режим 2)
#      NGINX_DIR=/etc/nginx   каталог конфигурации
#      DRY_RUN=1              показать план, ничего не менять
# =============================================================================
set -euo pipefail

PLANB_PORT="${PLANB_PORT:-8015}"
PREFIX="${PREFIX:-/planb/}"
DOMAIN="${DOMAIN:-}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
DRY_RUN="${DRY_RUN:-0}"
STAMP="$(date +%Y%m%d-%H%M%S)"

C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
log()  { printf '\n%s==> %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn() { printf '\n%s!!  %s%s\n' "$C_WARN" "$*" "$C_OFF"; }
die()  { printf '\n%sОшибка: %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }
note() { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_OFF"; }

command -v nginx >/dev/null 2>&1 || die "nginx не установлен — панель работает по прямому порту ${PLANB_PORT}"
[ -d "$NGINX_DIR" ] || die "не найден каталог $NGINX_DIR"
command -v python3 >/dev/null 2>&1 || die "нужен python3"

[[ "$PREFIX" == /* ]] || PREFIX="/$PREFIX"
[[ "$PREFIX" == */ ]] || PREFIX="$PREFIX/"
PREFIX_NOSLASH="${PREFIX%/}"          # /planb

reload_nginx() {
    if ! nginx -t >/dev/null 2>&1; then
        nginx -t || true
        return 1
    fi
    systemctl reload nginx 2>/dev/null || nginx -s reload
    return 0
}

# ---------------------------------------------------------------- режим 2
if [ -n "$DOMAIN" ]; then
    log "Отдельный поддомен: $DOMAIN  ->  127.0.0.1:${PLANB_PORT}"
    TARGET="$NGINX_DIR/conf.d/planb.conf"

    if [ "$DRY_RUN" = "1" ]; then
        note "DRY_RUN: будет создан $TARGET"
        exit 0
    fi

    [ -f "$TARGET" ] && cp -a "$TARGET" "$TARGET.planb-bak-$STAMP" && note "копия: $TARGET.planb-bak-$STAMP"
    mkdir -p "$NGINX_DIR/conf.d"

    cat > "$TARGET" <<EOF
# PlanB — панель бота (создано deploy/setup-nginx.sh $STAMP)
server {
    listen 80;
    server_name ${DOMAIN};

    location = / { return 301 ${PREFIX}; }
    location ${PREFIX_NOSLASH}/ {
        proxy_pass http://127.0.0.1:${PLANB_PORT}/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }
}
EOF
    note "создан $TARGET"

    if reload_nginx; then
        log "Готово"
        cat <<EOF

  Панель:  http://${DOMAIN}${PREFIX}      ${C_DIM}(нужна A-запись ${DOMAIN} -> IP сервера)${C_OFF}
  HTTPS:   apt-get install certbot python3-certbot-nginx && certbot --nginx -d ${DOMAIN}
  Откат:   rm ${TARGET} && systemctl reload nginx
EOF
        exit 0
    fi
    rm -f "$TARGET"
    die "конфигурация откатана (файл удалён)"
fi

# ---------------------------------------------------------------- режим 1
log "Префикс на текущем сайте: ${PREFIX_NOSLASH}/  ->  127.0.0.1:${PLANB_PORT}"

mapfile -t CANDIDATES < <(grep -rlE '^[[:space:]]*server[[:space:]]*\{' \
    "$NGINX_DIR/conf.d" "$NGINX_DIR/sites-enabled" "$NGINX_DIR/sites-available" 2>/dev/null \
    | grep -v 'planb' | sort -u || true)

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    die "не найдено ни одного server-блока в $NGINX_DIR.
    Если nginx ещё не настроен — используйте режим поддомена:
      DOMAIN=bot.example.com bash $0"
fi

TARGET="$(python3 - "${CANDIDATES[@]}" <<'PYEOF'
import re, sys
best, best_score = None, -1
for path in sys.argv[1:]:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    score = 0
    if re.search(r"listen\s+[^;]*443", text): score += 4
    if "default_server" in text: score += 3
    if re.search(r"listen\s+[^;]*\b80\b", text): score += 2
    if "server_name _;" in text: score += 1
    if score > best_score:
        best, best_score = path, score
print(best or "")
PYEOF
)"
[ -n "$TARGET" ] || die "не удалось выбрать конфигурацию для правки"
note "файл основного сайта: $TARGET"

if [ "$DRY_RUN" = "1" ]; then
    note "DRY_RUN: правка не применяется"
    exit 0
fi

[ "$DRY_RUN" = "1" ] || cp -a "$TARGET" "$TARGET.planb-bak-$STAMP"
note "резервная копия: $TARGET.planb-bak-$STAMP"

python3 - "$TARGET" "$PREFIX_NOSLASH" "$PLANB_PORT" <<'PYEOF'
import re, sys

path, prefix, port = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path, encoding="utf-8").read()

# 1. убираем наш прежний блок, если он уже был
src, removed = re.subn(
    r"\n[ \t]*# --- PlanB \(добавлено setup-nginx\.sh[^\n]*\n[\s\S]*?# --- /PlanB ---\n",
    "\n", src)
if removed:
    print(f"    обновляю существующий блок PlanB (найдено: {removed})")

snippet = f"""
    # --- PlanB (добавлено setup-nginx.sh; удаляется этим же скриптом при повторном запуске) ---
    location {prefix}/ {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;
    }}
    location = {prefix} {{ return 301 {prefix}/; }}
    # --- /PlanB ---
"""

# 2. находим все server-блоки верхнего уровня и выбираем лучший
blocks = []
for m in re.finditer(r"(?m)^[ \t]*server[ \t]*\{", src):
    start = m.start()
    depth, i = 0, m.end() - 1
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                blocks.append((start, i))
                break
        i += 1

if not blocks:
    print("!! в файле нет server-блока — ничего не изменено")
    sys.exit(1)

def score(block_text):
    s = 0
    if re.search(r"listen\s+[^;]*443", block_text): s += 4
    if "default_server" in block_text: s += 3
    if re.search(r"listen\s+[^;]*\b80\b", block_text): s += 2
    if "server_name _;" in block_text: s += 1
    return s

best = max(blocks, key=lambda b: score(src[b[0]:b[1]]))
insert_at = best[1]
src = src[:insert_at].rstrip() + "\n" + snippet + src[insert_at:]
open(path, "w", encoding="utf-8").write(src)
print("    блок location добавлен")
PYEOF

if reload_nginx; then
    log "Готово"
    cat <<EOF

  Панель:  http://<ваш_домен или IP>${PREFIX}
  ${C_DIM}Пути в интерфейсе относительные, поэтому префикс работает и для API, и для WebSocket.${C_OFF}
  Проверка через внешний адрес:  curl -u admin:ПАРОЛЬ http://ДОМЕН${PREFIX_NOSLASH}/api/health
  Откат:  cp -a $TARGET.planb-bak-$STAMP $TARGET && systemctl reload nginx
EOF
else
    cp -a "$TARGET.planb-bak-$STAMP" "$TARGET"
    warn "восстановлен исходный файл из резервной копии — чужой сайт не пострадал"
    die "nginx -t не прошёл проверку, изменения отменены"
fi
