#!/usr/bin/env python3
"""Локомотивы и акции WB: данные для правила «локомотив не выходит из всех акций малым шагом».

Что собирает (`promos.py fetch`, идёт до движка, пишет promos.json):
  1. Реклама — /adv/v3/fullstats по всем активным и приостановленным кампаниям за 14 дней:
     расход, заказы, показы по каждому артикулу. Локомотив = артикул, на который уходит
     не меньше LOCO_MIN_SHARE (plan.json → loco_min_share, по умолчанию 2%) рекламного расхода,
     плюс plan.json → loco_add, минус loco_remove. Контрольная группа и исключённые — не локомотивы.
  2. Акции — календарь (/api/v1/calendar/promotions) и детали каждой текущей/будущей акции:
     даты, бустинг по доле участия (ranging), сколько наших товаров участвует.
  3. Проходные цены по артикулам. Через API их для автоакций НЕ получить (метод
     /calendar/promotions/nomenclatures — «Not applicable for auto promos», а все текущие акции
     автоматические). Источники: выгрузки из кабинета (Календарь акций → акция → список товаров,
     xlsx) в папке promo-reports/ и/или ручной файл promo_prices.json:
        {"101": {"2907": 2600, "2873": 2650}}   — цена со скидкой продавца, выше которой артикул
                                                  выпадает из этой акции; ключ акции — её ID
        {"308": 2600}                            — одна проходная для всех акций
     Итог — cap[артикул] = максимальная проходная среди действующих сейчас акций: пока цена
     не выше неё, артикул остаётся хотя бы в одной акции.

`promos.py inject --page page.html` — после движка: вставляет D.promos (сводка для карточки
на странице) и loco/ad/cap по строкам, если движок их не проставил (страница старого шаблона).

Движок (wb_daily.py --promos promos.json) использует loco и cap в ветке «Поднять».
"""
import json, os, sys, re, time, datetime, argparse, subprocess, urllib.parse, glob

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'promos.json')
REPORTS = os.path.join(DIR, 'promo-reports')
MANUAL = os.path.join(DIR, 'promo_prices.json')
HIST_FILE = os.path.join(DIR, 'price_history.json')
STATUS_FILE = os.path.join(DIR, 'promo_status.json')     # журнал «в акции / вне акций» по дням
PLAN = os.path.join(DIR, 'plan.json')
AD_DAYS = 14


def today_msk():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Europe/Moscow')).date()
    except Exception:
        return datetime.date.today()


def log(m): print(f'[promos] {m}', file=sys.stderr)


def _load(path, default):
    if not os.path.exists(path): return default
    with open(path, encoding='utf-8') as f: return json.load(f)


def _save(path, obj, **kw):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(obj, f, ensure_ascii=False, **kw)
    os.replace(tmp, path)


def api(url, params=None, token=None, tries=3, pause=25):
    if params: url = url + '?' + urllib.parse.urlencode(params, safe=',')
    cmd = ['curl', '-sS', url, '-H', f'Authorization: {token}', '--max-time', '120', '-w', '\n%{http_code}']
    last = None
    for i in range(tries):
        r = subprocess.run(cmd, capture_output=True, text=True)
        out, _, code = r.stdout.rpartition('\n')
        if code == '204': return []
        if code == '200':
            try: return json.loads(out)
            except Exception: last = 'bad json'
        else:
            last = f'{code} {out[:120]}'
        time.sleep(pause)
    raise RuntimeError(f'{url.split("?")[0]}: {last}')


# ---------------------------------------------------------------- реклама
def goods_map(tok):
    """{nmID: артикул продавца} по всему кабинету."""
    out = {}; off = 0
    while True:
        j = api('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter', {'limit': 1000, 'offset': off}, tok)
        g = j['data']['listGoods']
        for it in g: out[it['nmID']] = str(it['vendorCode'])
        if len(g) < 1000: break
        off += 1000
    return out


