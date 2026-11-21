#!/usr/bin/env python3
"""Первичная настройка кампании плавного подъёма цен на Wildberries.
Считает по данным кабинета: базовую цену каждого артикула, базовый поток,
подбирает контрольную группу и записывает plan.json + пустой state.json.

python3 wb_setup.py --token <WB_TOKEN> [--subject Товары] [--base-month 2026-06]
                   [--goal 15] [--floor 15] [--end 2026-09-30]
"""
import json, math, argparse, datetime, statistics, subprocess, sys, os, time, random
from collections import defaultdict

AP = argparse.ArgumentParser()
AP.add_argument('--token', required=True)
AP.add_argument('--subject', default=None, help='предмет WB, напр. "Товары"; без него — весь кабинет')
AP.add_argument('--base-month', default=None, help='YYYY-MM, база для цены и потока (по умолчанию 3 месяца назад)')
AP.add_argument('--goal', type=float, default=15.0, help='цель роста средней цены, %%')
AP.add_argument('--floor', type=float, default=15.0, help='предельное падение заказов, %%')
AP.add_argument('--end', default=None, help='дата окончания кампании YYYY-MM-DD (по умолчанию конец текущего месяца)')
AP.add_argument('--plan', default='plan.json')
AP.add_argument('--state', default='state.json')
AP.add_argument('--orders', default=None, help='готовый json заказов вместо запроса к API (для отладки)')
A = AP.parse_args()

TODAY = datetime.date.today(); LAST = TODAY - datetime.timedelta(days=1)
if A.base_month:
    by, bm = map(int, A.base_month.split('-'))
else:
    m = TODAY.month - 3; y = TODAY.year
    if m <= 0: m += 12; y -= 1
    by, bm = y, m
BASE_PREFIX = f'{by:04d}-{bm:02d}'
BASE_DAYS = (datetime.date(by + (bm == 12), (bm % 12) + 1, 1) - datetime.date(by, bm, 1)).days
if A.end:
    END = datetime.date.fromisoformat(A.end)
else:
    nm = datetime.date(TODAY.year + (TODAY.month == 12), (TODAY.month % 12) + 1, 1)
    END = nm - datetime.timedelta(days=1)

def api(url, params=None, out=None, tries=3):
    cmd = ['curl', '-sS', '-G', url, '-H', f'Authorization: {A.token}', '--max-time', '600']
    for k, v in (params or {}).items(): cmd += ['--data-urlencode', f'{k}={v}']
    if out: cmd += ['-o', out]
    for _ in range(tries):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and (out or r.stdout.strip()):
            return json.load(open(out, encoding='utf-8')) if out else json.loads(r.stdout)
        time.sleep(25)
    raise RuntimeError(f'WB API не ответил: {url}')

print(f'[1/5] Загружаю заказы с {BASE_PREFIX}-01 …', file=sys.stderr)
raw = json.load(open(A.orders, encoding='utf-8')) if (A.orders and os.path.exists(A.orders)) else \
      api('https://statistics-api.wildberries.ru/api/v1/supplier/orders',
          {'dateFrom': f'{BASE_PREFIX}-01'}, out='orders_setup.json')

print('[2/5] Загружаю остатки …', file=sys.stderr)
stock = {}
try:
    t = api('https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains',
            {'groupByNm': 'true', 'groupBySa': 'true'})
    time.sleep(35)
    for r in api(f"https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains/tasks/{t['data']['taskId']}/download"):
        q = next((w['quantity'] for w in r['warehouses'] if w['warehouseName'] == 'Склад WB РФ'), 0)
        stock[str(r['vendorCode'])] = stock.get(str(r['vendorCode']), 0) + q
except Exception as e:
    print(f'[warn] остатки не получены ({e}) — правило по оборачиваемости будет отключено', file=sys.stderr)

HIST_FROM = LAST - datetime.timedelta(days=69)
days = [(HIST_FROM + datetime.timedelta(days=i)).isoformat() for i in range((LAST - HIST_FROM).days + 1)]
di = {d: i for i, d in enumerate(days)}
art = defaultdict(lambda: {'c': [0] * len(days), 'p': defaultdict(list), 'nm': set(),
                           'bq': 0, 'bp': [], 'first': None})
for r in raw:
    if A.subject and r.get('subject') != A.subject: continue
    d = r['date'][:10]
    k = r['supplierArticle'] or '(без артикула)'
    a = art[k]; a['nm'].add(r['nmId'])
    if a['first'] is None or d < a['first']: a['first'] = d
    if d.startswith(BASE_PREFIX):
        a['bq'] += 1
        if not r.get('isCancel') and r.get('priceWithDisc'): a['bp'].append(r['priceWithDisc'])
    if d in di:
        a['c'][di[d]] += 1
        if not r.get('isCancel') and r.get('priceWithDisc'): a['p'][d].append(r['priceWithDisc'])

print('[3/5] Считаю базовые цены и поток …', file=sys.stderr)
rows = {}
for k, a in art.items():
    if sum(a['c']) == 0: continue
    ps = []; carry = None
    for d in days:
        v = statistics.median(a['p'][d]) if a['p'].get(d) else None
        if v is not None: carry = v
        ps.append(carry)
    f = next((x for x in ps if x is not None), None)
    if f is None: continue
    ps = [x if x is not None else f for x in ps]
    if len(a['bp']) >= 5:
        anchor, src = round(statistics.mean(a['bp'])), 'база'
    else:                       # артикул стартовал позже базового месяца
        early = [x for d in days[:14] for x in a['p'].get(d, [])]
        anchor, src = (round(statistics.mean(early)) if early else round(ps[0])), 'старт'
    b14 = sum(a['c'][-14:]) / 14
    st = stock.get(str(k)); st = None if st == 0 else st
    rows[k] = {'a': k, 'nm': sorted(a['nm'])[0], 'anchor': anchor, 'src': src,
               'p': round(ps[-1]), 'now_pct': round((ps[-1] / anchor - 1) * 100, 1),
               'q14': round(b14, 2), 'qjun': round(a['bq'] / BASE_DAYS, 2) if a['bq'] else None,
               'stock': st, 'cover': (round(st / b14) if (st and b14 > 0) else None),
               'series': a['c']}

