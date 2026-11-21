#!/bin/bash
# Пересчёт акций после загрузки выгрузки со страницы (wb-control → _promo_upload).
# Реклама не перекачивается (--no-ads: медленный API), рекомендации цен не пересчитываются –
# обновляются участие в акциях и проходные цены на обеих страницах; движок учтёт новые проходные утром.
set -u
cd /opt/wb-repricer
exec 9>/tmp/wb-promos-refresh.lock
flock -w 600 9 || { echo "[promos-refresh] занято другим пересчётом"; exit 0; }
export TZ=Europe/Moscow
# --cron: ежечасно (с 13.10.2026) – значки рекламы (кампании идут / на паузе) и акции в течение дня; 08–23 МСК,
# не во время утреннего расчёта (он сам пишет page.html)
if [ "${1:-}" = "--cron" ]; then
  h=$(date +%H); [ "$h" -ge 8 ] && [ "$h" -le 23 ] || exit 0
  systemctl is-active --quiet wb-repricer.service && { echo "[promos-refresh] идёт утренний расчёт – пропуск"; exit 0; }
  pgrep -f "wb-daily-run.sh|/opt/wb-repricer/run.sh|/opt/wb-repricer/op/run.sh" >/dev/null && { echo "[promos-refresh] идёт расчёт – пропуск"; exit 0; }
fi
set -a; . ./.env; set +a
M=/opt/wb-repricer
echo "=== $(date '+%F %T %Z') пересчёт акций и значков рекламы${1:+ ($1)}"
python3 promos.py fetch --no-ads || { echo "[promos-refresh] promos.py fetch не отработал"; exit 1; }
# ОП за 7 дней: утром отчёта WB за вчера могло ещё не быть (op7_lag > 0) – подтягиваем, как только появится
if python3 -c "import json,sys;sys.exit(0 if (json.load(open('plan_margin.json'))['store'].get('op7_lag') or 0)>0 else 1)" 2>/dev/null; then
  python3 plan_margin.py refresh7 && python3 plan_margin.py inject --page page.html && python3 plan_margin.py inject --page op/page.html
fi
python3 promos.py inject --page page.html
python3 publish.py page.html "${PAGE_NAME:-wb-ceny.html}" | tail -1
cd "$M/op"
python3 "$M/promos.py" inject --page page.html
SITE_DIR=/var/www/wb-report/wb PAGE_NAME=wb-op.html python3 "$M/publish.py" page.html wb-op.html | tail -1
python3 /opt/mp-index/add_nav.py /var/www/wb-report/wb/wb-op.html wb --after '<body style="margin:0">' >/dev/null
/opt/mp-index/run.sh 2>&1 | tail -1
echo "=== $(date '+%F %T %Z') готово"