def ad_spend(tok, days=AD_DAYS):
    j = api('https://advert-api.wildberries.ru/adv/v1/promotion/count', None, tok)
    ids = [a['advertId'] for g in (j.get('adverts') or []) if g.get('status') in (9, 11) for a in g.get('advert_list', [])]
    t = today_msk(); d1 = (t - datetime.timedelta(days=days)).isoformat(); d2 = (t - datetime.timedelta(days=1)).isoformat()
    by = {}
    for i in range(0, len(ids), 50):
        if i: time.sleep(21)                       # лимит fullstats: 3 запроса в минуту
        st = api('https://advert-api.wildberries.ru/adv/v3/fullstats',
                 {'ids': ','.join(map(str, ids[i:i+50])), 'beginDate': d1, 'endDate': d2}, tok, tries=4, pause=30)
        for camp in st or []:
            for day in camp.get('days', []):
                for app in day.get('apps', []):
                    for nm in app.get('nms', []):
                        k = nm.get('nmId'); b = by.setdefault(k, {'spend': 0.0, 'orders': 0, 'views': 0, 'camps': set()})
                        b['spend'] += nm.get('sum', 0) or 0; b['orders'] += nm.get('orders', 0) or 0
                        b['views'] += nm.get('views', 0) or 0; b['camps'].add(camp.get('advertId'))
    for b in by.values(): b['camps'] = len(b['camps'])
    return {'from': d1, 'to': d2, 'campaigns': len(ids), 'by_nm': by}


# ---------------------------------------------------------------- акции
def promotions(tok):
    t = today_msk()
    s = (t - datetime.timedelta(days=30)).strftime('%Y-%m-%dT00:00:00Z'); e = (t + datetime.timedelta(days=60)).strftime('%Y-%m-%dT00:00:00Z')
    j = api('https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions',
            {'startDateTime': s, 'endDateTime': e, 'allPromo': 'true', 'limit': 300, 'offset': 0}, tok)
    out = []
    for p in j.get('data', {}).get('promotions', []):
        end = p['endDateTime'][:10]; start = p['startDateTime'][:10]
        if end < t.isoformat(): continue
        d = {}
        try:
            time.sleep(0.7)
            dj = api('https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions/details', {'promotionIDs': p['id']}, tok, tries=2, pause=3)
            lst = dj.get('data', {}).get('promotions', [])
            if lst: d = lst[0]
        except Exception as ex:
            log(f'детали акции {p["id"]}: {ex}')
        # WB отдаёт границы в UTC (…T21:00:00Z = 00:00 МСК следующего дня)
        st_msk = (datetime.datetime.fromisoformat(p['startDateTime'].replace('Z', '+00:00')) + datetime.timedelta(hours=3)).date().isoformat()
        en_msk = (datetime.datetime.fromisoformat(p['endDateTime'].replace('Z', '+00:00')) + datetime.timedelta(hours=3)).date().isoformat()
        out.append({'id': p['id'], 'name': p['name'], 'type': p.get('type'), 'start': st_msk, 'end': en_msk,
                    'active': st_msk <= t.isoformat() <= en_msk, 'upcoming': st_msk > t.isoformat(),
                    'ranging': [{'rate': r.get('participationRate'), 'boost': r.get('boost')} for r in d.get('ranging', [])],
                    'participation': d.get('participationPercentage'), 'in_total': d.get('inPromoActionTotal'),
                    'notin_total': d.get('notInPromoActionTotal'), 'advantages': d.get('advantages', [])})
    out.sort(key=lambda x: (not x['active'], x['start']))
    return out


# ---------------------------------------------------------------- проходные цены
def _num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).replace('\xa0', '').replace(' ', '').replace(',', '.')
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group(0)) if m else None


def _norm(s):
    import unicodedata   # имена файлов с Mac приходят в NFD («й» = «и» + знак) – приводим к NFC
    return re.sub(r'[^0-9a-zа-яё]+', '', unicodedata.normalize('NFC', str(s)).lower().replace('ё', 'е'))


def _promo_key(base, promos):
    """Какой акции принадлежит файл: ID в имени (promo-2873-…), иначе название акции из имени
    выгрузки кабинета («…для акции_<название>_14.09.2026 19.20.31.xlsx») ищется среди акций календаря."""
    m = re.search(r'(?<!\d)(2\d{3})(?!\d)', base.split('_')[0]) or re.match(r'\D*(\d{3,5})\D', base)
    ids = {str(p['id']) for p in promos}
    if m and m.group(1) in ids: return m.group(1)
    nm = re.sub(r'_\d{2}\.\d{2}\.\d{4}.*$', '', base)          # убрать дату и время
    nm = re.sub(r'^.*акци[юи]_', '', nm)                             # «Все товары подходящие для акции_»
    nn = _norm(nm)
    best = None
    for p in promos:
        pn = _norm(p['name'])
        if nn and (nn == pn or nn in pn or pn in nn):
            if best is None or len(pn) > len(_norm(best['name'])): best = p
    return str(best['id']) if best else None


