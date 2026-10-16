#!/usr/bin/env bash
# Удаление PlanB (сервис + юнит). Данные и код остаются на месте — удалите каталог вручную.
set -euo pipefail
SERVICE_NAME="${SERVICE_NAME:-planb}"
PLANB_DIR="${PLANB_DIR:-/root/planb}"

[ "$(id -u)" = "0" ] || { echo "запустите от root"; exit 1; }

systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

echo "Сервис $SERVICE_NAME удалён."
echo "Каталог с кодом и базой остался: $PLANB_DIR (удалить: rm -rf $PLANB_DIR)"
