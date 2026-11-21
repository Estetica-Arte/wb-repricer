#!/usr/bin/env python3
"""План продаж и маржа по артикулу – справочные колонки страницы (на решения движка не влияют).

1. Заказов в день к плану. План продаж на месяц, руб – Google Таблица план-факта (PM_SHEET_ID, лист PM_SHEET_NAME,
   колонка R «План продаж, руб»; артикул в A, шапка в строке PM_HEADER_ROW; там же AA «Факт продаж, руб» с начала месяца).
   Нужно заказов в день = (план − факт) ÷ (средний чек × % выкупа) ÷ дней до конца месяца (сегодня включительно).
   – средний чек: средняя цена заказа до СПП за 7 дней (wb_orders финансового сервиса), нет заказов – цена сейчас;
   – % выкупа: доля выкупленных заказов артикула за окно 45–14 дней назад (заказы wb_orders × продажи wb_sales по srid,
     дни из plan.json → anomaly_days исключены); меньше 30 заказов – выкуп базового артикула (для A-1/A-2), затем
     колонка U таблицы, затем средний по кабинету;
   – сейчас заказов в день: среднее за 7 полных дней.
   Если таблица недоступна – план берётся из копии финансового сервиса (sales_plan, тот же столбец R), факт – из wb_sales.
2. Маржа после налогов. Финансовый сервис mp-finance (/opt/mp-finance, та же логика, что в ОПиУ и «Сводной WB»):
   выручка до СПП − себестоимость − комиссия − эквайринг − логистика − хранение − реклама − штрафы − приёмка − удержания
   + компенсации − налог (УСН 5% + НДС 5%/1,05 от выручки после СПП). Период – 28 дней, закрытых финотчётами WB.
   Маржинальность = маржа после налогов ÷ выручка до СПП. Без сервиса – колонки пустые.

  python3 plan_margin.py fetch                 → plan_margin.json (в run.sh до сборки страницы)
  python3 plan_margin.py inject --page page.html   → r['need'], r['mg'], r['mgp'], r['pmf'] в строки страницы
"""
import json, os, sys, argparse, datetime, calendar, re, sqlite3
from collections import defaultdict

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, 'plan_margin.json')
sys.path.insert(0, DIR)


def env():
    from op_sheet import env as _env
    e = _env()
    e.update({k: v for k, v in os.environ.items() if k.startswith(('PM_', 'FIN_'))})
    return e


def today_msk():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Europe/Moscow')).date()
    except Exception:
        return datetime.date.today()


def num(v):
    if v in (None, ''): return None
    try: return float(str(v).replace(',', '.').replace('\xa0', '').replace(' ', ''))
    except ValueError: return None


def base_art(a):
    m = re.fullmatch(r'(.+)-\d+', str(a))
    return m.group(1) if m else None


# ---------------------------------------------------------------- план из таблицы
def sheet_plan(e):
    from op_sheet import bridge, col_index
    sid = e.get('PM_SHEET_ID')
    if not sid: raise LookupError('PM_SHEET_ID не задан')
    sheet = e.get('PM_SHEET_NAME', 'ДействияПоАртикулам'); hdr = int(e.get('PM_HEADER_ROW', '3'))
    ac, pc, uc, fc = e.get('PM_ART_COL', 'A'), e.get('PM_PLAN_COL', 'R'), e.get('PM_BUYOUT_COL', 'U'), e.get('PM_FACT_COL', 'AA')
    oc = e.get('PM_OP_COL', 'H')                                   # «ОП за месяц итого» – доля выручки (0,14 = 14%)
    hi = max((ac, pc, uc, fc, oc), key=col_index)
    rows = bridge(e['GS_URL'], e['GS_TOKEN'], action='read', spreadsheetId=sid, sheet=sheet, range=f'A1:{hi}400')
    def cell(row, col):
        i = col_index(col)
        return row[i] if i < len(row) else None
    out, ops = {}, {}
    for row in rows[hdr:]:
        a = str(cell(row, ac) or '').strip()
        if not a or not re.search(r'\d', a): continue          # строки-менеджеры «ВИКА», пустые
        op = num(cell(row, oc))
        if op is not None:
            if abs(op) > 5: op /= 100                             # на случай процентов вместо доли
            ops[a] = round(op, 4)
        plan = num(cell(row, pc))
        if not plan: continue
        out[a] = {'plan': plan, 'fact': num(cell(row, fc)) or 0.0, 'buyout_plan': num(cell(row, uc))}
    if not out: raise RuntimeError(f'в листе «{sheet}» нет ни одного плана в колонке {pc}')
    return out, f'{sheet} / {pc}', ops