def caps_from_reports(nm2art, promos):
    """Выгрузки кабинета (xlsx) из promo-reports/: {арт: {ID акции: {'cap','in','src'}}}.
    Колонки выгрузки WB: «Артикул поставщика», «Артикул WB», «Плановая цена для акции» (проходная —
    цена со скидкой, при которой товар в акции), «Товар уже участвует в акции», «Статус»."""
    caps = {}; notes = []
    files = sorted(glob.glob(os.path.join(REPORTS, '*.xlsx')), key=os.path.getmtime)
    if not files: return caps, notes
    try:
        import openpyxl
    except ImportError:
        notes.append('openpyxl не установлен — выгрузки promo-reports не прочитаны'); return caps, notes
    for f in files:
        base = os.path.basename(f)
        pkey = _promo_key(base, promos)
        if not pkey:
            notes.append(f'{base}: не понял, к какой акции относится файл (нет ID и название не совпало с календарём) — пропущен'); continue
        try:
            wb = openpyxl.load_workbook(f, data_only=True)      # без read_only: выгрузки WB не хранят размер листа
        except Exception as ex:
            notes.append(f'{base}: не открылся ({ex})'); continue
        got = 0
        for ws in wb.worksheets:
            hdr = None; cols = {}
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip().lower() if c is not None else '' for c in row]
                if hdr is None:
                    if any('артикул' in c for c in cells) and any('цен' in c for c in cells):
                        hdr = cells
                        for i, c in enumerate(cells):
                            if ('артикул продавца' in c or 'артикул поставщика' in c) and 'art' not in cols: cols['art'] = i
                            elif ('артикул wb' in c or 'артикул вб' in c or 'номенклатур' in c or c == 'артикул') and 'nm' not in cols: cols['nm'] = i
                            elif 'цен' in c and ('акци' in c or 'участ' in c or 'план' in c) and 'cap' not in cols: cols['cap'] = i
                            elif ('участв' in c or 'в акции' in c) and 'цен' not in c and 'скидк' not in c and 'in' not in cols: cols['in'] = i
                        if 'cap' not in cols or ('art' not in cols and 'nm' not in cols):
                            notes.append(f'{base}/{ws.title}: не узнал колонки ({[c for c in cells if c][:8]})'); hdr = None; break
                    continue
                art = None
                if 'art' in cols and row[cols['art']] not in (None, ''): art = str(row[cols['art']]).strip()
                elif 'nm' in cols:
                    nm = _num(row[cols['nm']]); art = nm2art.get(int(nm)) if nm else None
                cap = _num(row[cols['cap']]) if cols.get('cap') is not None else None
                if not art or not cap: continue
                inv = None
                if 'in' in cols and row[cols['in']] is not None:
                    v = str(row[cols['in']]).strip().lower(); inv = v in ('да', 'yes', 'true', '1', 'участвует')
                caps.setdefault(art, {})[pkey] = {'cap': round(cap), 'in': inv, 'src': base}; got += 1
        notes.append(f'{base[:60]}: акция {pkey}, строк {got}' if got else f'{base}: строк с плановой ценой не найдено')
    return caps, notes


def caps_manual():
    if not os.path.exists(MANUAL): return {}
    j = json.load(open(MANUAL, encoding='utf-8'))
    out = {}
    for a, v in j.items():
        if str(a).startswith('_'): continue
        if isinstance(v, dict):
            out[str(a)] = {str(k): {'cap': round(float(x)), 'in': None, 'src': 'promo_prices.json'} for k, x in v.items() if x}
        elif v:
            out[str(a)] = {'*': {'cap': round(float(v)), 'in': None, 'src': 'promo_prices.json'}}
    return out


