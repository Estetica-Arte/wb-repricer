#!/usr/bin/env python3
"""«Остаток дней» по артикулам из Google Таблицы план-факта — справочно, показывается рядом со значком «А».

Читается через мост Apps Script (URL и токен в .env: GS_URL, GS_TOKEN). Настройки в .env:
  SD_SHEET_ID     — ID таблицы план-факта
  SD_SHEET_NAME   — лист (например «ДействияПоАртикулам»)
  SD_ART_COL      — колонка с артикулом продавца (A)
  SD_VAL_COL      — колонка «Остаток дней» (AE)
  SD_HEADER_ROW   — строка шапки (3); данные читаются ниже неё
  SD_LABEL        — подпись на странице (по умолчанию «Остаток дней»)
Результат — stock_days.json: {"date": "...", "label": "Остаток дней", "src_label": "…", "by_art": {"305": 138.6}}
Идёт в run.sh перед сборкой страницы; при ошибке остаётся прошлый файл; без SD_SHEET_ID шаг пропускается.
Страница: promos.py inject → r['sd'], показывается в колонке «А» рядом со значком акций.
"""
import json, os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from op_sheet import env, bridge, col_index      # тот же мост и разбор блока, что у ОП

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'stock_days.json')


def main():
    e = env()
    e.update({k: v for k, v in os.environ.items() if k.startswith('SD_')})
    url, tok = e.get('GS_URL'), e.get('GS_TOKEN')
    sid = e.get('SD_SHEET_ID')
    if not sid: print('[sd] SD_SHEET_ID не задан — остаток дней из таблицы не читается'); return
    if not url or not tok: print('[sd] GS_URL / GS_TOKEN не заданы — остаток дней не читается'); return
    sheet = e.get('SD_SHEET_NAME', '')
    acol, vcol = e.get('SD_ART_COL', 'A'), e.get('SD_VAL_COL', 'AE')
    hdr = int(e.get('SD_HEADER_ROW', '1'))
    lo = min(acol, vcol, key=col_index); hi = max(acol, vcol, key=col_index); base = col_index(lo)
    rows = bridge(url, tok, action='read', spreadsheetId=sid, sheet=sheet, range=f'{lo}1:{hi}400')
    def cell(row, col):
        i = col_index(col) - base
        return row[i] if i < len(row) else None
    src_label = str(cell(rows[hdr - 1], vcol) or '').strip() if len(rows) >= hdr else f'колонка {vcol}'
    prev = json.load(open(OUT, encoding='utf-8')).get('by_art', {}) if os.path.exists(OUT) else {}
    by = {}
    for row in rows[hdr:]:
        a = str(cell(row, acol) or '').strip(); v = cell(row, vcol)
        if not a or v in (None, '') or a.lower().startswith('арт'): continue
        try: x = float(str(v).replace(',', '.').replace('\xa0', '').replace(' ', ''))
        except ValueError: continue
        by[a] = round(x, 1)
    if len(by) < len(prev) * 0.7:
        sys.exit(f'[sd] прочитано только {len(by)} артикулов против {len(prev)} раньше — похоже на сбой листа, файл не трогаю')
    try:
        from zoneinfo import ZoneInfo; today = datetime.datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    except Exception: today = datetime.date.today().isoformat()
    out = {'date': today, 'sheet': sheet, 'col': vcol, 'label': e.get('SD_LABEL', 'Остаток дней'), 'src_label': src_label, 'by_art': by}
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, OUT)
    low = sorted(((a, x) for a, x in by.items() if x < 10), key=lambda kv: kv[1])
    print(f'[sd] {sheet} / {vcol} «{src_label}»: {len(by)} артикулов' + (f'; меньше 10 дней: ' + ', '.join(f'{a}={x:g}' for a, x in low[:15]) if low else ''))


if __name__ == '__main__':
    main()
