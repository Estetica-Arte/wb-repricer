#!/bin/bash
# Проверка «План заказов» или запись цены в 23:50. Цены в кабинет не пишет.
cd /opt/wb-repricer
export TZ=Europe/Moscow
export PATH="$PATH:$HOME/.local/bin"
set -a
. ./.env
set +a
if systemctl is-active --quiet wb-repricer.service; then
  echo "[plan] идёт утренний расчёт – пропуск"
  exit 0
fi
exec 9>/tmp/wb-promos-refresh.lock
flock -w 900 9 || { echo "[plan] занято другим пересчётом"; exit 0; }
python3 order_plan.py "${1:-check}"