# ---------------------------------------------------------------- статус «в акции» по дням
def promo_status(caps_all, promos, today, uncovered=()):
    """По каждому артикулу: в акции ли он сейчас и с какого дня так. Статус дня = цена дня
    (снимок кабинета из price_history.json) не выше проходной хотя бы одной акции, идущей в тот
    день. Дни без известной проходной не оцениваются. Журнал promo_status.json хранит статусы,
    посчитанные в прошлые прогоны (проходные тех дней), и имеет приоритет над пересчётом."""
    hist = _load(HIST_FILE, {}) if os.path.exists(HIST_FILE) else {}
    journal = _load(STATUS_FILE, {}) if os.path.exists(STATUS_FILE) else {}
    pid2p = {str(p['id']): p for p in promos}
    unc = [pid2p[str(i)] for i in uncovered if str(i) in pid2p]      # идущие акции без выгрузки: по ним статус неизвестен
    days = sorted(d for d in hist if d <= today)[-90:]
    out = {}
    for a, d in caps_all.items():
        tl = {}; unk = {}
        for day in days:
            price = hist.get(day, {}).get(a)
            if price is None: continue
            cap = None
            for key, v in d.items():
                p = pid2p.get(key)
                if key == '*' or (p and p['start'] <= day <= p['end']):
                    cap = v['cap'] if cap is None else max(cap, v['cap'])
            if cap is None: continue
            tl[day] = price <= cap * 1.003
            u = [p['name'] for p in unc if p['start'] <= day <= p['end']]
            if not tl[day] and u: tl[day] = None; unk[day] = u          # ниже известных проходных, но есть акция без данных
        # журнал уточняет только известные дни: «в акции» из журнала надёжно (тогда проходная была видна),
        # «вне акций» принимаем лишь там, где и пересчёт дал определённый ответ (нет акций без выгрузки)
        for day, m in journal.items():
            if a in m and day <= today and day in tl:
                if m[a]: tl[day] = True
                elif tl[day] is not None: tl[day] = False
        if not tl: continue
        ds = sorted(tl); last = ds[-1]; cur = tl[last]
        since = None; known_from = ds[0]
        for day in reversed(ds):
            if tl[day] != cur: break
            since = day
        if since == known_from: since = None                  # весь известный период в одном статусе
        out[a] = {'in': cur, 'since': since, 'known_from': known_from, 'as_of': last, 'unknown': unk.get(last, [])}
        if cur is not None: journal.setdefault(last, {})[a] = cur
    for day in [x for x in journal if x < (datetime.date.fromisoformat(today) - datetime.timedelta(days=120)).isoformat()]:
        journal.pop(day, None)
    _save(STATUS_FILE, journal)
    return out


def ad_campaigns(tok, nm2art):
    """Рекламные кампании по артикулам: {артикул: [{'t': 'АРК'|'Поиск'|'Реком', 'on': True/False, 'id', 'name', 'bid'}]}.
    Все кампании WB теперь тип 9: «Единая ставка» (bid_type unified, поиск + рекомендации) – это АРК,
    «Ручная ставка» с местом «поиск» – Поиск, только «рекомендации» – Реком. Берём идущие (статус 9) и на паузе (11)."""
    c = api('https://advert-api.wildberries.ru/adv/v1/promotion/count', None, tok, tries=2, pause=5)
    ids = [a['advertId'] for g in (c or {}).get('adverts', []) if g.get('status') in (9, 11) for a in g.get('advert_list', [])]
    out = {}
    for i in range(0, len(ids), 50):
        if i: time.sleep(1)
        j = api('https://advert-api.wildberries.ru/api/advert/v2/adverts', {'ids': ','.join(map(str, ids[i:i + 50]))}, tok, tries=2, pause=5)
        for a in (j or {}).get('adverts', []):
            st = a.get('settings') or {}; pl = st.get('placements') or {}
            nmu = str(st.get('name', '')).upper()
            # сначала название (у владельца «… АРК …», «… ПОИСК …»), потом тип ставки и места показа
            t = ('АРК' if 'АРК' in nmu else ('Поиск' if 'ПОИСК' in nmu else
                 ('АРК' if a.get('bid_type') == 'unified' else ('Поиск' if pl.get('search') else ('Реком' if pl.get('recommendations') else 'другая')))))
            for n in a.get('nm_settings') or []:
                art = nm2art.get(int(n.get('nm_id') or 0))
                if not art: continue
                b = n.get('bids_kopecks') or {}
                out.setdefault(str(art), []).append({'t': t, 'on': a.get('status') == 9, 'id': a.get('id'), 'name': st.get('name', ''),
                                                     'bid': round(max(b.get('search') or 0, b.get('recommendations') or 0) / 100)})
    return out