# ---------------------------------------------------------------- финансовый сервис
def fin_db(e):
    d = e.get('FIN_DIR', '/opt/mp-finance')
    p = os.path.join(d, 'data', 'finance.sqlite')
    if not os.path.exists(p): raise LookupError(f'нет {p}')
    c = sqlite3.connect(f'file:{p}?mode=ro', uri=True); c.row_factory = sqlite3.Row
    return c, d


FIN_TOTAL = {}


def fin_margin(fdir, days=28, shift=0):
    """Маржа после налогов по артикулу продавца за `days` дней, закрытых финотчётами (функция финансового сервиса).
    shift – сдвиг назад от последнего закрытого дня (для сравнения с предыдущим таким же периодом)."""
    cwd = os.getcwd(); sys.path.insert(0, fdir); os.chdir(fdir)
    try:
        from app.reports.wb_digit import wb_period_sku, last_fact_date
        lf = last_fact_date()
        if not lf: return {}, None, None
        lf = lf - datetime.timedelta(days=shift)
        s = lf - datetime.timedelta(days=days - 1)
        res = wb_period_sku(s, lf, estimate=False)
    finally:
        os.chdir(cwd)
    by = defaultdict(lambda: {'rev': 0.0, 'margin': 0.0, 'qty': 0.0, 'tax': 0.0, 'var': 0.0, 'no_cost': False})
    # итог по WB-кабинету целиком (включая строки без артикула: реклама с баланса, подписки) – как в ОПиУ
    tot = {'rev': sum(m['revenue'] or 0 for m in res.values()), 'margin': sum(m['margin'] or 0 for m in res.values()),
           'ads': sum(m.get('ads') or 0 for m in res.values()), 'orders_rub': sum(m.get('orders_rub') or 0 for m in res.values())}
    FIN_TOTAL.update(tot)
    for nm, m in res.items():
        a = str(m.get('seller_sku') or '').strip()
        if not a: continue
        b = by[a]
        if m.get('unit_cost') in (None, 0) and (m.get('sales_qty') or 0) > 0: b['no_cost'] = True   # себестоимость не заполнена – маржа завышена
        b['rev'] += m['revenue'] or 0; b['margin'] += m['margin'] or 0; b['qty'] += m['sales_qty'] or 0; b['tax'] += m['tax'] or 0
        b['var'] += (m.get('commission') or 0) + (m.get('acquiring') or 0) + (m.get('tax') or 0)   # расходы, пропорциональные цене
    return dict(by), s.isoformat(), lf.isoformat()


def fin_orders(c, today, anomaly):
    """% выкупа (когорта заказов 45–14 дней назад), средний чек и заказов/день за 7 полных дней."""
    a, b = (today - datetime.timedelta(days=45)).isoformat(), (today - datetime.timedelta(days=14)).isoformat()
    an = tuple(anomaly) or ('0000-00-00',)
    q = f"""WITH o AS (SELECT srid, seller_sku FROM wb_orders WHERE date>=? AND date<? AND substr(date,1,10) NOT IN ({','.join('?'*len(an))})),
             s AS (SELECT srid, SUM(sign) sg FROM wb_sales GROUP BY srid)
            SELECT o.seller_sku a, COUNT(*) n, SUM(CASE WHEN s.sg>0 THEN 1 ELSE 0 END) b FROM o LEFT JOIN s ON s.srid=o.srid GROUP BY o.seller_sku"""
    buy = {str(r['a']): (r['n'], r['b']) for r in c.execute(q, (a, b, *an))}
    w0, w1 = (today - datetime.timedelta(days=7)).isoformat(), today.isoformat()
    chk = {str(r['a']): (r['avg'], r['n']) for r in c.execute(
        "SELECT seller_sku a, AVG(price_with_disc) avg, COUNT(*) n FROM wb_orders WHERE date>=? AND date<? GROUP BY seller_sku", (w0, w1))}
    return buy, chk


