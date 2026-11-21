#!/usr/bin/env python3
"""Подставить на уже собранные страницы свежие ОП и остаток дней, движок цен не запускать.

ОП – op_margin.json (юнитка, «ОП от нашей Цены»).
Дни – утренние штуки WB и скорость из sd_info, фул и «в пути на ВБ» из свежего avail.json
(колонки по названию). Решения утра (kind, new, act, note) не меняются.
"""
import argparse, datetime, json, os, re, sys

DIR = os.path.dirname(os.path.abspath(__file__))


def season_f(coefs, today):
    co = {int(k): float(v) for k, v in coefs.items()}
    idx = {0: 1.0}
    for k in range(1, 12):
        m = (today.month - 1 + k) % 12 + 1
        idx[k] = idx[k - 1] * co[m]
    idx[-1] = 1.0 / co[today.month]

    def f(d):
        k = (d.year - today.year) * 12 + d.month - today.month
        return idx[-1] if k == -1 else idx[k % 12]
    return f


def season_days_left(stock, speed, window, today, seas):
    if stock <= 0:
        return 0.0
    win = [today - datetime.timedelta(days=i) for i in range(1, window + 1)]
    base = speed / (sum(seas(d) for d in win) / window)
    d = today
    n = 0.0
    while n < 3650:
        daily = base * seas(d)
        if stock <= daily:
            return n + stock / daily
        stock -= daily
        n += 1
        d += datetime.timedelta(days=1)
    return 3650.0


def lookup(table, a):
    a = str(a)
    if a in table:
        return table[a]
    m = re.fullmatch(r'(.+)-\d+', a)
    return table.get(m.group(1)) if m else None


def calc(info, ff, to_wb, today, seas, season_on, need=3):
    wb = int(info.get('wb') or 0)
    q5 = float(info.get('q5') or 0)
    q28 = float(info.get('q28') or 0)
    if q5 * 5 >= need:
        sp, win = q5, 5
    elif q28 > 0:
        sp, win = q28, 28
    else:
        sp, win = 0.0, None
    ff_n = int(ff or 0)
    tw = int(to_wb or 0)
    tot = wb + ff_n + tw
    if season_on and seas and sp > 0:
        days = season_days_left(tot, sp, win, today, seas)
    else:
        days = (tot / sp) if sp > 0 else (9999.0 if tot > 0 else 0.0)
    out = {
        'days': round(min(days, 9999.0), 1), 'wb': wb, 'ff': ff_n, 'to_wb': tw,
        'q5': info.get('q5'), 'q28': info.get('q28'), 'win': win,
        'nosales': bool(sp == 0 and tot > 0), 'out': tot <= 0, 'season': bool(season_on),
        'of': info.get('of'),
    }
    for k in ('wb_own', 'q5_own'):
        if k in info:
            out[k] = info[k]
    return out


def apply(page, avail, op, season):
    src = open(page, encoding='utf-8').read()
    m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
    if not m:
        sys.exit(f'[sd] не нашёл const D в {page}')
    D = json.loads(m.group(1))
    today = datetime.date.fromisoformat(D['today'])
    coefs = (season or {}).get('coefs') or {}
    season_on = bool(coefs) and any((r.get('sd_info') or {}).get('season') for r in D['rows'])
    seas = season_f(coefs, today) if season_on else None
    need = 3
    try:
        op_plan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'op', 'plan.json'), encoding='utf-8'))
        need = int(((op_plan.get('op_target') or {}).get('speed_min_orders')) or 3)
    except (OSError, ValueError, TypeError):
        need = 3
    ff = {str(k): v for k, v in (avail.get('ff') or {}).items()}
    tw = {str(k): v for k, v in (avail.get('to_wb') or {}).items()}
    by = {str(r['a']): r for r in D['rows']}
    fresh = {}
    for r in D['rows']:
        a = str(r['a'])
        base = re.fullmatch(r'(.+)-[12]', a)
        if base and base.group(1) in by:
            continue
        info = r.get('sd_info') or {}
        fresh[a] = calc(info, lookup(ff, a), lookup(tw, a), today, seas, season_on and info.get('season', True), need)
    for r in D['rows']:
        a = str(r['a'])
        base = re.fullmatch(r'(.+)-[12]', a)
        if base and base.group(1) in fresh:
            own = r.get('sd_info') or {}
            o = dict(fresh[base.group(1)])
            o['of'] = base.group(1)
            o['wb_own'] = own.get('wb_own', own.get('wb'))
            o['q5_own'] = own.get('q5_own', own.get('q5'))
            fresh[a] = o
        elif a not in fresh:
            info = r.get('sd_info') or {}
            fresh[a] = calc(info, lookup(ff, a), lookup(tw, a), today, seas, season_on and info.get('season', True), need)
    changed = 0
    op_by = (op or {}).get('by_art') or {}
    for r in D['rows']:
        a = str(r['a'])
        info = fresh.get(a)
        if info and r.get('sd_info') is not None:
            old = r.get('sd')
            r['sd'] = info['days']
            r['sd_info'] = info
            r['ff'] = info['ff']
            if old != info['days']:
                changed += 1
        if a in op_by:
            r['op'] = op_by[a]
        elif op:
            r['op'] = None
    if isinstance(D.get('avail'), dict):
        D['avail']['ff_col'] = avail.get('ff_col')
        D['avail']['to_wb_col'] = avail.get('to_wb_col')
        D['avail']['ff_date'] = avail.get('ff_date')
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
        D['avail']['sd_recalc'] = now.strftime('%d.%m %H:%M') + ' МСК'
    if op:
        D['op_meta'] = {'label': op.get('label'), 'date': op.get('date'), 'sheet': op.get('sheet'), 'col': op.get('col')}
    new = src[:m.start(1)] + json.dumps(D, ensure_ascii=False) + src[m.end(1):]
    tmp = page + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(new)
    os.replace(tmp, page)
    art = next((r for r in D['rows'] if str(r['a']) == '308'), None)
    extra = ''
    if art:
        extra = f"; 308: ОП {art.get('op')}, дни {art.get('sd')}, фул {(art.get('sd_info') or {}).get('ff')}"
    print(f'[sd] {page}: дней изменено у {changed} строк{extra}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--page', action='append', required=True)
    ap.add_argument('--avail', default=os.path.join(DIR, 'avail.json'))
    ap.add_argument('--op', default=os.path.join(DIR, 'op_margin.json'))
    ap.add_argument('--season', default=os.path.join(DIR, 'season.json'))
    A = ap.parse_args()
    avail = json.load(open(A.avail, encoding='utf-8'))
    op = json.load(open(A.op, encoding='utf-8')) if os.path.exists(A.op) else {}
    season = json.load(open(A.season, encoding='utf-8')) if os.path.exists(A.season) else {}
    for page in A.page:
        apply(page, avail, op, season)


if __name__ == '__main__':
    main()