# ---------------------------------------------------------------- сборка
def build(tok, skip_ads=False, skip_promos=False):
    t = today_msk().isoformat()
    plan = json.load(open(PLAN, encoding='utf-8')) if os.path.exists(PLAN) else {}
    ctrl = set(map(str, plan.get('ctrl', []))); excl = set(map(str, plan.get('exclude', [])))
    min_share = float(plan.get('loco_min_share', 2.0)); add = set(map(str, plan.get('loco_add', []))); rem = set(map(str, plan.get('loco_remove', [])))
    prev = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
    warnings = []
    try:
        nm2art = goods_map(tok)
    except Exception as ex:
        warnings.append(f'карта nm→артикул: {ex}'); nm2art = {int(k): v for k, v in prev.get('nm2art', {}).items()}
    # реклама
    ad = None
    if not skip_ads:
        try: ad = ad_spend(tok)
        except Exception as ex: warnings.append(f'реклама: {ex} — взяты прошлые данные')
    if ad is None: ad = prev.get('ad_raw')
    # кампании по артикулам (АРК / Поиск, идут или на паузе) – значки у артикула на странице
    try: camps = ad_campaigns(tok, nm2art)
    except Exception as ex:
        warnings.append(f'кампании рекламы: {ex} — взяты прошлые данные'); camps = prev.get('ad_camps', {})
    by_art = {}
    if ad:
        for nm, b in ad['by_nm'].items():
            a = nm2art.get(int(nm)) if str(nm).isdigit() else None
            if not a: continue
            x = by_art.setdefault(a, {'spend': 0.0, 'orders': 0, 'views': 0, 'camps': 0})
            x['spend'] += b['spend']; x['orders'] += b['orders']; x['views'] += b['views']; x['camps'] = max(x['camps'], b['camps'])
    total = sum(x['spend'] for x in by_art.values()) or 0.0
    for a, x in by_art.items():
        x['spend'] = round(x['spend']); x['share'] = round(x['spend'] / total * 100, 1) if total else 0.0
    loco = {}
    for a, x in sorted(by_art.items(), key=lambda kv: -kv[1]['spend']):
        if a in ctrl or a in excl or a in rem: continue
        if x['share'] >= min_share and x['spend'] >= 1000: loco[a] = x
    for a in add:
        if a not in loco and a not in ctrl: loco[a] = by_art.get(a, {'spend': 0, 'orders': 0, 'views': 0, 'camps': 0, 'share': 0.0})
    # акции
    promos = None
    if not skip_promos:
        try: promos = promotions(tok)
        except Exception as ex: warnings.append(f'календарь акций: {ex} — взят прошлый список')
    if promos is None: promos = prev.get('promos', [])
    active = [p for p in promos if p['active']]
    # проходные
    caps_rep, notes = caps_from_reports(nm2art, promos); warnings += notes
    caps_man = caps_manual()
    caps_all = {}
    for src in (caps_rep, caps_man):
        for a, d in src.items(): caps_all.setdefault(a, {}).update(d)
    pid2p = {str(p['id']): p for p in promos}
    cap = {}
    for a, d in caps_all.items():
        best = None; inlist = []
        for key, v in d.items():
            p = pid2p.get(key)
            if key != '*' and (p is None or not p['active']): continue      # акция кончилась/не началась — её проходная не держит
            if v.get('in'): inlist.append(p['name'] if p else key)
            if best is None or v['cap'] > best['cap']:
                best = {'cap': v['cap'], 'promo_id': (None if key == '*' else int(key)),
                        'promo': (p['name'] if p else 'акция'), 'end': (p['end'] if p else None), 'src': v['src'],
                        'boost': (max((r['boost'] for r in p['ranging']), default=None) if p else None)}
        if best:
            best['in_now'] = inlist                 # в каких идущих акциях артикул участвует по выгрузке
            cap[a] = best
    covered = {k for d in caps_all.values() for k in d}
    uncovered = [{'id': p['id'], 'name': p['name'], 'end': p['end']} for p in active if str(p['id']) not in covered and '*' not in covered]
    if uncovered: warnings.append('нет выгрузок по идущим акциям: ' + ', '.join(f"{u['name']} (до {u['end']})" for u in uncovered) + ' — участие в них не видно')
    try:
        status = promo_status(caps_all, promos, t, [u['id'] for u in uncovered])
    except Exception as ex:
        warnings.append(f'статус в акциях не посчитан: {ex}'); status = {}
    out = {'date': t, 'ad': {'from': ad.get('from') if ad else None, 'to': ad.get('to') if ad else None, 'days': AD_DAYS,
                             'campaigns': ad.get('campaigns') if ad else None, 'total': round(total), 'by_art': by_art},
           'loco': loco, 'loco_rule': {'min_share': min_share, 'add': sorted(add), 'remove': sorted(rem), 'exit_ok': sorted(map(str, plan.get('loco_exit_ok', [])))},
           'promos': promos, 'active_count': len(active), 'cap': cap, 'caps_all': caps_all, 'status': status, 'uncovered': uncovered,
           'cap_sources': {'reports': len(glob.glob(os.path.join(REPORTS, '*.xlsx'))), 'manual': os.path.exists(MANUAL)},
           'warnings': warnings, 'ad_raw': ad, 'ad_camps': camps, 'nm2art': {str(k): v for k, v in nm2art.items()}}
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False); os.replace(tmp, OUT)
    print(f"[promos] реклама {out['ad']['from']}…{out['ad']['to']}: {out['ad']['total']} ₽ по {len(by_art)} артикулам; локомотивов {len(loco)}: "
          + ', '.join(f"{a} ({x['share']}%)" for a, x in list(loco.items())[:12]))
    ex7 = [a for a, s in status.items() if not s['in'] and s.get('since') and s['since'] >= (datetime.date.fromisoformat(t) - datetime.timedelta(days=7)).isoformat()]
    print(f"[promos] в акциях сейчас {sum(1 for s in status.values() if s['in'])} артикулов, вне акций {sum(1 for s in status.values() if s['in'] is False)}, неизвестно {sum(1 for s in status.values() if s['in'] is None)}; вышли за 7 дней: {', '.join(ex7) or 'нет'}")
    print(f"[promos] акций сейчас идёт {len(active)}, впереди {sum(1 for p in promos if p['upcoming'])}; проходные цены известны по {len(cap)} артикулам"
          + ('' if cap else ' — положите выгрузки из Календаря акций в promo-reports/ или заполните promo_prices.json'))
    for w in warnings: log(w)
    return out


