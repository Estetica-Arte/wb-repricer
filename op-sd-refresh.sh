#!/bin/bash
# Свежие ОП из юнитки и остаток дней по текущим штукам фула. Движок цен не запускается:
# рекомендации утра остаются, на странице меняются ОП и дни остатка.
# --cron: 11, 15, 19 и 23 МСК (утро 07:00 считает run.sh).
set -u
cd /opt/wb-repricer
export TZ=Europe/Moscow
if [ "${1:-}" = "--cron" ]; then
  h=$(date +%H)
  case "$h" in
    11|15|19|23) ;;
    *) exit 0 ;;
  esac
  systemctl is-active --quiet wb-repricer.service && { echo "[op-sd] идёт утренний расчёт – пропуск"; exit 0; }
  pgrep -f "wb-daily-run.sh|/opt/wb-repricer/run.sh|/opt/wb-repricer/op/run.sh" >/dev/null && { echo "[op-sd] идёт расчёт – пропуск"; exit 0; }
fi
exec 9>/tmp/wb-promos-refresh.lock
flock -w 600 9 || { echo "[op-sd] занято другим пересчётом"; exit 0; }
set -a; . ./.env; set +a
M=/opt/wb-repricer
echo "=== $(date '+%F %T %Z') ОП и остаток дней"
python3 op_sheet.py || { echo "[op-sd] op_sheet не отработал"; exit 1; }
python3 avail.py --ff-only || { echo "[op-sd] avail --ff-only не отработал"; exit 1; }
python3 sd_apply.py --page page.html --page op/page.html || { echo "[op-sd] sd_apply не отработал"; exit 1; }
python3 publish.py page.html "${PAGE_NAME:-wb-ceny.html}" | tail -1
cd "$M/op"
SITE_DIR=/var/www/wb-report/wb PAGE_NAME=wb-op.html python3 "$M/publish.py" page.html wb-op.html | tail -1
python3 /opt/mp-index/add_nav.py /var/www/wb-report/wb/wb-op.html wb --after '<body style="margin:0">' >/dev/null
/opt/mp-index/run.sh 2>&1 | tail -1
echo "=== $(date '+%F %T %Z') готово"