def fin_drr(c, today):
    """ДРР (реклама ÷ заказы в рублях) по артикулу и по магазину: за 7 полных дней и за базу 8–35 дней назад;
    заказы в рублях в день за 7 дней. Реклама – wb_adv_daily (по артикулам из рекламного кабинета), заказы – wb_orders."""
    d7, d1 = (today - datetime.timedelta(days=7)).isoformat(), today.isoformat()
    b0, b1 = (today - datetime.timedelta(days=35)).isoformat(), d7
    p0, p1 = (today - datetime.timedelta(days=14)).isoformat(), d7      # предыдущие 7 дней – динамика на плитке (13.10.2026)
    nm2a = {str(r['nm_id']): str(r['seller_sku']) for r in c.execute("SELECT DISTINCT nm_id, seller_sku FROM wb_orders WHERE seller_sku IS NOT NULL")}
    def ads(a0, a1):
        out = defaultdict(float)
        for r in c.execute("SELECT nm_id, SUM(sum) s FROM wb_adv_daily WHERE day>=? AND day<? GROUP BY nm_id", (a0, a1)):
            out[nm2a.get(str(r['nm_id']), '')] += r['s'] or 0
        return out
    def orders(a0, a1):
        return {str(r['a']): (r['rub'] or 0, r['n'] or 0) for r in c.execute(
            "SELECT seller_sku a, SUM(price_with_disc) rub, COUNT(*) n FROM wb_orders WHERE date>=? AND date<? GROUP BY seller_sku", (a0, a1))}
    a7, ab = ads(d7, d1), ads(b0, b1); o7, ob = orders(d7, d1), orders(b0, b1)
    ap, op_ = ads(p0, p1), orders(p0, p1)
    tot_a7 = sum(v for k, v in a7.items()); tot_o7 = sum(v[0] for v in o7.values())
    tot_ab = sum(v for k, v in ab.items()); tot_ob = sum(v[0] for v in ob.values())
    tot_ap = sum(v for k, v in ap.items()); tot_op = sum(v[0] for v in op_.values())
    dd = lambda n: (today - datetime.timedelta(days=n)).isoformat()
    store = {'orders_rub_day': round(tot_o7 / 7), 'orders_qty_day': round(sum(v[1] for v in o7.values()) / 7, 1),
             'ads_day': round(tot_a7 / 7), 'drr7': round(tot_a7 / tot_o7 * 100, 2) if tot_o7 else None,
             'drr_base': round(tot_ab / tot_ob * 100, 2) if tot_ob else None,
             # предыдущие 7 дней (8–14 дней назад) – «как было в прошлый такой же период»
             'drr7_from': d7, 'drr7_to': dd(1), 'drr7_prev_from': p0, 'drr7_prev_to': dd(8),
             'drr7_prev': round(tot_ap / tot_op * 100, 2) if tot_op else None,
             'ads_prev_day': round(tot_ap / 7), 'orders_rub_prev_day': round(tot_op / 7)}
    per = {}
    for a in set(o7) | set(a7):
        if not a: continue
        r7 = o7.get(a, (0, 0))[0]; rb = ob.get(a, (0, 0))[0]
        per[a] = {'ads7': round(a7.get(a, 0)), 'orders7': round(r7),
                  'drr7': round(a7.get(a, 0) / r7 * 100, 2) if r7 else None,
                  'drr_base': round(ab.get(a, 0) / rb * 100, 2) if rb else None}
    return store, per


def fin_cpo(c, today, days=63):
    """Реклама по дням для CPO по формуле рекламного сервиса (сверено 30.09.2026 по 308: 65 219 ₽ / (120 + 153) = 238,9 ₽):
    расход на артикул во всех его кампаниях ÷ (рекламные заказы самого артикула + заказы других артикулов по этим кампаниям).
    «Другие артикулы» – строки кампании без расхода; в кампании на несколько артикулов делятся по доле расхода.
    Выход: {артикул продавца: [[день, расход, заказы, клики], ...]} за days закрытых дней (без сегодняшнего)."""
    d0, d1 = (today - datetime.timedelta(days=days)).isoformat(), today.isoformat()
    nm2a = {str(r['nm_id']): str(r['seller_sku']) for r in c.execute("SELECT DISTINCT nm_id, seller_sku FROM wb_orders WHERE seller_sku IS NOT NULL")}
    by = defaultdict(list)
    for r in c.execute("SELECT day, advert_id, nm_id, SUM(sum) s, SUM(orders) o, SUM(clicks) k FROM wb_adv_daily "
                       "WHERE day>=? AND day<? GROUP BY day, advert_id, nm_id", (d0, d1)):
        by[(r['day'], r['advert_id'])].append((str(r['nm_id']), r['s'] or 0.0, r['o'] or 0.0, r['k'] or 0.0))
    out = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    for (day, _), lst in by.items():
        tot = sum(x[1] for x in lst)
        if tot <= 0: continue
        other = sum(x[2] for x in lst if x[1] <= 0)                  # заказы других артикулов по кампании
        for nm, s_, o_, k_ in lst:
            if s_ <= 0: continue
            a = nm2a.get(nm)
            if not a: continue
            x = out[a][day]; x[0] += s_; x[1] += o_ + other * s_ / tot; x[2] += k_
    return {a: [[d, round(v[0], 2), round(v[1], 2), round(v[2])] for d, v in sorted(dd.items())] for a, dd in out.items()}