print('[4/5] Подбираю контрольную группу …', file=sys.stderr)
W = [list(range(i, i + 7)) for i in range(0, len(days) - 6, 7)]
tot = [sum(r['series'][i] for r in rows.values()) for i in range(len(days))]
TW = [sum(tot[i] for i in w) for w in W]
def score(keys):
    cs = [sum(rows[k]['series'][i] for k in keys) for i in range(len(days))]
    cw = [sum(cs[i] for i in w) for w in W]
    if min(cw) < 8: return None
    lr = [math.log((t - c) / c) for t, c in zip(TW, cw) if c > 0 and t > c]
    if len(lr) < len(W): return None
    n = len(lr); xs = list(range(n)); mx = sum(xs) / n; my = sum(lr) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, lr)) / sum((x - mx) ** 2 for x in xs)
    var = statistics.pvariance(lr)
    return var + 40 * slope * slope, var, slope, sum(cs) / sum(tot)
pool = [k for k, r in rows.items()
        if sum(r['series']) >= 25 and (r['qjun'] or 0) >= 0.5 and r['q14'] < max(6.0, 0.05 * sum(tot) / len(days))
        and not (r['cover'] and r['cover'] > 150)]
best = None; random.seed(7)
for _ in range(40):
    cur = []; cs = None; order = pool[:]; random.shuffle(order)
    for _ in range(14):
        cand = None; cbest = cs
        for k in order:
            if k in cur: continue
            s = score(cur + [k])
            if s is None or s[3] > 0.13: continue
            if cbest is None or s[0] < cbest[0] - 1e-9: cbest = s; cand = k
        if cand is None: break
        cur.append(cand); cs = cbest
        if cs[3] >= 0.07 and len(cur) >= 6: break
    if cs and cs[3] >= 0.05 and (best is None or cs[0] < best[1][0]): best = (sorted(cur), cs)
if best is None:
    raise SystemExit('Не удалось подобрать контрольную группу: слишком мало артикулов с историей.')
CTRL, sc = best
print(f'    контроль: {CTRL} — доля потока {sc[3]*100:.1f}%, разброс отношения {math.sqrt(sc[1])*100:.1f}%, '
      f'дрейф {sc[2]*100:+.2f}%/нед', file=sys.stderr)

print('[5/5] Считаю целевые проценты и пишу файлы …', file=sys.stderr)
flow = sum(r['q14'] for r in rows.values()) or 1
frozen = sum(r['q14'] for k, r in rows.items() if k in CTRL or (r['cover'] and r['cover'] > 150))
share = max(0.3, 1 - frozen / flow)
target_pct = round(min(A.goal * 1.45, A.goal / share), 1)
base_price = round(sum((r['anchor'] * (r['qjun'] or r['q14'])) for r in rows.values()) /
                   sum((r['qjun'] or r['q14']) for r in rows.values()))
base_day = round(sum(r['qjun'] or 0 for r in rows.values()), 1)
out = []
for k, r in sorted(rows.items(), key=lambda kv: -kv[1]['q14']):
    mode = 'контроль' if k in CTRL else ('затоварен' if (r['cover'] and r['cover'] > 150) else 'поднимать')
    tgt = 0.0 if mode != 'поднимать' else target_pct
    out.append({'a': r['a'], 'nm': r['nm'], 'anchor': r['anchor'], 'src': r['src'], 'p': r['p'],
                'now_pct': r['now_pct'], 'q14': r['q14'], 'qjun': r['qjun'], 'stock': r['stock'],
                'cover': r['cover'], 'tgt_pct': tgt, 'mode': mode,
                'p_target': round(r['anchor'] * (1 + tgt / 100))})
plan = {'subject': A.subject, 'base_month': BASE_PREFIX, 'ctrl': CTRL,
        'goal_pct': A.goal, 'target_pct': target_pct, 'cap_from_now': 25.0,
        'june_orders_day': base_day, 'june_price': base_price,
        'start': TODAY.isoformat(), 'end': END.isoformat(),
        'floor_pct': A.floor, 'yellow_at': 0.55, 'red_at': 0.80, 'rows': out}
json.dump(plan, open(A.plan, 'w', encoding='utf-8'), ensure_ascii=False)
if not os.path.exists(A.state):
    json.dump({'arts': {}, 'meta': {}}, open(A.state, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

print(f"\nГотово. Артикулов: {len(out)} | база {BASE_PREFIX}: средняя цена {base_price} ₽, {base_day:.0f} заказов/день")
print(f"Цель: +{A.goal:.0f}% к базовой цене к {END.isoformat()} (по поднимаемым артикулам это +{target_pct:.1f}%)")
print(f"Лимит падения заказов: −{A.floor:.0f}% от базы кампании | контрольная группа: {', '.join(CTRL)}")
print(f"Поднимаем {sum(1 for r in out if r['mode']=='поднимать')}, заморожено в контроле {len(CTRL)}, "
      f"затоварено {sum(1 for r in out if r['mode']=='затоварен')}")