def summary_for_page(P):
    """Компактная сводка для D.promos (без сырых данных)."""
    return {'date': P.get('date'), 'ad': {k: v for k, v in P.get('ad', {}).items() if k != 'by_art'},
            'loco': P.get('loco', {}), 'loco_rule': P.get('loco_rule', {}),
            'promos': [{k: v for k, v in p.items() if k != 'advantages'} for p in P.get('promos', [])],
            'active_count': P.get('active_count', 0), 'cap': P.get('cap', {}), 'caps_all': P.get('caps_all', {}),
            'cap_sources': P.get('cap_sources', {}), 'warnings': P.get('warnings', []), 'status': P.get('status', {}), 'uncovered': P.get('uncovered', []), 'ad_camps': P.get('ad_camps', {}),
            'top_ad': sorted(((a, x) for a, x in P.get('ad', {}).get('by_art', {}).items()), key=lambda kv: -kv[1]['spend'])[:25]}


def art_promos(P, a, price):
    """Список акций по артикулу для значка «А» на странице: идущие и будущие, с проходной, бустингом и
    участием (цена сейчас не выше проходной). Идущие акции без выгрузки — «нет данных»."""
    pid2p = {str(p['id']): p for p in P.get('promos', [])}
    out = []
    for key, v in (P.get('caps_all', {}).get(a) or {}).items():
        p = pid2p.get(key)
        if key == '*' or not p or not (p['active'] or p['upcoming']): continue
        boost = max((r['boost'] for r in p.get('ranging', []) if r.get('boost') is not None), default=None)
        inn = (price is not None and price <= v['cap'] * 1.003) if p['active'] else None
        out.append({'id': p['id'], 'name': p['name'], 'end': p['end'], 'start': p['start'], 'active': p['active'],
                    'cap': v['cap'], 'boost': boost, 'in': inn, 'flag': v.get('in')})
    for u in P.get('uncovered', []):
        p = pid2p.get(str(u['id']))
        out.append({'id': u['id'], 'name': u['name'], 'end': u['end'], 'start': p['start'] if p else None, 'active': True,
                    'cap': None, 'boost': (max((r['boost'] for r in p.get('ranging', [])), default=None) if p else None), 'in': None, 'unknown': True})
    out.sort(key=lambda x: (not x['active'], x['in'] is not True, x['name']))
    return out