def fin_daily(c, today, cpo, days=28):
    """Динамика для плиток страницы: по дням за `days` закрытых дней – заказы ₽ и шт, реклама, ДРР, средний чек,
    CPO локомотивов (формула рекламного сервиса по cpo_daily, локомотивы – из promos.json)."""
    ds = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days, 0, -1)]
    o, spp = {}, {}
    for r in c.execute("SELECT substr(date,1,10) d, SUM(price_with_disc) rub, COUNT(*) n, SUM(spp*price_with_disc) sw FROM wb_orders "
                       "WHERE date>=? AND date<? GROUP BY d", (ds[0], today.isoformat())):
        o[r['d']] = (r['rub'] or 0, r['n'] or 0)
        if r['rub']: spp[r['d']] = (r['sw'] or 0) / r['rub']           # СПП за день, взвешенная по сумме заказа
    a = {r['day']: r['s'] or 0 for r in c.execute("SELECT day, SUM(sum) s FROM wb_adv_daily WHERE day>=? AND day<? GROUP BY day", (ds[0], today.isoformat()))}
    try: loco = set((json.load(open(os.path.join(DIR, 'promos.json'), encoding='utf-8')).get('loco') or {}).keys())
    except Exception: loco = set()
    cs, co = defaultdict(float), defaultdict(float)
    for art, rows in (cpo or {}).items():
        if art not in loco: continue
        for d, sp, n, _k in rows: cs[d] += sp; co[d] += n
    rnd = lambda x, k=0: round(x, k) if x is not None else None
    return {'days': ds,
            'orders_rub': [rnd(o.get(d, (0, 0))[0]) for d in ds], 'orders_qty': [o.get(d, (0, 0))[1] for d in ds],
            'ads': [rnd(a.get(d, 0)) for d in ds],
            'drr': [rnd(a.get(d, 0) / o[d][0] * 100, 2) if o.get(d, (0, 0))[0] else None for d in ds],
            'check': [rnd(o[d][0] / o[d][1]) if o.get(d, (0, 0))[1] else None for d in ds],
            'cpo_loco': [rnd(cs[d] / co[d]) if co.get(d) else None for d in ds],
            'spp': [rnd(spp[d], 1) if d in spp else None for d in ds]}


