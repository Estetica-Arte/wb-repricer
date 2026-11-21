#!/usr/bin/env python3
"""Сезонная оборачиваемость для движка – тот же расчёт, что колонка AE «Остаток дней ВБ + Фул Москва» в «План-факте»
(~/wb-stock-days/update.py, с 09.10.2026). Код коэффициентов и колонок берётся оттуда же, чтобы логика была одна.

Пишет season.json:
  {"date", "coefs": {"1".."12": коэф. роста месяц к месяцу}, "where": {...ячейки...},
   "by_nm": {nmId: {"wb", "to_wb", "ff", "speed"}}}   – значения строк «ДействияПоАртикулам» (AP, AQ, AR, AG).
Движок (wb_daily.py, turnover.season=true) считает остаток дней: (WB сейчас + в отгрузке + на ФФ Москва) расходуется
день за днём со скоростью за 5 дней, каждый следующий месяц – × сезонный коэффициент (days_left из update.py).
При ошибке остаётся прошлый season.json (коэффициенты меняются редко).
"""
import datetime
import json
import os
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'season.json')
SD = '/opt/wb-stock-days'


def main():
    sys.path.insert(0, SD)
    cwd = os.getcwd(); os.chdir(SD)
    try:
        import update as sd                      # тот же код, что пишет AE
        coefs, where = sd.load_coefs()
        t = sd.CFG['target']
        sid, sh, hr, first = t['spreadsheet_id'], t['sheet'], int(t['header_row']), int(t['first_row'])
        head = sd.read(sid, sh, f'A{hr}:ZZ{hr}')[0]
        cols = {}
        for key, names in t['columns'].items():
            hit = [j for j, x in enumerate(head) if sd.norm_head(x) in {sd.norm_head(n) for n in names}]
            if hit: cols[key] = hit[0]
        need = ('nm', 'wb', 'to_wb', 'ff', 'speed')
        miss = [k for k in need if k not in cols]
        if miss: raise RuntimeError(f'в «{sh}» не нашёл колонки: {miss}')
        last = max(cols[k] for k in need)
        rows = sd.read(sid, sh, f'A{first}:{sd.col_letter(last)}1000')
    finally:
        os.chdir(cwd)
    by = {}
    for r in rows:
        r = r + [''] * (last + 1 - len(r))
        nm = str(r[cols['nm']]).strip().split('.')[0]
        if not nm.isdigit(): continue
        by[nm] = {k: sd.num(r[cols[k]]) for k in ('wb', 'to_wb', 'ff', 'speed')}
    out = {'date': datetime.datetime.now(sd.MSK).date().isoformat(), 'coefs': {str(m): coefs[m] for m in coefs},
           'where': {str(m): where[m] for m in where}, 'by_nm': by}
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, OUT)
    print(f'[season] коэффициенты: ' + ', '.join(f'{m}:{coefs[m]}' for m in range(1, 13)) + f'; строк план-факта: {len(by)}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'[season] !!! {type(e).__name__}: {e} – остаётся прошлый season.json'); sys.exit(1)
