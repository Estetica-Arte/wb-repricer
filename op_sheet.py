#!/usr/bin/env python3
"""ОП по артикулам из Google Таблицы владельца («Юнит-экономика» → лист «Цены ВБ (FBS Москва)»,
колонка BC «ОП от нашей Цены», ключ — артикул продавца в колонке D).

Читается через мост Apps Script (URL и токен в .env: GS_URL, GS_TOKEN; лист и колонки —
OP_SHEET_ID, OP_SHEET_NAME, OP_ART_COL, OP_VAL_COL). Результат — op_margin.json:
  {"date": "...", "label": "ОП за 3 дня", "by_art": {"305": 0.12, ...}}   (доля, как принято у владельца: 0,12)
Идёт в run.sh перед сборкой страницы; при ошибке остаётся прошлый файл. Страница показывает
значение в карточке графика артикула (promos.py inject → r['op']).
"""
import json, os, sys, subprocess, datetime

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'op_margin.json')


def env():
    e = {}
    p = os.path.join(DIR, '.env')
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); e[k.strip()] = v.strip().strip('"\'')
    e.update({k: v for k, v in os.environ.items() if k.startswith(('GS_', 'OP_'))})
    return e


def bridge(url, tok, tries=3, pause=20, **params):
    # GET с query-параметрами: тело POST теряется на 302-редиректе Google (curl переходит на GET без тела).
    # Google иногда отдаёт HTML-заглушку вместо JSON — повторяем с паузой.
    import urllib.parse, time
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


def col_name(i):
    s = ''; i += 1
    while i:
        i, r = divmod(i - 1, 26); s = chr(65 + r) + s
    return s


def header_col(url, tok, sid, sheet, name):
    """Колонка по точному заголовку первой строки. Буква не запоминается: лист сдвигают.
    Не нашли или нашли дважды – None, вызывающий ничего не пишет."""
    want = ' '.join(name.split()).lower()
    rows = bridge(url, tok, action='read', spreadsheetId=sid, sheet=sheet, range='A1:ZZ1')
    h = rows[0] if rows else []
    hit = [i for i, c in enumerate(h) if ' '.join(str(c or '').split()).lower() == want]
    if len(hit) != 1: return None, len(hit)
    return col_name(hit[0]), 1


def block(url, tok, sid, sheet, c1, c2, n=400):
    """Один запрос на блок колонок c1..c2 — чтобы артикул, цена и ОП были из одного снимка листа."""
    v = bridge(url, tok, action='read', spreadsheetId=sid, sheet=sheet, range=f'{c1}1:{c2}{n}')
    base = col_index(c1)
    def cell(row, col):
        i = col_index(col) - base
        return row[i] if i < len(row) else None
    return v, cell


def main():
    e = env()
    url, tok = e.get('GS_URL'), e.get('GS_TOKEN')
    if not url or not tok: sys.exit('[op] нет GS_URL / GS_TOKEN в .env')
    sid = e.get('OP_SHEET_ID', '14WB3OssjSeBDK9duo5zhjdgjxwU3lWVBwWd3pXofExw')
    sheet = e.get('OP_SHEET_NAME', 'Цены ВБ (FBS Москва)')
    acol, pcol = e.get('OP_ART_COL', 'D'), e.get('OP_PRICE_COL', 'U')
    vheader = e.get('OP_VAL_HEADER', 'ОП от нашей Цены')
    vcol, nh = header_col(url, tok, sid, sheet, vheader)
    if not vcol:
        sys.exit(f'[op] колонка «{vheader}» на листе «{sheet}» найдена {nh} раз – файл не трогаю')
    lo = min(acol, vcol, pcol, key=col_index); hi = max(acol, vcol, pcol, key=col_index)
    rows, cell = block(url, tok, sid, sheet, lo, hi)
    src_label = str(cell(rows[0], vcol) or '').strip() if rows else f'колонка {vcol}'
    label = e.get('OP_LABEL', 'ОП за 3 дня')                # так показатель называют у владельца
    prev = json.load(open(OUT, encoding='utf-8')).get('by_art', {}) if os.path.exists(OUT) else {}
    by = {}; skipped = 0
    for row in rows[1:]:
        a, v, pr = cell(row, acol), cell(row, vcol), cell(row, pcol)
        if a in (None, '') or v in (None, ''): continue
        try: x = float(str(v).replace(',', '.').replace('%', '').replace('\xa0', '').replace(' ', ''))
        except ValueError: continue
        try: price = float(str(pr).replace(',', '.').replace('\xa0', '').replace(' ', '')) if pr not in (None, '') else 0
        except ValueError: price = 0
        if price <= 0:                                            # цена из WB не подтянулась — ОП в этот момент мусор
            skipped += 1
            if str(a).strip() in prev: by[str(a).strip()] = prev[str(a).strip()]
            continue
        if '%' in str(v) or abs(x) > 5: x /= 100                 # проценты → доля
        by[str(a).strip()] = round(x, 2)
    if len(by) < len(prev) * 0.7:
        sys.exit(f'[op] прочитано только {len(by)} артикулов против {len(prev)} раньше — похоже на сбой листа, файл не трогаю')
    try:
        from zoneinfo import ZoneInfo; today = datetime.datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    except Exception: today = datetime.date.today().isoformat()
    out = {'date': today, 'sheet': sheet, 'col': vcol, 'label': label, 'src_label': src_label, 'by_art': by}
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, OUT)
    print(f'[op] {sheet} / {vcol} «{src_label}» → «{label}»: {len(by)} артикулов' + (f'; без цены из WB (оставлено прошлое): {skipped}' if skipped else ''))


if __name__ == '__main__':
    main()
