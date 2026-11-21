#!/usr/bin/env python3
"""Стратегия «План заказов».

На артикул ставится отдельно. Правила оборачиваемости на него не действуют.
Сутки – 12 отрезков по 2 часа по тепловой карте. Замер в конце каждого:
02:00, 04:00, …, 22:00 и полночь.
Заказы – лента WB (statistics /supplier/orders): все оформленные за сутки,
вместе с отменёнными. На замере план – сколько заказов должно было накопиться
с полуночи по долям уже закрытых отрезков. Процент – факт с полуночи ÷ этот план.
Шкала в настройках смотрит на этот процент и говорит, что сделать с ценой.
Своя карта – если за последние 30 полных суток заказов не меньше порога
со страницы. Иначе карта магазина.
Карта артикула собирается за 30 полных суток. Если заказов меньше 100, окно
полных дней удлиняется назад, пока не станет 100 или не кончится история.
Таблица долей пересобирается раз в день.
При включении старт – средняя цена заказа за предыдущий день.
В 23:50 фиксирует цену этого дня: следующие сутки считаются от неё.

Команды: check | anchor | curves | test [артикул]
Цены в кабинет WB не пишет.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "op", "order_plan.json")
MSK = ZoneInfo("Europe/Moscow")
SLOTS = [(i * 2, i * 2 + 2) for i in range(12)]
BANDS = (
    (0, 10, "down", 5), (10, 20, "down", 5), (20, 30, "down", 5),
    (30, 40, "down", 4), (40, 50, "down", 4), (50, 60, "down", 4),
    (60, 70, "down", 3), (70, 80, "down", 2), (80, 90, "down", 1),
    (90, 100, "same", 0), (100, 110, "same", 0),
    (110, 120, "up", 2), (120, 130, "up", 3), (130, 140, "up", 4),
)


def now_msk():
    return datetime.datetime.now(MSK)


def empty():
    return {
        "up_step_pp": 1.0,
        "down_step_pp": 1.0,
        "bands": [],
        "articles": {},
        "anchor": {"date": "", "prices": {}},
        "samples": {},
        "today": {},
        "live": {},
        "curves": {},
        "shop_slots": [],
        "curves_through": "",
        "curves_from": "",
        "curves_min": 0,
        "curves_bins": 0,
        "curves_n30": False,
        "curves_feed": "",
        "curve_min": 100,
    }


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = empty()
    base = empty()
    if isinstance(data, dict):
        base.update({k: data.get(k, base[k]) for k in base})
    if not isinstance(base["articles"], dict):
        base["articles"] = {}
    return base


def save(data):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STATE)


def env_token():
    token = os.environ.get("WB_TOKEN") or ""
    if token:
        return token
    path = os.path.join(DIR, ".env")
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith("WB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def api(url, params, timeout=180):
    token = env_token()
    if not token:
        raise RuntimeError("нет токена WB")
    cmd = ["curl", "-sS", "-G", url, "-H", f"Authorization: {token}", "--max-time", str(timeout), "-w", "\n%{http_code}"]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    for _ in range(4):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError("WB не ответил")
        body, _, code = out.stdout.rpartition("\n")
        code = code.strip()
        if code == "429":
            time.sleep(65)
            continue
        if code != "200" or not body.strip():
            raise RuntimeError(f"WB ответил {code or 'пусто'}")
        return json.loads(body)
    raise RuntimeError("WB не ответил: 429")


def parse_dt(value):
    text = str(value or "")
    if text.endswith("Z"):
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(MSK)
    if len(text) >= 19:
        dt = datetime.datetime.fromisoformat(text[:19])
        return dt.replace(tzinfo=MSK)
    return None


def orders_today(day):
    """Лента заказов за календарные сутки МСК: артикул → 12 чисел по отрезкам в 2 часа.

    Все оформленные, вместе с отменёнными. flag=1 – заказы этой даты.
    """
    start = datetime.datetime(day.year, day.month, day.day, tzinfo=MSK)
    raw = api(
        "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
        {"dateFrom": day.isoformat(), "flag": 1},
    )
    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("orders") or []
    buckets = {}
    for row in raw:
        dt = parse_dt(row.get("date"))
        if not dt or dt.date() != day:
            continue
        art = str(row.get("supplierArticle") or "").strip()
        if not art:
            continue
        slot = min(dt.hour // 2, 11)
        buckets.setdefault(art, [0] * 12)[slot] += 1
    return buckets, start


def cabinet_prices():
    prices = {}
    off = 0
    while True:
        j = api(
            "https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter",
            {"limit": 1000, "offset": off},
        )
        goods = ((j.get("data") or {}).get("listGoods")) or []
        for it in goods:
            sz = (it.get("sizes") or [{}])[0]
            if sz.get("discountedPrice"):
                prices[str(it.get("vendorCode"))] = round(sz["discountedPrice"])
        if len(goods) < 1000:
            break
        off += 1000
    return prices


def file_orders():
    path = os.path.join(DIR, "orders_daily.json")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def history_orders():
    """Заказы WB за длинный срок: чтобы артикулу с малым потоком набрать 100 штук."""
    start = now_msk().date() - datetime.timedelta(days=400)
    try:
        data = api(
            "https://statistics-api.wildberries.ru/api/v1/supplier/orders",
            {"dateFrom": start.isoformat()},
            timeout=300,
        )
        if isinstance(data, dict):
            data = data.get("data") or data.get("orders") or []
        if isinstance(data, list) and len(data) > 100:
            return data
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[plan] длинная выгрузка заказов не вышла, беру утренний файл: {exc}", file=sys.stderr)
    return file_orders()


def rebuild_curves(data, today):
    """Доля двенадцати отрезков по каждому артикулу за 30 последних полных суток.

    Сегодняшний день не входит: он ещё не кончился. День последней записи
    в выгрузке тоже не входит, если эта запись раньше 23:00.
    Заказы – лента, вместе с отменёнными.
    """
    yesterday = (today - datetime.timedelta(days=1)).isoformat()
    if (data.get("curves_feed") == "tape" and data.get("curves_through") == yesterday
            and data.get("curves_min") == 100 and data.get("curves_bins") == 12
            and data.get("curves_n30") and data.get("curves")):
        return False
    raw = history_orders()
    latest = None
    parsed = []
    for row in raw:
        art = str(row.get("supplierArticle") or "").strip()
        dt = parse_dt(row.get("date"))
        if not art or not dt:
            continue
        parsed.append((art, dt))
        if latest is None or dt > latest:
            latest = dt
    if not latest:
        print("[plan] в заказах нет дат, таблицу долей не собрал", file=sys.stderr)
        return False
    last_full = latest.date()
    if latest.hour < 23:
        last_full -= datetime.timedelta(days=1)
    if last_full >= today:
        last_full = today - datetime.timedelta(days=1)
    if (data.get("curves_feed") == "tape" and data.get("curves_through") == last_full.isoformat()
            and data.get("curves_min") == 100 and data.get("curves_bins") == 12
            and data.get("curves_n30") and data.get("curves")):
        return False
    base_start = last_full - datetime.timedelta(days=29)
    by_art = {}
    shop = [0] * 12
    earliest = last_full
    for art, dt in parsed:
        day = dt.date()
        if day > last_full:
            continue
        earliest = min(earliest, day)
        slot = min(dt.hour // 2, 11)
        by_art.setdefault(art, {}).setdefault(day, [0] * 12)[slot] += 1
        if base_start <= day <= last_full:
            shop[slot] += 1
    curves = {}
    extended = 0
    short = 0
    for art, by_day in by_art.items():
        total = 0
        n30 = 0
        counts = [0] * 12
        used_days = 0
        day = last_full
        art_from = last_full
        while day >= earliest and (total < 100 or day >= base_start):
            bucket = by_day.get(day)
            if bucket:
                got = 0
                for i, n in enumerate(bucket):
                    counts[i] += n
                    total += n
                    got += n
                if base_start <= day <= last_full:
                    n30 += got
                used_days += 1
            art_from = day
            if total >= 100 and day < base_start:
                break
            day -= datetime.timedelta(days=1)
        if not total:
            continue
        if art_from < base_start:
            extended += 1
        if total < 100:
            short += 1
        curves[art] = {
            "n": total,
            "n30": n30,
            "days": used_days,
            "from": art_from.isoformat(),
            "through": last_full.isoformat(),
            "slots": [round(n / total, 4) for n in counts],
        }
    shop_total = sum(shop) or 1
    data["curves"] = curves
    data["shop_slots"] = [round(n / shop_total, 4) for n in shop]
    data["curves_through"] = last_full.isoformat()
    data["curves_from"] = base_start.isoformat()
    data["curves_min"] = 100
    data["curves_bins"] = 12
    data["curves_n30"] = True
    data["curves_feed"] = "tape"
    print(f"[plan] доли суток с {base_start.isoformat()} по {last_full.isoformat()}: {len(curves)} арт., длиннее 30 дней {extended}, меньше 100 заказов {short}")
    return True


def curve_min(data):
    """Сколько заказов за 30 полных суток нужно, чтобы брать карту артикула."""
    try:
        n = int(data.get("curve_min") or 100)
    except (TypeError, ValueError):
        n = 100
    return n if n >= 1 else 100


def orders_30(data, row):
    """Заказы артикула за последние 30 полных суток, не за удлинённое окно."""
    if isinstance(row, dict) and isinstance(row.get("n30"), (int, float)):
        return int(row["n30"])
    start = str((data or {}).get("curves_from") or "")
    frm = str((row or {}).get("from") or "")
    if frm and start and frm < start:
        return -1
    try:
        return int((row or {}).get("n") or 0)
    except (TypeError, ValueError):
        return 0


def shares_of(data, art):
    """Доля отрезков. Хватает заказов за 30 суток – карта артикула, иначе карта магазина."""
    row = (data.get("curves") or {}).get(art) or {}
    slots = row.get("slots") or []
    if orders_30(data, row) >= curve_min(data) and len(slots) == 12 and abs(sum(slots) - 1) < 0.05:
        return slots
    shop = data.get("shop_slots") or []
    if len(shop) == 12 and abs(sum(shop) - 1) < 0.05:
        return shop
    return [1 / 12] * 12


def progress_at(moment, shares):
    """Доля суток к текущей минуте МСК, вместе с незаконченным отрезком."""
    if len(shares) != 12:
        shares = [1 / 12] * 12
    hour = moment.hour + moment.minute / 60 + moment.second / 3600
    if hour >= 24:
        return 1.0, 11, "22–24", 1.0
    slot = min(int(hour // 2), 11)
    part = min(1.0, max(0.0, (hour - slot * 2) / 2))
    share = sum(shares[:slot]) + shares[slot] * part
    a, b = SLOTS[slot]
    return share, slot, f"{a}–{b}", part


def measurement(moment):
    """День и индекс последнего закрытого отрезка.

    0 – это 00–02, 11 – это 22–24. Замер в 02:00 закрывает первый отрезок,
    в полночь – весь вчерашний день. До 02:00 нового дня закрытого отрезка нет.
    Незакрытый отрезок в сумму не входит.
    """
    if moment.hour == 0 and moment.minute < 30:
        return moment.date() - datetime.timedelta(days=1), 11
    end_hour = (moment.hour // 2) * 2
    if end_hour < 2:
        return None
    return moment.date(), end_hour // 2 - 1


def due_plan(plan, shares, last):
    """Доля карты и план заказов с полуночи по закрытый отрезок включительно."""
    share = sum(shares[: last + 1])
    return share, plan * share


def bands_of(data):
    raw = data.get("bands")
    if not isinstance(raw, list) or not raw:
        return [{"from": a, "to": b, "dir": d, "pct": p} for a, b, d, p in BANDS]
    return sorted(raw, key=lambda b: b.get("from") or 0)


def band_for(pct, bands):
    for item in bands:
        if item["from"] <= pct < item["to"]:
            return item
    if bands and pct >= bands[-1]["from"]:
        return bands[-1]
    return bands[0] if bands else {"dir": "same", "pct": 0, "from": 0, "to": 0}


def enabled(data):
    """Живой прогон. Тестовые артикулы сюда не входят: цену им не подставляем."""
    out = {}
    for art, item in (data.get("articles") or {}).items():
        if isinstance(item, dict) and item.get("on") and item.get("per_day") and not item.get("test"):
            out[str(art)] = item
    return out


def avg_order_price(art, day):
    """Средняя цена заказа за сутки: до СПП, после скидки продавца. Отмены не считаем."""
    total = 0.0
    n = 0
    for row in file_orders():
        if row.get("isCancel"):
            continue
        if str(row.get("supplierArticle") or "").strip() != art:
            continue
        dt = parse_dt(row.get("date"))
        if not dt or dt.date() != day:
            continue
        price = row.get("priceWithDisc")
        if not price:
            continue
        total += float(price)
        n += 1
    if not n:
        return None
    return int(round(total / n))


def price_on(day, art):
    """Цена со скидкой продавца, до СПП, из утреннего снимка."""
    path = os.path.join(DIR, "price_history.json")
    try:
        hist = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(hist, dict):
        return None
    keys = sorted(k for k in hist if isinstance(hist.get(k), dict))
    if day in hist and art in hist[day]:
        return int(round(hist[day][art]))
    for key in reversed(keys):
        if key <= day and art in hist[key]:
            return int(round(hist[key][art]))
    for key in keys:
        if art in hist[key]:
            return int(round(hist[key][art]))
    return None


def orders_by_slot(art):
    """Артикул → день → 12 отрезков по ленте. Отменённые тоже считаются."""
    by_day = {}
    latest = None
    for row in file_orders():
        if str(row.get("supplierArticle") or "").strip() != art:
            dt = parse_dt(row.get("date"))
            if dt and (latest is None or dt > latest):
                latest = dt
            continue
        dt = parse_dt(row.get("date"))
        if not dt:
            continue
        if latest is None or dt > latest:
            latest = dt
        slot = min(dt.hour // 2, 11)
        by_day.setdefault(dt.date(), [0] * 12)[slot] += 1
    return by_day, latest


def replay_test(data, art, per_day, floor, since=None, today_slots=None):
    """Смены цены со дня включения стратегии. В кабинет не пишет.

    Дни до включения не считаем. В журнал попадает только новая цена.
    Первый день начинается со средней цены заказа за предыдущие сутки.
    Конец суток становится базой следующего дня.
    """
    by_day, latest = orders_by_slot(art)
    today = now_msk().date()
    if not latest and today_slots is None:
        return []
    if not latest:
        latest = datetime.datetime(today.year, today.month, today.day, tzinfo=MSK)
    last_day = latest.date()
    last_closed = latest.hour // 2 - 1
    if today_slots is not None and today >= last_day:
        now_closed = now_msk().hour // 2 - 1
        if today > last_day or now_closed > last_closed:
            last_day = today
            last_closed = now_closed
    if last_closed < 0:
        last_day -= datetime.timedelta(days=1)
        last_closed = 11
    start = None
    if since:
        try:
            start = datetime.date.fromisoformat(str(since)[:10])
        except ValueError:
            start = None
    if start is None or start > last_day:
        start = last_day
    window = []
    day = start
    while day <= last_day:
        window.append(day)
        day += datetime.timedelta(days=1)
    before = start - datetime.timedelta(days=1)
    base = avg_order_price(art, before) or price_on(start.isoformat(), art)
    if not base:
        return []
    shares = shares_of(data, art)
    bands = bands_of(data)
    plan = float(per_day)
    log = []
    shown = int(base)
    for day in window:
        closed = last_closed if day == last_day else 11
        if closed < 0:
            continue
        if today_slots is not None and day == now_msk().date():
            slots = list(today_slots)
        else:
            slots = list(by_day.get(day) or [0] * 12)
        day_base = int(base)
        for i in range(closed + 1):
            share, expected = due_plan(plan, shares, i)
            if expected <= 0:
                continue
            actual = sum(slots[: i + 1])
            raw, way, _step, _floor = quote_price(day_base, actual / expected * 100, bands, floor)
            if raw != shown:
                a, b = SLOTS[i]
                log.append({
                    "day": day.isoformat(),
                    "slot": f"{a}–{b}",
                    "base": int(day_base),
                    "price": int(raw),
                    "dir": way,
                    "step": round((raw / day_base - 1) * 100, 1) if day_base else None,
                    "fill": round(actual / expected * 100, 1),
                })
                shown = int(raw)
        base = shown
    return log[-16:]


def working_base(data, art, day):
    """Цена, от которой идёт день: фиксация 23:50, иначе средняя цена заказа за вчера."""
    anchor = data.setdefault("anchor", {"date": "", "prices": {}})
    prices = anchor.get("prices") if isinstance(anchor.get("prices"), dict) else {}
    today = day.isoformat()
    if anchor.get("date") == today and prices.get(art):
        return int(prices[art]), "фиксация 23:50"
    if (anchor.get("date") or "") < today:
        anchor["date"] = today
        anchor["prices"] = {}
        prices = anchor["prices"]
    yesterday = day - datetime.timedelta(days=1)
    base = avg_order_price(art, yesterday) or price_on(today, art)
    if base and anchor.get("date") == today and art not in prices:
        prices[art] = int(base)
    return (int(base) if base else None), "средняя за вчера"


def quote_now(data, art, item, moment, buckets):
    """Процент плана на текущую минуту МСК. Двухчасовой границы не ждёт."""
    base, src = working_base(data, art, moment.date())
    share, _slot, label, _part = progress_at(moment, shares_of(data, art))
    actual = sum((buckets or {}).get(art) or [])
    plan = float(item.get("per_day") or 0)
    expected = plan * share
    floor = 0
    try:
        floor = int(round(float(item.get("min_price") or 0)))
    except (TypeError, ValueError):
        floor = 0
    snap = {
        "at": moment.strftime("%Y-%m-%d %H:%M"),
        "day": moment.date().isoformat(),
        "slot": moment.strftime("%H:%M"),
        "segment": label,
        "actual": actual,
        "plan": plan,
        "share": round(share, 4),
        "expected": round(expected, 2),
        "fill": None,
        "base": base,
        "base_src": src,
        "price": base,
    }
    if base and expected > 0:
        pct = actual / expected * 100
        raw, way, step, at_floor = quote_price(base, pct, bands_of(data), floor)
        snap.update({
            "fill": round(pct, 1),
            "dir": way,
            "pct": step,
            "price": raw,
            "at_floor": at_floor,
        })
        if raw != base:
            log = item.setdefault("test_log", [])
            last = log[-1].get("price") if log else None
            mark = moment.strftime("%H:%M")
            if last != raw:
                log.append({
                    "day": moment.date().isoformat(),
                    "slot": mark,
                    "segment": label,
                    "base": int(base),
                    "price": int(raw),
                    "dir": way,
                    "step": round((raw / base - 1) * 100, 1) if base else step,
                    "fill": round(pct, 1),
                })
                item["test_log"] = log[-16:]
    item["now"] = snap
    return snap


def run_test(arts):
    data = load()
    moment = now_msk()
    articles = data.setdefault("articles", {})
    targets = arts or [a for a, item in articles.items() if isinstance(item, dict) and item.get("on") and item.get("test")]
    buckets = {}
    live = None
    if any(targets):
        try:
            buckets, _ = orders_today(moment.date())
            live = buckets
        except Exception as exc:
            print(f"[plan] лента заказов на сейчас не пришла, беру файл: {exc}", file=sys.stderr)
            buckets = {}
    for art in targets:
        item = articles.get(art) if isinstance(articles.get(art), dict) else None
        if not item or not item.get("on"):
            continue
        item["test"] = True
        if not item.get("since"):
            item["since"] = moment.date().isoformat()
        tape = list(live.get(art) or [0] * 12) if live is not None else None
        item["test_log"] = replay_test(data, art, item.get("per_day") or 0, item.get("min_price") or 0, item.get("since"), tape)
        snap = quote_now(data, art, item, moment, buckets)
        item["test_at"] = moment.strftime("%Y-%m-%d %H:%M")
        fill = snap.get("fill")
        print(f"[plan] тест {art}: база {snap.get('base')} ₽ ({snap.get('base_src')}), сейчас план {fill if fill is not None else '—'}%, цена {snap.get('price')} ₽")
    save(data)


def quote_price(base, fill_pct, bands, floor):
    """Цена со скидкой продавца, до СПП, от утренней базы и процента плана."""
    band = band_for(fill_pct, bands)
    sign = {"down": -1, "up": 1}.get(band.get("dir"), 0)
    raw = max(1, round(base * (1 + sign * float(band.get("pct") or 0) / 100)))
    floor = int(floor or 0)
    at_floor = False
    way = band.get("dir") or "same"
    step = band.get("pct") or 0
    if floor and sign < 0 and raw < floor:
        at_floor = True
        raw = base if floor >= base else floor
        if raw >= base:
            way, step = "same", 0
        else:
            way = "down"
            step = round((base - raw) / base * 100, 2)
    return raw, way, step, at_floor


def decide(data, moment, day, last, buckets, prices):
    """Процент выполнения – факт с полуночи ÷ план, накопленный по закрытым отрезкам."""
    anchor = data.setdefault("anchor", {"date": "", "prices": {}})
    if not isinstance(anchor.get("prices"), dict):
        anchor["prices"] = {}
    live = {}
    a, b = SLOTS[last]
    for art, item in enabled(data).items():
        plan = float(item["per_day"])
        slots = list(buckets.get(art) or [0] * 12)
        if len(slots) < 12:
            slots = slots + [0] * (12 - len(slots))
        data.setdefault("today", {})[art] = slots
        sh = shares_of(data, art)
        share, _slot, label, _part = progress_at(moment, sh)
        a, b = label.split("–") if "–" in label else (a, b)
        expected = plan * share
        actual = sum(slots)
        base, _src = working_base(data, art, day)
        if not base:
            base = prices.get(art)
        if base:
            anchor["prices"].setdefault(art, base)
        row = {
            "at": moment.strftime("%Y-%m-%d %H:%M"),
            "day": day.isoformat(),
            "slot": last,
            "slot_label": f"{a}–{b}",
            "actual": actual,
            "plan": plan,
            "share": round(share, 4),
            "expected": round(expected, 2),
            "dir": "",
            "price": base,
            "base": base,
        }
        if not base or expected <= 0:
            live[art] = row
            continue
        pct = actual / expected * 100
        floor = 0
        try:
            floor = int(round(float(item.get("min_price") or 0)))
        except (TypeError, ValueError):
            floor = 0
        raw, way, step, at_floor = quote_price(base, pct, bands_of(data), floor)
        row.update({
            "fill": round(pct, 1),
            "dir": way,
            "pct": step,
            "price": raw,
            "min_price": floor or None,
            "at_floor": at_floor,
        })
        live[art] = row
    data["live"] = live
    data["today_date"] = day.isoformat()
    return data


def remember_day(data, day, buckets):
    samples = data.setdefault("samples", {})
    for art in enabled(data):
        slots = buckets.get(art) or (data.get("today") or {}).get(art)
        if not slots or not sum(slots):
            continue
        hist = [row for row in samples.setdefault(art, []) if isinstance(row, list)]
        hist.append(slots)
        samples[art] = hist[-28:]


def patch_pages(data):
    live = data.get("live") or {}
    arts = enabled(data)
    for name in ("page.html", os.path.join("op", "page.html")):
        path = os.path.join(DIR, name)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r"^const D=(\{.*\});\s*$", src, re.M)
        if not m:
            print(f"[plan] нет const D в {name}", file=sys.stderr)
            continue
        page = json.loads(m.group(1))
        changed = False
        for row in page.get("rows") or []:
            art = str(row.get("a"))
            if art not in arts:
                if row.get("plan_orders"):
                    row.pop("plan_orders", None)
                    changed = True
                continue
            info = live.get(art) or {}
            plan = arts[art]["per_day"]
            base = info.get("base")
            price = info.get("price")
            cur = row.get("p")
            row["plan_orders"] = {
                "per_day": plan,
                "min_price": arts[art].get("min_price"),
                "actual": info.get("actual"),
                "expected": info.get("expected"),
                "fill": info.get("fill"),
                "slot": info.get("slot_label"),
                "base": base,
                "at": info.get("at"),
            }
            if price and cur and price != cur:
                kind = "up" if price > cur else "back"
                verb = "Поднять" if price > cur else "Снизить"
                row["kind"] = kind
                row["new"] = price
                row["act"] = f"{verb} до {price:,} ₽".replace(",", " ")
                disc = row.get("disc")
                if isinstance(disc, (int, float)) and disc < 100:
                    row["nfull"] = round(price / (1 - disc / 100))
            else:
                row["kind"] = "hold"
                row["new"] = cur
                row["act"] = "Держать"
                row["nfull"] = None
            fill = info.get("fill")
            expected = info.get("expected")
            move = {"down": "опускаем", "up": "поднимаем"}.get(info.get("dir"), "оставляем")
            step = "" if info.get("dir") in ("", "same") else f" на {info.get('pct') or 0:g}%"
            if fill is None and not expected:
                row["note"] = "План заказов: по карте к этому часу заказов не ждут, цену не меняем. Правила оборачиваемости не действуют."
            elif fill is None:
                row["note"] = "План заказов: цены вчерашнего 23:50 ещё нет, цену не меняем. Правила оборачиваемости не действуют."
            else:
                base_txt = f"от цены {base:g} ₽, зафиксированной вчера в 23:50"
                floor_txt = ""
                if info.get("at_floor") and info.get("min_price"):
                    floor_txt = f" Ниже {info['min_price']:g} ₽ не опускаем."
                row["note"] = (
                    f"План заказов: отрезок {info.get('slot_label') or ''}, с полуночи {info.get('actual') or 0:g} заказов "
                    f"при плане {expected:g} к этому часу ({fill:g}%). "
                    f"Цену {move}{step} {base_txt}.{floor_txt} Правила оборачиваемости не действуют."
                )
            changed = True
        if not changed:
            continue
        blob = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        src = src[: m.start(1)] + blob + src[m.end(1) :]
        open(path, "w", encoding="utf-8").write(src)
        print(f"[plan] обновлена {name}")


def inject_heat(data):
    heat = {
        "from": data.get("curves_from") or "",
        "through": data.get("curves_through") or "",
        "shop": data.get("shop_slots") or [],
        "by": data.get("curves") or {},
    }
    for name in ("page.html", os.path.join("op", "page.html")):
        path = os.path.join(DIR, name)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        m = re.search(r"^const D=(\{.*\});\s*$", src, re.M)
        if not m:
            continue
        page = json.loads(m.group(1))
        page["heat"] = heat
        blob = json.dumps(page, ensure_ascii=False, separators=(",", ":"))
        open(path, "w", encoding="utf-8").write(src[: m.start(1)] + blob + src[m.end(1) :])


def publish():
    env = os.environ.copy()
    env["TZ"] = "Europe/Moscow"
    page_name = env.get("PAGE_NAME") or "index.html"
    subprocess.run(
        ["python3", "publish.py", "page.html", page_name],
        cwd=DIR, env=env, check=False,
    )
    env2 = env.copy()
    env2["SITE_DIR"] = "/var/www/wb-report/wb"
    env2["PAGE_NAME"] = "wb-op.html"
    subprocess.run(
        ["python3", os.path.join(DIR, "publish.py"), "page.html", "wb-op.html"],
        cwd=os.path.join(DIR, "op"), env=env2, check=False,
    )
    subprocess.run(
        ["python3", "/opt/mp-index/add_nav.py",
         "/var/www/wb-report/wb/wb-op.html", "wb", "--after", "<body style=\"margin:0\">"],
        check=False,
    )


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    moment = now_msk()
    data = load()
    if rebuild_curves(data, moment.date()) or cmd == "curves":
        save(data)
        inject_heat(data)
    if cmd == "curves":
        publish()
        print(f"[plan] таблица долей по {data.get('curves_through')}: {len(data.get('curves') or {})} арт.")
        return
    if cmd == "test":
        run_test(sys.argv[2:])
        return
    if not enabled(data) and cmd == "check":
        print("[plan] нет артикулов на стратегии")
        return
    point = measurement(moment)
    if cmd == "check" and point is None:
        print("[plan] за сегодня ещё не закрыт ни один отрезок, цену не меняю")
        return
    day = point[0] if cmd == "check" else moment.date()
    buckets, _ = orders_today(day)
    prices = cabinet_prices()
    if cmd == "anchor":
        run_test([])
        data = load()
        remember_day(data, moment.date(), buckets)
        today = moment.date().isoformat()
        fixed = {}
        for art, item in (data.get("articles") or {}).items():
            if not isinstance(item, dict) or not item.get("on"):
                continue
            price = None
            now = item.get("now") or {}
            if str(now.get("at") or "").startswith(today) and now.get("price"):
                price = int(now["price"])
            if price is None:
                for row in item.get("test_log") or []:
                    if row.get("day") == today and row.get("price"):
                        price = int(row["price"])
            if price is None:
                live = (data.get("live") or {}).get(art) or {}
                if live.get("price"):
                    price = int(live["price"])
            if price is None and art in prices:
                price = prices[art]
            if price:
                fixed[str(art)] = price
        data["anchor"] = {
            "date": (moment.date() + datetime.timedelta(days=1)).isoformat(),
            "prices": fixed,
        }
        save(data)
        print(f"[plan] цены на {data['anchor']['date']} записаны, {len(fixed)} арт.")
        return
    data = decide(data, moment, point[0], point[1], buckets, prices)
    save(data)
    patch_pages(data)
    publish()
    a, b = SLOTS[point[1]]
    print(f"[plan] проверка {moment.strftime('%H:%M')} МСК, отрезок {a}–{b} суток {point[0].isoformat()}, артикулов {len(data.get('live') or {})}")


if __name__ == "__main__":
    main()