def fin_op7(fdir, days=7):
    """ОП после налогов за последние `days` дней по ЕЖЕДНЕВНЫМ финотчётам WB (wb_fin_daily_rows; дни, которых там нет, –
    из недельного отчёта по дате операции, суммы совпадают до рубля). Тот же код, что ОПиУ и «WB по дням» (daily_dash):
    группировка строк финотчёта, себестоимость и налог, реклама по артикулу из fullstats. Решение владельца 09.10.2026.
    Возвращает (по артикулу продавца {rev, margin, qty, no_cost}, магазин {op7, op7_prev, from, to, prev_from, prev_to}, ОП по дням)."""
    cwd = os.getcwd(); sys.path.insert(0, fdir); os.chdir(fdir)
    try:
        from app.reports import daily_dash as dd
        from app.db import query, scalar
        from app.etl.products import cost_map, product_map
        cmap, pmap = cost_map(), product_map('wb')
        last = scalar("SELECT MAX(rr_dt) FROM wb_fin_daily_rows")
        if not last: return {}, {}, []
        end = datetime.date.fromisoformat(last); start = end - datetime.timedelta(days=days - 1)
        cols = ('rr_dt, nm_id, seller_sku, doc_type, oper_name, bonus_type, quantity, retail_amount, retail_price_withdisc, for_pay, '
                'acquiring_fee, delivery_rub, storage_fee, penalty, acceptance, additional_payment, deduction, rebill_logistic')
        def rows_for(s_, e_):
            d = query(f"SELECT {cols} FROM wb_fin_daily_rows WHERE rr_dt BETWEEN ? AND ?", (s_.isoformat(), e_.isoformat()))
            have = {r['rr_dt'] for r in d}
            miss = [(s_ + datetime.timedelta(days=i)).isoformat() for i in range((e_ - s_).days + 1)]
            miss = [x for x in miss if x not in have]
            if miss:
                d += query(f"SELECT {cols} FROM wb_fin_rows WHERE rr_dt IN ({','.join('?' * len(miss))})", miss)
            return d
        def ads_by_nm(s_, e_):
            return {str(r['nm_id']): r['s'] or 0.0 for r in query(
                "SELECT nm_id, SUM(sum) s FROM wb_adv_daily WHERE day BETWEEN ? AND ? GROUP BY nm_id", (s_.isoformat(), e_.isoformat()))}
        def calc(s_, e_):
            res = dd._sales_by(rows_for(s_, e_), lambda r: dd._nm(r['nm_id']))
            dd._add_cost_tax(res, lambda k: k, cmap, pmap)
            ads = ads_by_nm(s_, e_)
            by = {}; tot_rev = tot_pb = 0.0
            for nm, m in res.items():
                pb = dd._profit_before_ads(m); tot_rev += m['revenue']; tot_pb += pb
                if not nm: continue
                a = str((pmap.get(nm) or {}).get('seller_sku') or '').strip()
                if not a: continue
                x = by.setdefault(a, {'rev': 0.0, 'margin': 0.0, 'qty': 0.0, 'var': 0.0, 'no_cost': False})
                x['rev'] += m['revenue']; x['margin'] += pb - ads.get(nm, 0.0); x['qty'] += m['qty']
                x['var'] += m['commission'] + m['acquiring'] + m['tax']   # расходы, пропорциональные цене – для ОП при другой цене
                if m.get('no_cost_qty'): x['no_cost'] = True
            tot_ads = sum(ads.values())
            return by, ((tot_pb - tot_ads) / tot_rev * 100 if tot_rev else None), tot_rev
        by, op7, rev7 = calc(start, end)
        pe = start - datetime.timedelta(days=1); ps = pe - datetime.timedelta(days=days - 1)
        _, op7p, rev7p = calc(ps, pe)
        # ОП по дням за 28 дней + скользящее ОП за 7 дней на каждый день (линия динамики на графике плитки, владелец 15.10.2026);
        # 34 дня, чтобы у первого из 28 дней было своё окно 7 дней. Источник – «WB по дням»: строки финотчёта по дате операции
        # (недельные, а после последнего недельного – ежедневные)
        rep_ = dd.build(34)
        num = [(d['date'], d['s'].get('revenue') or 0.0, dd._profit_before_ads(d['s']) - (d.get('ads') or 0.0))
               for d in sorted(rep_['days'], key=lambda x: x['date']) if d['date'] <= last]
        series = []
        for i, (dt, rv, pr) in enumerate(num):
            w = num[max(0, i - 6):i + 1]; R7 = sum(x[1] for x in w); P7 = sum(x[2] for x in w)
            series.append({'d': dt, 'rev': round(rv), 'op': round(pr / rv * 100, 2) if rv else None,
                           'op7r': round(P7 / R7 * 100, 2) if (R7 and len(w) == 7) else None})
        store = {'op7': round(op7, 2) if op7 is not None else None, 'op7_prev': round(op7p, 2) if op7p is not None else None,
                 # выручка в день – продажи в ценах продавца (до СПП) по финотчётам: цель кампании 1,2 млн ₽ в день
                 'rev7_day': round(rev7 / days), 'rev7_prev_day': round(rev7p / days),
                 # отставание окна от «последних полных 7 календарных дней»: 0 – окно кончается вчера; >0 – отчёт за вчера ещё не пришёл
                 'op7_lag': (today_msk() - datetime.timedelta(days=1) - end).days,
                 'op7_from': start.isoformat(), 'op7_to': end.isoformat(), 'op7_prev_from': ps.isoformat(), 'op7_prev_to': pe.isoformat()}
        return by, store, series[-28:]
    finally:
        os.chdir(cwd)


def fin_weekly_op(fdir, weeks=12):
    """ОП магазина по неделям (пн–вс) за `weeks` недель, закрытых финотчётами: [{from, to, op, rev}]."""
    cwd = os.getcwd(); sys.path.insert(0, fdir); os.chdir(fdir); out = []
    try:
        from app.reports.wb_digit import wb_period_sku, last_fact_date
        lf = last_fact_date()
        if not lf: return []
        end = lf - datetime.timedelta(days=(lf.weekday() - 6) % 7)        # последнее воскресенье с закрытым отчётом
        for i in range(weeks - 1, -1, -1):
            e = end - datetime.timedelta(days=7 * i); b = e - datetime.timedelta(days=6)
            res = wb_period_sku(b, e, estimate=False)
            rev = sum(m['revenue'] or 0 for m in res.values()); mar = sum(m['margin'] or 0 for m in res.values())
            out.append({'from': b.isoformat(), 'to': e.isoformat(), 'op': round(mar / rev * 100, 2) if rev else None, 'rev': round(rev)})
    finally:
        os.chdir(cwd)
    return out


def fin_plan(c, today):
    mk = today.strftime('%Y-%m'); m0 = today.replace(day=1).isoformat()
    plan = {str(r['article']): {'plan': r['plan_rub'], 'buyout_plan': r['buyout_pct']} for r in c.execute(
        "SELECT article, plan_rub, buyout_pct FROM sales_plan WHERE month_key=? AND plan_rub>0", (mk,))}
    fact = {str(r['a']): r['rub'] or 0.0 for r in c.execute(
        "SELECT seller_sku a, SUM(sign*price_with_disc) rub FROM wb_sales WHERE date>=? GROUP BY seller_sku", (m0,))}
    for a, p in plan.items(): p['fact'] = fact.get(a, 0.0)
    return plan


