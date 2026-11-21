#!/usr/bin/env bash
# Ежедневный прогон: забрать данные WB → посчитать решения → подсказка Grok → собрать и опубликовать страницу.
# Запуск:  ./run.sh               обычный прогон (один раз в сутки)
#          ./run.sh --force       повторный прогон в тот же день
#          ./run.sh --no-comment  без обращения к Grok за комментарием
#
# Серверная версия (14.09.2026): добавлены московское время (сервер живёт в America/New_York,
# без TZ «сегодня» в 07:00 МСК зимой было бы вчера), шаг comment.py и резерв comments.json.
# Сам движок wb_daily.py и publish.py — без изменений.
set -euo pipefail
cd "$(dirname "$0")"
export TZ=Europe/Moscow
export PATH="$PATH:/usr/local/bin:/usr/bin:$HOME/.local/bin"

[ -f .env ] && set -a && . ./.env && set +a

: "${WB_TOKEN:?нет WB_TOKEN — скопируйте .env.example в .env и впишите токен}"

FORCE=0; WANT_COMMENT=1
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --no-comment) WANT_COMMENT=0 ;;
  esac
done

TODAY=$(date +%F)
LAST=$(python3 -c "import json;print(json.load(open('state.json'))['meta'].get('last_run',''))" 2>/dev/null || echo '')

if [ "$FORCE" = 0 ] && [ "$LAST" = "$TODAY" ]; then
  echo "[run] прогон за $TODAY уже был. Повторный запуск в тот же день не нужен:"
  echo "[run] последним днём считается вчерашний, сегодняшний шаг движок увидит только завтра."
  echo "[run] если всё же нужно — ./run.sh --force"
  exit 0
fi

mkdir -p backup
cp -f state.json         "backup/state.$TODAY.json"         2>/dev/null || true
cp -f price_history.json "backup/price_history.$TODAY.json" 2>/dev/null || true
cp -f comments.json      "backup/comments.$TODAY.json"      2>/dev/null || true
cp -f feedback.json      "backup/feedback.$TODAY.json"      2>/dev/null || true
cp -f recommendations.json "backup/recommendations.$TODAY.json" 2>/dev/null || true

# Обратная связь по вчерашним рекомендациям (feedback.py): сверка с кабинетом по API,
# «поставил» → шаг в историю цен днём рекомендации; «не ставлю» → артикул отложен (holds).
# Идёт ДО движка, чтобы он увидел шаг сразу, а не через день.
python3 feedback.py apply || echo "[run] !!! feedback apply не отработал — движок считает без обратной связи"

# Локомотивы (реклама) и акции WB (promos.py): при сбое API остаётся прошлый promos.json.
python3 promos.py fetch || echo "[run] !!! promos fetch не отработал — движок считает по прошлому promos.json"

# Монитор рынка: частотность ядра запросов (из панели по товарым) и показы/позиция (API) — за вчера
python3 market.py || echo "[run] !!! market.py не отработал — графики рынка останутся на прошлых данных"

# ОП по артикулам из Google Таблицы (колонка BC листа «Цены ВБ (FBS Москва)») — для карточки графика
python3 op_sheet.py || echo "[run] !!! op_sheet не отработал — на странице останется прошлое значение ОП"

# Остаток дней (WB + фул) по артикулам из план-факта (SD_SHEET_ID в .env) — число рядом со значком «А»
python3 stockdays_sheet.py || echo "[run] !!! stockdays_sheet не отработал — на странице останется прошлый остаток дней"

# План продаж по артикулу (план-факт, столбец R) → заказов в день к плану; маржа после налогов из финансового сервиса
python3 plan_margin.py fetch || echo "[run] !!! plan_margin не отработал – колонки плана и маржи останутся прошлыми"

# Доступность (avail.py): остаток на фуле из таблицы «Рассчет поставки» (колонка AW) и фото карточек WB.
# Нет на остатках (фул и WB < 10) или нет фото — артикул не поднимается. При сбое остаётся прошлый avail.json.
python3 avail.py || echo "[run] !!! avail.py не отработал — движок считает по прошлому avail.json"

# сезонные коэффициенты и остатки план-факта для оборачиваемости (тот же расчёт, что колонка AE) – season.json
python3 season_days.py || echo "[run] !!! season_days.py не отработал – остаётся прошлый season.json"
echo "[run] $(date '+%F %T %Z') — считаю"
python3 wb_daily.py --token "$WB_TOKEN" \
  --plan plan.json --state state.json \
  --prices prices.json --disc discounts.json \
  --pricehist price_history.json --market market.json \
  --feedback feedback.json --promos promos.json --avail avail.json --stockdays stock_days.json --planmargin plan_margin.json --season season.json --stocks-out stocks_live.json \
  --out page.html --tpl page_template.html

# журнал сегодняшних рекомендаций (по нему завтра сверка) и блок обратной связи в странице
python3 feedback.py inject --page page.html || echo "[run] !!! feedback inject не отработал — кнопки на странице работать не будут"
# список акций по артикулу (значок «А»), ОП из таблицы и сводка локомотивов в строках страницы
python3 promos.py inject --page page.html || echo "[run] !!! promos inject не отработал — значок «А» и ОП на странице будут пустыми"
python3 plan_margin.py inject --page page.html || echo "[run] !!! plan_margin inject не отработал – колонки плана и маржи будут пустыми"

# Подсказка Grok за сегодня. Если Grok не ответил – страница публикуется с историей
# комментариев, но без нового: отчёт важнее подсказки.
if [ "$WANT_COMMENT" = 1 ]; then
  set +e
  python3 comment.py --page page.html --comments comments.json
  crc=$?
  set -e
  case $crc in
    0) echo "[run] комментарий Grok добавлен" ;;
    3) echo "[run] комментарий за сегодня уже был" ;;
    *) echo "[run] комментарий Grok НЕ добавлен (код $crc) – публикую без него" ;;
  esac
else
  python3 comment.py --page page.html --comments comments.json --inject-only || true
fi

python3 publish.py page.html "${PAGE_NAME:-wb-ceny.html}"

# бэкапы старше 30 дней не нужны
find backup -name '*.json' -mtime +30 -delete 2>/dev/null || true
echo "[run] $(date '+%F %T %Z') готово"
