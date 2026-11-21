# Установка на свой сервер

## 1. Код

```bash
sudo mkdir -p /opt/wb-repricer
sudo chown "$USER":"$USER" /opt/wb-repricer
git clone <url> /opt/wb-repricer
cd /opt/wb-repricer
```

## 2. Секреты

```bash
cp .env.example .env && chmod 600 .env
nano .env
```

Обязательно:

- `WB_TOKEN` – токен кабинета WB с правом читать статистику и аналитику;
- `SITE_DIR` – каталог, куда писать HTML (корень сайта или `/var/www/...`);
- `PAGE_NAME` – имя файла страницы.

Если читаете остатки фуллфилмента из Google Таблицы – заполните `GS_URL`, `GS_TOKEN`, `AVAIL_*` и названия колонок в `AVAIL_FF_HEADERS`.

## 3. План кампании

```bash
cp config.example.json config.json
cp examples/state.example.json state.json
python3 wb_setup.py --token "$(grep ^WB_TOKEN= .env | cut -d= -f2-)"
# или положите свой plan.json
```

Для «цен под цель»:

```bash
mkdir -p op
cp examples/op-plan.example.json op/plan.json
cp examples/state.example.json op/state.json
# поправьте цели, даты и пороги в op/plan.json
```

## 4. Первый прогон

```bash
export TZ=Europe/Moscow
./run.sh
# и/или
./op/run.sh
```

Откройте страницу с сайта. Кириллица должна читаться (в HTML есть charset).

## 5. Приёмник отметок

```bash
python3 control/feedback_server.py
```

Пример nginx (фрагмент):

```nginx
location /wb-feedback {
    proxy_pass http://127.0.0.1:8092/wb-feedback;
    proxy_set_header X-FB-Key $http_x_fb_key;
    proxy_set_header Authorization $http_authorization;
}
```

Удобно оформить systemd-сервис на `control/feedback_server.py`.

## 6. Крон / systemd

Утренний расчёт – **один раз в день**, до того как менеджер ставит цены. Пример (07:00 МСК при сервере в UTC):

```
0 4 * * * cd /opt/wb-repricer && export TZ=Europe/Moscow && ./run.sh >> logs/run.log 2>&1
10 4 * * * cd /opt/wb-repricer && export TZ=Europe/Moscow && ./op/run.sh >> logs/op-run.log 2>&1
```

Цены из кабинета должны успеть попасть туда до следующего прогона: иначе шаг засчитывается следующим утром.

## 7. Опциональные источники

| Источник | Зачем | Без него |
|---|---|---|
| Google Sheets + мост | остаток фулла, «в пути», план продаж, ОП месяца | остаток дней только по складу WB; часть колонок пустые |
| mp-finance (sqlite) | фактическая ОП 7/14 дней как в ОПиУ | колонки маржи пустые или из таблицы |
| Выгрузки акций xlsx в `promo-reports/` | проходные цены акций | блок акций без проходных |

Мост таблиц – ваш Apps Script с тем же контрактом, что ожидают `avail.py` / `op_sheet.py` (actions `read`, `listSheets`, …). Токен моста только в `.env`.

## 8. Проверка, что данных кабинета нет в git

```bash
git status
git grep -E 'WB_TOKEN=.+' || true
ls state.json plan.json prices.json 2>/dev/null || echo "живых данных нет – так и должно быть в чистом клоне"
```

Живые файлы в `.gitignore`. Перед передачей репозитория третьим лицам ещё раз прогоните поиск по токену и названиям своих артикулов.