# ---------------------------------------------------------------- сборка
def fetch():
    e = env(); today = today_msk()
    last_day = calendar.monthrange(today.year, today.month)[1]
    days_left = last_day - today.day + 1
    anomaly = []
    try: anomaly = json.load(open(os.path.join(DIR, 'plan.json'), encoding='utf-8')).get('anomaly_days', [])
    except Exception: pass
    prev = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
    notes = []
    c = fdir = None
    try: c, fdir = fin_db(e)
    except Exception as ex: notes.append(f'финансовый сервис недоступен: {ex}')
    # план
    plan, plan_src, ops = {}, None, {}
    err = None
    for attempt in range(3):                       # Google иногда отвечает «Service Spreadsheets timed out» – повторяем
        try: plan, plan_src, ops = sheet_plan(e); err = None; break
        except LookupError as ex: err = ex; break
        except Exception as ex:
            err = ex
            if attempt < 2: import time; time.sleep(20)
    if err is not None:
        notes.append(f'таблица план-факта: {err}')
        if c is not None:
            plan, plan_src = fin_plan(c, today), 'копия финансового сервиса (sales_plan)'
        # ОП за месяц (H) есть только в таблице – оставляем прошлое значение
        ops = {a: x['op_month'] / 100 for a, x in (prev.get('by_art') or {}).items() if x.get('op_month') is not None}
    # выкуп, чек, темп
    buy, chk = (fin_orders(c, today, anomaly) if c is not None else ({}, {}))
    store, drr, cpo = ({}, {}, {})
    if c is not None:
        try: store, drr = fin_drr(c, today)
        except Exception as ex: notes.append(f'ДРР: {ex}')
        try: cpo = fin_cpo(c, today)
        except Exception as ex: notes.append(f'CPO: {ex}')
    tb = sum(v[1] for v in buy.values()); tn = sum(v[0] for v in buy.values())
    buy_all = (tb / tn) if tn else None
    # маржа
    mg, m_from, m_to = {}, None, None
    mg14, m14_from, m14_to, store14 = {}, None, None, {}
    if fdir:
        # ОП за 14 дней (два последних недельных финотчёта) и предыдущие 14 дней – для страницы ОП (решение владельца 30.09.2026);
        # 28 дней считаются последними: FIN_TOTAL остаётся за 28 дней, как раньше
        try:
            mg14, m14_from, m14_to = fin_margin(fdir, 14); T14 = dict(FIN_TOTAL)
            _, p14_from, p14_to = fin_margin(fdir, 14, shift=14); P14 = dict(FIN_TOTAL)
            if T14.get('rev'):
                store14 = {'op14': round(T14['margin'] / T14['rev'] * 100, 2), 'rev14_day': round(T14['rev'] / 14), 'margin14_day': round(T14['margin'] / 14),
                           'op14_from': m14_from, 'op14_to': m14_to}
                if P14.get('rev'): store14.update({'op14_prev': round(P14['margin'] / P14['rev'] * 100, 2), 'op14_prev_from': p14_from, 'op14_prev_to': p14_to,
                                                   'rev14_prev_day': round(P14['rev'] / 14)})
        except Exception as ex: notes.append(f'маржа 14 дн.: {type(ex).__name__}: {ex}')
        try: mg, m_from, m_to = fin_margin(fdir)
        except Exception as ex: notes.append(f'маржа: {type(ex).__name__}: {ex}')
    prices = {}
    try: prices = {str(k): v for k, v in json.load(open(os.path.join(DIR, 'prices.json'), encoding='utf-8')).items() if not str(k).startswith('_')}
    except Exception: pass

    by = {}
    arts = set(plan) | set(mg) | set(ops) | set(drr)
    for a in arts:
        rec = {}
        p = plan.get(a)
        # выкуп
        n, b = buy.get(a, (0, 0)); src = 'факт'
        if n < 30 and base_art(a) and buy.get(base_art(a), (0, 0))[0] >= 30:
            n, b = buy[base_art(a)]; src = f'факт {base_art(a)}'
        if n >= 30: bo = b / n
        elif p and p.get('buyout_plan'): bo, src = p['buyout_plan'], 'план (U)'
        else: bo, src = buy_all, 'средний по кабинету'
        # чек и темп
        avg, on = chk.get(a, (None, 0))
        check = avg or prices.get(a)
        rec.update({'buyout': round(bo, 3) if bo else None, 'buyout_src': src, 'check': round(check) if check else None,
                    'orders_7d': round(on / 7, 1)})
        if p:
            left = max(0.0, p['plan'] - (p.get('fact') or 0.0))
            need = (left / (check * bo) / days_left) if (check and bo and days_left > 0) else None
            rec.update({'plan': round(p['plan']), 'fact': round(p.get('fact') or 0.0), 'left': round(left),
                        'done_pct': round((p.get('fact') or 0.0) / p['plan'] * 100, 1) if p['plan'] else None,
                        'need': round(need, 1) if need is not None else None})
        g = mg.get(a)
        if g and (g['rev'] or g['margin']):
            rec.update({'margin': round(g['margin']), 'margin_pct': round(g['margin'] / g['rev'] * 100, 1) if g['rev'] else None,
                        'rev': round(g['rev']), 'qty': g['qty'], 'per_unit': round(g['margin'] / g['qty']) if g['qty'] else None,
                        'no_cost': g.get('no_cost', False),
                        # для оценки маржи при другой цене: средняя цена продажи до СПП и доля расходов, зависящих от цены
                        'avg_price': round(g['rev'] / g['qty']) if g['qty'] and g['qty'] > 0 else None,
                        'var_pct': round(g['var'] / g['rev'], 4) if g['rev'] else None})
        g14 = mg14.get(a)
        if g14 and g14['rev']:
            rec.update({'margin14': round(g14['margin']), 'margin14_pct': round(g14['margin'] / g14['rev'] * 100, 1), 'no_cost14': g14.get('no_cost', False),
                        'rev14': round(g14['rev']), 'qty14': g14['qty'],
                        'avg_price14': round(g14['rev'] / g14['qty']) if g14['qty'] and g14['qty'] > 0 else None,
                        'var_pct14': round(g14['var'] / g14['rev'], 4)})
        if a in ops: rec['op_month'] = round(ops[a] * 100, 1)       # ОП за месяц из план-факта (H), %
        if a in drr: rec.update({'drr7': drr[a]['drr7'], 'drr_base': drr[a]['drr_base'], 'ads7': drr[a]['ads7']})
        by[a] = rec
    if not by and prev:
        sys.exit('[pm] ничего не собрано: ' + '; '.join(notes) + ' – оставляю прошлый файл')
    store.update(store14)
    # динамика для плиток: дни (28) и ОП по неделям (12)
    if c is not None:
        try: store['daily'] = fin_daily(c, today, cpo)
        except Exception as ex: notes.append(f'динамика по дням: {ex}')
    if fdir:
        try:
            op7_by, store7, op_series = fin_op7(fdir)
            store.update(store7)
            if op_series: store.setdefault('daily', {})['op_series'] = op_series
            for a, x in op7_by.items():
                if a in by and x['rev']:
                    by[a].update({'margin7': round(x['margin']), 'margin7_pct': round(x['margin'] / x['rev'] * 100, 1), 'rev7': round(x['rev']),
                                  'qty7': x['qty'], 'no_cost7': x['no_cost'],
                                  'avg_price7': round(x['rev'] / x['qty']) if x['qty'] > 0 else None, 'var_pct7': round(x['var'] / x['rev'], 4)})
        except Exception as ex: notes.append(f'ОП 7 дней: {type(ex).__name__}: {ex}')
        # 14 дней по тем же ежедневным отчётам – запасная основа для решений, когда за 7 дней мало выручки (с 13.10.2026)
        try:
            op14d_by, store14d, _ = fin_op7(fdir, days=14)
            store.update({'op14d': store14d.get('op7'), 'op14d_from': store14d.get('op7_from'), 'op14d_to': store14d.get('op7_to')})
            for a, x in op14d_by.items():
                if a in by and x['rev']:
                    by[a].update({'margin14d_pct': round(x['margin'] / x['rev'] * 100, 1), 'rev14d': round(x['rev']), 'qty14d': x['qty'], 'no_cost14d': x['no_cost'],
                                  'avg_price14d': round(x['rev'] / x['qty']) if x['qty'] > 0 else None, 'var_pct14d': round(x['var'] / x['rev'], 4)})
        except Exception as ex: notes.append(f'ОП 14 дней по дням: {type(ex).__name__}: {ex}')
        try: store['weekly_op'] = fin_weekly_op(fdir)
        except Exception as ex: notes.append(f'ОП по неделям: {type(ex).__name__}: {ex}')
    if FIN_TOTAL.get('rev'):
        store.update({'op28': round(FIN_TOTAL['margin'] / FIN_TOTAL['rev'] * 100, 2), 'rev28_day': round(FIN_TOTAL['rev'] / 28),
                      'margin28_day': round(FIN_TOTAL['margin'] / 28)})
    out = {'date': today.isoformat(), 'month': today.strftime('%Y-%m'), 'days_left': days_left, 'plan_src': plan_src, 'store': store,
           'margin_from': m_from, 'margin_to': m_to, 'margin14_from': m14_from, 'margin14_to': m14_to, 'buyout_window': '45–14 дней назад', 'buyout_all': round(buy_all, 3) if buy_all else None,
           'notes': notes, 'by_art': by, 'cpo_daily': cpo}
    tmp = OUT + '.tmp'; json.dump(out, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=0); os.replace(tmp, OUT)
    np_ = sum(1 for r in by.values() if 'need' in r); nm = sum(1 for r in by.values() if 'margin' in r)
    print(f'[pm] план {plan_src}: {np_} артикулов, до конца месяца {days_left} дн.; маржа {m_from}..{m_to}: {nm} артикулов'
          + (f'; {"; ".join(notes)}' if notes else ''))


