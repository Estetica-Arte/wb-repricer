#!/usr/bin/env bash
# Кампания «ОП 15%» (решение владельца 01.10.2026): тот же движок, цель – ОП после налогов по артикулу, контроль ДРР.
# Свои plan.json / state.json / feedback.json / recommendations.json / comments.json; общие с основной кампанией данные
# (заказы, остатки, акции, остатки дней, маржа, рынок) берутся из /opt/wb-repricer – их утром собирает основной прогон.
# Запуск: из /opt/wb-repricer/wb-daily-run.sh сразу после основной кампании; вручную – ./run.sh [--force] [--no-comment]
set -euo pipefail
cd "$(dirname "$0")"
export TZ=Europe/Moscow
export PATH="$PATH:/usr/local/bin:/usr/bin:$HOME/.local/bin"
M=/opt/wb-repricer
set -a; . "$M/.env"; set +a
FORCE=0; WANT_COMMENT=1
for a in "$@"; do case "$a" in --force) FORCE=1;; --no-comment) WANT_COMMENT=0;; esac; done
TODAY=$(date +%F)
LAST=$(python3 -c "import json;print(json.load(open('state.json'))['meta'].get('last_run',''))" 2>/dev/null || echo '')
if [ "$FORCE" = 0 ] && [ "$LAST" = "$TODAY" ]; then echo "[op] прогон за $TODAY уже был (--force для повтора)"; exit 0; fi
mkdir -p backup logs
for f in state.json price_history.json feedback.json recommendations.json comments.json; do
  [ -f "$f" ] && cp -f "$f" "backup/${f%.json}.$TODAY.json" || true
done
python3 feedback.py apply || echo "[op] !!! feedback apply не отработал"
ORD=""; [ -f "$M/orders_daily.json" ] && [ "$(date -r "$M/orders_daily.json" +%F)" = "$TODAY" ] && ORD="--orders $M/orders_daily.json"
echo "[op] $(date '+%F %T %Z') — считаю"
python3 "$M/wb_daily.py" --token "$WB_TOKEN" $ORD --stocks "$M/stocks_live.json" \
  --plan plan.json --state state.json --prices prices.json --disc discounts.json \
  --pricehist price_history.json --market "$M/market.json" --feedback feedback.json \
  --promos "$M/promos.json" --avail "$M/avail.json" --stockdays "$M/stock_days.json" --planmargin "$M/plan_margin.json" --season "$M/season.json" \
  --out page.html --tpl "$M/page_template.html"
python3 feedback.py inject --page page.html || echo "[op] !!! feedback inject"
python3 "$M/promos.py" inject --page page.html || echo "[op] !!! promos inject"
python3 "$M/plan_margin.py" inject --page page.html || echo "[op] !!! plan_margin inject"
if [ "$WANT_COMMENT" = 1 ]; then
  set +e; python3 "$M/comment.py" --page page.html --comments comments.json; set -e
else
  python3 "$M/comment.py" --page page.html --comments comments.json --inject-only || true
fi
SITE_DIR=/var/www/wb-report/wb PAGE_NAME=wb-op.html python3 "$M/publish.py" page.html wb-op.html
# общее меню отчётов (☰, «← Все отчёты») – как на остальных страницах панели
python3 /opt/mp-index/add_nav.py /var/www/wb-report/wb/wb-op.html wb --after '<body style="margin:0">' || echo "[op] меню не вставлено"
find backup -name '*.json' -mtime +30 -delete 2>/dev/null || true
echo "[op] $(date '+%F %T %Z') готово: https://your-panel.example/wb-reports/wb-op.html"
