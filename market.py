#!/usr/bin/env python3
"""Монитор рынка: ежедневное обновление market.json (графики «Рынок и наша видимость»).

В комплекте владельца market.json заполнялся вручную. Здесь он дополняется каждое утро за вчера:
  core    — сумма частотности WB по ядру запросов (market.json → core_queries). Берётся из состояния
            панели по товарым на этом же сервере (wb-panel-state.json → qdaily: панель ежедневно
            в 05:00 МСК качает отчёт «Поисковые запросы» по дням; проверено — совпадает с ручными
            значениями market.json с точностью до правок WB задним числом).
  open    — показы наших карточек в поиске за день, vis — видимость %, pos_avg / pos_med — средняя
            и медианная позиция: seller-analytics /api/v2/search-report/report по списку артикулов
            (visibilityInfo.byDay и positionInfo.chartItems), positionCluster=all. Список nmID
            хранится в market.json → nm_ids (первый раз берётся из page.html), чтобы серия была
            однородной; окно 30 дней пересчитывается целиком, WB правит цифры задним числом.
Старые значения не теряются: дни без новых данных остаются как были. Хранится 120 дней.
Запуск: python3 market.py [--days 30] [--page page.html]   (в run.sh — до движка)
"""
import json, os, sys, re, argparse, subprocess, datetime

DIR = os.path.dirname(os.path.abspath(__file__))
MARKET = os.path.join(DIR, 'market.json')
PANEL_STATE = os.environ.get('MARKET_PANEL_STATE', '/opt/wb-panel/wb-panel-state.json')


def log(m): print(f'[market] {m}', file=sys.stderr)


def today_msk():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Europe/Moscow')).date()
    except Exception:
        return datetime.date.today()


def post(url, token, body, tries=3, pause=20):
    last = None
    for _ in range(tries):
        r = subprocess.run(['curl', '-sS', '-X', 'POST', url, '-H', f'Authorization: {token}', '-H', 'Content-Type: application/json',
                            '-d', json.dumps(body), '--max-time', '120', '-w', '\n%{http_code}'], capture_output=True, text=True)
        out, _, code = r.stdout.rpartition('\n')
        if code == '200':
            try: return json.loads(out)
            except Exception: last = 'bad json'
        else: last = f'{code} {out[:160]}'
        import time; time.sleep(pause)
    raise RuntimeError(f'{url.split("/api/")[-1]}: {last}')


def nm_ids_from_page(page):
    try:
        src = open(page, encoding='utf-8').read()
        m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
        return sorted({int(r['nm']) for r in json.loads(m.group(1))['rows'] if r.get('nm')})
    except Exception as e:
        log(f'nmID из страницы не прочитаны: {e}'); return []


def core_from_panel(queries, days):
    """core — сумма частотности ядра запросов; mkt — сумма частотности всех запросов со словом «товарыь»
    (dem.market панели, ~630 запросов) — это спрос рынка, на него не влияют наши цены, реклама и склейки;
    mkt_all — сумма по 535 отслеживаемым запросам панели (dem.all)."""
    try:
        ps = json.load(open(PANEL_STATE, encoding='utf-8')); qd = ps.get('qdaily', {}); dem = {r['date']: r for r in ps.get('dem', [])}
    except Exception as e:
        log(f'состояние панели не прочитано ({PANEL_STATE}): {e}'); return {}, {}, {}
    out = {}; mkt = {}; mkt_all = {}
    for d in days:
        vals = [qd.get(q, {}).get(d) for q in queries]
        if any(v is not None for v in vals): out[d] = sum(v or 0 for v in vals)
        if d in dem:
            if dem[d].get('market') is not None: mkt[d] = dem[d]['market']
            if dem[d].get('all') is not None: mkt_all[d] = dem[d]['all']
    return out, mkt, mkt_all