def inject(page):
    src = open(page, encoding='utf-8').read()
    m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
    if not m: raise SystemExit('[pm] не нашёл const D=... в странице')
    D = json.loads(m.group(1))
    P = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
    by = P.get('by_art', {})
    D['pm_meta'] = {k: P.get(k) for k in ('date', 'month', 'days_left', 'plan_src', 'margin_from', 'margin_to', 'margin14_from', 'margin14_to', 'buyout_window', 'buyout_all')} if P else None
    D['store_kpi'] = P.get('store') if P else None
    if P:                                       # средняя доля расходов, зависящих от цены – для оценки маржи при новой цене на странице
        vs = [x['var_pct'] for x in by.values() if x.get('var_pct') and not x.get('no_cost')]
        D['pm_meta']['var_avg'] = round(sum(vs) / len(vs), 4) if vs else 0.35
    for r in D['rows']:
        x = by.get(r['a']) or {}
        r['pmf'] = x or None
        r['need'] = x.get('need'); r['mg'] = x.get('margin'); r['mgp'] = x.get('margin_pct'); r['opm'] = x.get('op_month'); r['mgp14'] = x.get('margin14_pct')
        # ОП за 7 дней по ежедневным финотчётам; мало выручки за 7 дней (< 20 000 ₽) – берём 14 дней, иначе цифра прыгает
        r['mgp7'] = x.get('margin7_pct') if (x.get('rev7') or 0) >= 20000 else None; r['mgp14d'] = x.get('margin14d_pct')
        r['mgp7_fb'] = r['mgp7'] is None and (x.get('margin14d_pct') if x.get('margin14d_pct') is not None else x.get('margin14_pct')) is not None
    new = src[:m.start(1)] + json.dumps(D, ensure_ascii=False) + src[m.end(1):]
    open(page + '.tmp', 'w', encoding='utf-8').write(new); os.replace(page + '.tmp', page)
    print(f"[pm] в страницу вставлено: план у {sum(1 for r in D['rows'] if r.get('need') is not None)}, маржа у {sum(1 for r in D['rows'] if r.get('mg') is not None)} из {len(D['rows'])}")


