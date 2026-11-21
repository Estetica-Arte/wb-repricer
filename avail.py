#!/usr/bin/env python3
"""Доступность артикула для подъёма цены (правило доступности):

  1. Остатки. Из Google Таблицы «Рассчет поставки Brand» → лист «РассчетПоставки»,
     колонки «Остаток на ФФ 1» + «Остаток на ФФ 2» (ищутся по названию в строке шапки; буквы сдвигаются – не запоминать; до 16.11.2026 – ФФ-1 + «с учетом китайской поставки»),
     ключ — артикул в колонке B (шапка в строке 5). Остаток на WB движок берёт сам из API
     (warehouse_remains, только склад «Склад WB РФ» – правило остатка).
     Если на фуле меньше AVAIL_FF_MIN (10) И на WB меньше AVAIL_WB_MIN (10) — артикула нет
     на остатках: он не поднимается, не откатывается и не участвует в расчёте бюджета потока.
     Дубли (305-1, 305-2) в таблице отдельной строкой не идут — берут остаток фула базового артикула.
  2. Фото. Карточки WB без фото (content-api /content/v2/get/cards/list, поле photos пустое)
     не работают — такие артикулы тоже не рекомендуются к подъёму.

Результат — avail.json:
  {"date": "...", "ff": {"305": 1378, ...}, "photos": {"305": 7, ...}, "nophoto": ["510", ...],
   "ff_min": 10, "wb_min": 10, "ff_date": "...", "photos_date": "..."}
Идёт в run.sh перед движком; при ошибке одного источника остаётся его прошлое значение из файла
(с прошлой датой), при ошибке обоих файл не трогается. Движок: wb_daily.py --avail avail.json.
"""
import json, os, sys, subprocess, datetime, re, time

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'avail.json')


def env():
    e = {}
    p = os.path.join(DIR, '.env')
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); e[k.strip()] = v.strip().strip('"\'')
    e.update({k: v for k, v in os.environ.items() if k.startswith(('GS_', 'AVAIL_', 'WB_'))})
    return e