def visibility(token, nm_ids, start, end):
    """{день: {'open','vis','pos_avg','pos_med'}} по всем nm сразу."""
    span = (end - start).days + 1
    body = {"currentPeriod": {"start": start.isoformat(), "end": end.isoformat()},
            "pastPeriod": {"start": (start - datetime.timedelta(days=span)).isoformat(), "end": (start - datetime.timedelta(days=1)).isoformat()},
            "nmIds": nm_ids, "topOrderBy": "openCard", "orderBy": {"field": "avgPosition", "mode": "asc"},
            "positionCluster": "all", "includeSubstitutedSKUs": False, "includeSearchTexts": True, "limit": 1000, "offset": 0}
    j = post('https://seller-analytics-api.wildberries.ru/api/v2/search-report/report', token, body)
    d = j.get('data', {}); out = {}
    for x in d.get('visibilityInfo', {}).get('byDay', []) or []:
        out.setdefault(x['dt'], {})['open'] = x.get('open'); out[x['dt']]['vis'] = x.get('visibility')
    for x in d.get('positionInfo', {}).get('chartItems', []) or []:
        out.setdefault(x['dt'], {})['pos_avg'] = x.get('average'); out[x['dt']]['pos_med'] = x.get('median')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=50); ap.add_argument('--page', default=os.path.join(DIR, 'page.html'))
    ap.add_argument('--token', default=os.environ.get('WB_TOKEN', '')); ap.add_argument('--no-api', action='store_true')
    A = ap.parse_args()
    m = json.load(open(MARKET, encoding='utf-8')) if os.path.exists(MARKET) else {'days': [], 'core': [], 'open': [], 'vis': [], 'pos_avg': [], 'pos_med': []}
    KEYS = ('core', 'mkt', 'mkt_all', 'open', 'vis', 'pos_avg', 'pos_med')
    for k in KEYS: m.setdefault(k, [])
    rows = {d: {k: (m[k][i] if i < len(m[k]) else None) for k in KEYS} for i, d in enumerate(m.get('days', []))}
    last = today_msk() - datetime.timedelta(days=1); start = last - datetime.timedelta(days=A.days - 1)
    days = [(start + datetime.timedelta(days=i)).isoformat() for i in range(A.days)]
    # 1. частотность — из панели: ядро, весь рынок «товарыь», отслеживаемые запросы
    core, mkt, mkt_all = core_from_panel(m.get('core_queries') or [], days)
    for d, v in core.items(): rows.setdefault(d, {})['core'] = v
    for d, v in mkt.items(): rows.setdefault(d, {})['mkt'] = v
    for d, v in mkt_all.items(): rows.setdefault(d, {})['mkt_all'] = v
    # 2. показы / видимость / позиция — из API
    nm_ids = m.get('nm_ids') or nm_ids_from_page(A.page)
    got = {}
    if not A.no_api and nm_ids and A.token:
        try:
            got = visibility(A.token, nm_ids, start, last)
            m['nm_ids'] = nm_ids
        except Exception as e:
            log(f'видимость не получена: {e} — старые значения сохранены')
    for d, v in got.items():
        r = rows.setdefault(d, {})
        for k in ('open', 'vis', 'pos_avg', 'pos_med'):
            if v.get(k) is not None: r[k] = v[k]
    # 3. собрать обратно, 120 дней
    all_days = sorted(rows)[-120:]
    m['days'] = all_days
    for k in KEYS:
        m[k] = [rows[d].get(k) for d in all_days]
    m['updated'] = today_msk().isoformat()
    tmp = MARKET + '.tmp'; json.dump(m, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, MARKET)
    have_core = [d for d in days if rows.get(d, {}).get('core') is not None]; have_vis = [d for d in days if rows.get(d, {}).get('open') is not None]
    have_mkt = [d for d in days if rows.get(d, {}).get('mkt') is not None]
    print(f"[market] дней {len(all_days)} до {all_days[-1] if all_days else '—'}; частотность ядра по {len(have_core)} из {A.days} дней окна (до {have_core[-1] if have_core else '—'}), рынок «товарыь» по {len(have_mkt)} (до {have_mkt[-1] if have_mkt else '—'}), показы/позиция по {len(have_vis)} (до {have_vis[-1] if have_vis else '—'}), артикулов в сводке {len(nm_ids)}")


if __name__ == '__main__':
    main()