def inject(page):
    src = open(page, encoding='utf-8').read()
    m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
    if not m: raise SystemExit('[promos] не нашёл const D=... в странице')
    D = json.loads(m.group(1))
    P = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
    D['promos'] = summary_for_page(P)
    opf = os.path.join(DIR, 'op_margin.json')
    OP = json.load(open(opf, encoding='utf-8')) if os.path.exists(opf) else {}
    D['op_meta'] = {'label': OP.get('label'), 'date': OP.get('date'), 'sheet': OP.get('sheet')} if OP else None
    sdf = os.path.join(DIR, 'stock_days.json')
    SD = json.load(open(sdf, encoding='utf-8')) if os.path.exists(sdf) else {}
    D['sd_meta'] = {'label': SD.get('label'), 'date': SD.get('date'), 'src_label': SD.get('src_label')} if SD else None
    by = P.get('ad', {}).get('by_art', {})
    for r in D['rows']:
        a = r['a']
        if 'loco' not in r:
            r['loco'] = a in P.get('loco', {}); r['ad'] = by.get(a); c = P.get('cap', {}).get(a)
            r['cap'] = c['cap'] if c else None; r['cap_promo'] = c['promo'] if c else None; r['cap_end'] = c['end'] if c else None
        r['pl'] = art_promos(P, a, r.get('p'))
        r['op'] = (OP.get('by_art') or {}).get(a) if OP else None
        # остаток дней теперь считает движок (r['sd'], r['sd_info']); значение из план-факта – справочно и как запасное
        r['sd_sheet'] = (SD.get('by_art') or {}).get(a) if SD else None
        if r.get('sd_info') is None: r['sd'] = r['sd_sheet']
        st = P.get('status', {}).get(a)
        if 'promo_in' not in r:
            r['promo_in'] = st['in'] if st else None; r['promo_since'] = st.get('since') if st else None; r['promo_known_from'] = st.get('known_from') if st else None
            r['promo_unknown'] = st.get('unknown', []) if st else []
            r.setdefault('exit', False)
    new = src[:m.start(1)] + json.dumps(D, ensure_ascii=False) + src[m.end(1):]
    open(page + '.tmp', 'w', encoding='utf-8').write(new); os.replace(page + '.tmp', page)
    print(f"[promos] в страницу вставлено: локомотивов {len(P.get('loco', {}))}, акций {len(P.get('promos', []))}, проходных {len(P.get('cap', {}))}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    a1 = sub.add_parser('fetch', help='собрать promos.json: реклама, акции, проходные')
    a1.add_argument('--token', default=os.environ.get('WB_TOKEN', ''))
    a1.add_argument('--no-ads', action='store_true'); a1.add_argument('--no-promos', action='store_true')
    a2 = sub.add_parser('inject', help='вставить D.promos в готовую страницу'); a2.add_argument('--page', default='page.html')
    a3 = sub.add_parser('show', help='напечатать локомотивы и проходные из promos.json')
    A = ap.parse_args()
    if A.cmd == 'fetch':
        if not A.token: sys.exit('[promos] нет WB_TOKEN')
        build(A.token, A.no_ads, A.no_promos)
    elif A.cmd == 'inject': inject(A.page)
    else:
        P = json.load(open(OUT, encoding='utf-8'))
        print(json.dumps({'loco': P['loco'], 'cap': P['cap'], 'promos': [(p['id'], p['name'][:40], p['start'], p['end'], p['active'], p['participation']) for p in P['promos']], 'warnings': P['warnings']}, ensure_ascii=False, indent=1))