def bridge(url, tok, tries=3, pause=20, **params):
    # GET с query-параметрами: тело POST теряется на 302-редиректе Google. HTML-заглушку повторяем.
    import urllib.parse
    q = urllib.parse.urlencode({'token': tok, **params}); last = None
    for i in range(tries):
        r = subprocess.run(['curl', '-sSL', f'{url}?{q}', '--max-time', '90'], capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
            if not j.get('ok'): raise RuntimeError(f"мост: {j.get('error')}")
            res = j.get('result')
            return res.get('values') if isinstance(res, dict) and 'values' in res else res
        except json.JSONDecodeError:
            last = f'не JSON ({r.stdout[:80]!r})'
        if i < tries - 1: time.sleep(pause)
    raise RuntimeError(f'мост: {last}')


def col_index(col):
    n = 0
    for ch in col.upper(): n = n * 26 + (ord(ch) - 64)
    return n - 1


def num(v):
    if v in (None, ''): return None
    try: return float(str(v).replace(',', '.').replace('\xa0', '').replace(' ', ''))
    except ValueError: return None


def ff_from_sheet(e):
    url, tok = e.get('GS_URL'), e.get('GS_TOKEN')
    if not url or not tok: raise RuntimeError('нет GS_URL / GS_TOKEN в .env')
    sid = e.get('AVAIL_SHEET_ID', '1BBEh0fnIe3VjNPMm1eFZQ8gVvC4zchw-dlAS8bUEtbQ')
    sheet = e.get('AVAIL_SHEET_NAME', 'РассчетПоставки')
    acol, fcol = e.get('AVAIL_ART_COL', 'B'), e.get('AVAIL_FF_COL', 'BF')
    hdr = int(e.get('AVAIL_HEADER_ROW', '5'))
    # колонку остатка ищем по названию в шапке: в таблицу добавляют столбцы, и буква съезжает
    # (30.09.2026 «Остаток на FBO» переехал из AW в BA). Буква из .env – запасной вариант.
    # фул для расчёта цен = сумма колонок (владелец 27.10.2026, «нужны все остатки, это остатки на двух фулах»):
    # «Остаток на ФФ 1» + «Остаток на ФФ 2». Каждую ищем по названию в строке шапки; не нашлась или встречается
    # дважды – ошибка, main() оставит прошлый avail.json. Буквы не подставляем: 26.10 так читали пустую AW.
    wants = [' '.join(x.split()).lower() for x in e.get('AVAIL_FF_HEADERS', 'Остаток на ФФ 1|Остаток на ФФ 2').split('|') if x.strip()]
    # «Товары в пути на склад ВБ» (сейчас ACY) – прибавляется к остатку в расчёте остатка дней (владелец 15.10.2026)
    want2 = e.get('AVAIL_TOWB_HEADER', 'Товары в пути на склад ВБ').strip().lower(); tcol = e.get('AVAIL_TOWB_COL', 'ADK')
    rows = bridge(url, tok, action='read', spreadsheetId=sid, sheet=sheet, range='A1:AEZ400')
    def cell(row, col):
        i = col if isinstance(col, int) else col_index(col)
        return row[i] if i < len(row) else None
    def col_name(i):
        s = ''; i += 1
        while i: i, r = divmod(i - 1, 26); s = chr(65 + r) + s
        return s
    hrow = rows[hdr - 1] if len(rows) >= hdr else []
    fis = []
    for w in wants:
        hit = [i for i, c in enumerate(hrow) if ' '.join(str(c or '').split()).lower() == w]
        if len(hit) != 1:
            raise RuntimeError(f'колонка «{w}» в строке {hdr} листа «{sheet}» найдена {len(hit)} раз – фул не прочитан, остаётся прошлый')
        fis.append(hit[0])
    fcol = '+'.join(col_name(i) for i in fis)
    ti = None
    if len(rows) >= hdr and want2:
        ti = next((i for i, c in enumerate(rows[hdr - 1]) if ' '.join(str(c or '').split()).lower().startswith(want2)), None)
    if ti is None: ti = col_index(tcol)
    tcol = col_name(ti); thead = ' '.join(str(cell(rows[hdr - 1], ti) or '').split()) if len(rows) >= hdr else ''
    head = ' + '.join(' '.join(str(hrow[i]).split()) for i in fis)
    ff, towb = {}, {}
    for row in rows[hdr:]:
        a = str(cell(row, acol) or '').strip()
        if not a or a.lower() == 'артикул': continue
        qs = [num(cell(row, i)) for i in fis]
        if all(q is None for q in qs): continue      # секции без остатков (тарелки, приборы) — пропускаем
        ff[a] = int(round(sum(q or 0 for q in qs)))   # сумма остатков на двух фулах
        t = num(cell(row, ti)); towb[a] = int(round(t)) if t is not None else 0
    if not ff: raise RuntimeError(f'в листе «{sheet}» не нашлось ни одного артикула с числом в {fcol} («{head}»)')
    return ff, head, sheet, fcol, towb, tcol, thead


def photos_from_wb(tok):
    photos = {}; SKU2ART.clear(); cur = {'limit': 100}
    while True:
        body = json.dumps({'settings': {'cursor': cur, 'filter': {'withPhoto': -1}}})
        r = subprocess.run(['curl', '-sS', '-X', 'POST', 'https://content-api.wildberries.ru/content/v2/get/cards/list',
                            '-H', f'Authorization: {tok}', '-H', 'Content-Type: application/json', '-d', body, '--max-time', '30'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or f'curl exit {r.returncode}').strip()[:200]
            raise RuntimeError(f'content-api curl: {err}')
        raw = (r.stdout or '').strip()
        if not raw:
            raise RuntimeError('content-api: пустой ответ')
        try:
            j = json.loads(raw)
        except json.JSONDecodeError as ex:
            raise RuntimeError(f'content-api: не JSON ({ex}); начало ответа: {raw[:120]!r}') from ex
        if 'cards' not in j: raise RuntimeError(f'content-api: {raw[:200]}')
        for c in j['cards']:
            photos[str(c.get('vendorCode', '')).strip()] = len(c.get('photos') or [])
            for sz in c.get('sizes') or []:
                for b in sz.get('skus') or []: SKU2ART[b] = str(c.get('vendorCode', '')).strip()
        c = j.get('cursor', {})
        if c.get('total', 0) < 100: break
        cur = {'limit': 100, 'updatedAt': c['updatedAt'], 'nmID': c['nmID']}
    if not photos: raise RuntimeError('content-api вернул пустой список карточек')
    return photos


SKU2ART = {}      # баркод → артикул продавца, заполняется при чтении карточек


def _mp(method, url, tok, body=None):
    cmd = ['curl', '-sS', '-X', method, url, '-H', f'Authorization: {tok}', '-H', 'Content-Type: application/json', '--max-time', '60', '-w', '\n%{http_code}']
    if body is not None: cmd += ['-d', json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True); out, _, code = r.stdout.rpartition('\n')
    if code != '200': raise RuntimeError(f'{url.split("/api/")[-1]}: {code} {out[:160]}')
    return json.loads(out)


def fbs_from_wb(tok):
    """Остаток, выставленный на складах продавца (FBS), по артикулу: сумма по всем складам из кабинета
    (marketplace-api /api/v3/warehouses + /api/v3/stocks/{склад}). 0 по всем складам = артикул «обнулён»."""
    if not SKU2ART: raise RuntimeError('нет баркодов карточек')
    whs = [w for w in _mp('GET', 'https://marketplace-api.wildberries.ru/api/v3/warehouses', tok) if not w.get('isDeleting')]
    skus = list(SKU2ART); fbs = {a: 0 for a in set(SKU2ART.values())}
    for w in whs:
        for i in range(0, len(skus), 1000):
            j = _mp('POST', f"https://marketplace-api.wildberries.ru/api/v3/stocks/{w['id']}", tok, {'skus': skus[i:i + 1000]})
            for s in j.get('stocks', []):
                a = SKU2ART.get(s['sku'])
                if a is not None: fbs[a] += int(s.get('amount') or 0)
            time.sleep(0.25)
    return fbs, len(whs)


def main(ff_only=False):
    e = env()
    try:
        from zoneinfo import ZoneInfo; today = datetime.datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    except Exception: today = datetime.date.today().isoformat()
    prev = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
    # порог «нет на остатках»: на фуле и на WB меньше 20 штук (решение владельца 30.09.2026, раньше 10)
    out = {'date': today, 'ff_min': int(e.get('AVAIL_FF_MIN', 20)),
           'wb_min': int(e.get('AVAIL_WB_MIN', 20))}
    errs = []
    try:
        ff, head, sheet, fcol, towb, tcol, thead = ff_from_sheet(e)
        out.update({'ff': ff, 'ff_date': today, 'ff_sheet': sheet, 'ff_col': fcol, 'ff_head': head,
                    'to_wb': towb, 'to_wb_col': tcol, 'to_wb_head': thead})
        print(f'[avail] остатки фула: лист «{sheet}» / {fcol} «{head}» — {len(ff)} артикулов, меньше {out["ff_min"]}: '
              + (', '.join(f'{a}={q}' for a, q in sorted(ff.items()) if q < out['ff_min']) or 'нет'))
        print(f'[avail] в пути на склад ВБ: {tcol} «{thead}» — {len(towb)} артикулов, всего {sum(towb.values())} шт')
    except Exception as ex:
        errs.append(f'таблица: {ex}')
        for k in ('ff', 'ff_date', 'ff_sheet', 'ff_col', 'ff_head', 'to_wb', 'to_wb_col', 'to_wb_head'):
            if k in prev: out[k] = prev[k]
    tok = e.get('WB_TOKEN')
    try:
        if ff_only: raise LookupError('только фул')
        if not tok: raise RuntimeError('нет WB_TOKEN')
        ph = photos_from_wb(tok)
        nop = sorted(a for a, n in ph.items() if n == 0)
        out.update({'photos': ph, 'nophoto': nop, 'photos_date': today})
        print(f'[avail] карточки WB: {len(ph)}, без фото: {", ".join(nop) or "нет"}')
    except LookupError:
        for k in ('photos', 'nophoto', 'photos_date'):
            if k in prev: out[k] = prev[k]
    except Exception as ex:
        errs.append(f'content-api: {ex}')
        for k in ('photos', 'nophoto', 'photos_date'):
            if k in prev: out[k] = prev[k]
    # остатки на складах продавца (FBS) – для проверки «не выставлен на складах»
    try:
        if ff_only: raise LookupError('только фул')
        if e.get('AVAIL_FBS', '') not in ('1', 'true', 'yes'): raise LookupError('выключено')   # правило по FBS снято 30.09.2026; включить – AVAIL_FBS=1
        if not tok: raise RuntimeError('нет WB_TOKEN')
        fbs, nwh = fbs_from_wb(tok)
        out.update({'fbs': fbs, 'fbs_date': today, 'fbs_warehouses': nwh})
        print(f'[avail] склады продавца (FBS): {nwh}, артикулов {len(fbs)}, с нулём на всех складах: {sum(1 for v in fbs.values() if v == 0)}')
    except LookupError:
        pass
    except Exception as ex:
        print(f'[avail] !!! склады FBS: {ex} — оставлено прошлое значение', file=sys.stderr)
        for k in ('fbs', 'fbs_date', 'fbs_warehouses'):
            if k in prev: out[k] = prev[k]
    if len(errs) == 2 and not prev:
        sys.exit('[avail] ' + '; '.join(errs))
    for m in errs: print(f'[avail] !!! {m} — оставлено прошлое значение', file=sys.stderr)
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, OUT)
    sys.exit(1 if len(errs) == 2 else 0)


if __name__ == '__main__':
    main(ff_only='--ff-only' in sys.argv)