def refresh7():
    """Только ОП за 7 дней по ежедневным финотчётам и ОП по дням – без таблиц и плана (для подтяжки днём, если утром
    отчёта за вчера ещё не было). Пишет в plan_margin.json те же поля, что fetch."""
    e = env(); fdir = e.get('FIN_DIR', '/opt/mp-finance')
    P = json.load(open(OUT, encoding='utf-8'))
    op7_by, store7, op_series = fin_op7(fdir)
    if not store7: sys.exit('[pm] refresh7: нет ежедневных финотчётов')
    P.setdefault('store', {}).update(store7)
    if op_series: P['store'].setdefault('daily', {})['op_series'] = op_series
    by = P.get('by_art') or {}
    for a, x in op7_by.items():
        if a in by and x['rev']:
            by[a].update({'margin7': round(x['margin']), 'margin7_pct': round(x['margin'] / x['rev'] * 100, 1), 'rev7': round(x['rev']),
                          'qty7': x['qty'], 'no_cost7': x['no_cost'],
                          'avg_price7': round(x['rev'] / x['qty']) if x['qty'] > 0 else None, 'var_pct7': round(x['var'] / x['rev'], 4)})
    tmp = OUT + '.tmp'; json.dump(P, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False); os.replace(tmp, OUT)
    print(f"[pm] refresh7: ОП {store7.get('op7')}% за {store7.get('op7_from')}–{store7.get('op7_to')}, отставание {store7.get('op7_lag')} дн.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('fetch'); sub.add_parser('refresh7')
    a2 = sub.add_parser('inject'); a2.add_argument('--page', default='page.html')
    A = ap.parse_args()
    if A.cmd == 'fetch': fetch()
    elif A.cmd == 'refresh7': refresh7()
    else: inject(A.page)
