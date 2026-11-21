#!/usr/bin/env python3
"""Обратная связь по рекомендациям цен: поставил сотрудник цену или нет — и учёт этого в движке.

Серверный аналог команды «цены поднял» из версии для Claude AI. Три роли одного файла:

  1. Библиотека для меню отчётов (/opt/wb-control/app.py): mark() / state_for() —
     кнопки «Поставил» / «Не ставлю» на странице пишут отметки в feedback.json.
  2. `feedback.py apply` — ПЕРЕД движком. Берёт рекомендации прошлого прогона
     (recommendations.json), сверяет их с фактическими ценами кабинета (API цен) и с отметками:
       • «Поставил» и кабинет показывает новую цену  → шаг записывается в price_history
         днём рекомендации (как делала команда «цены поднял»): движок видит шаг сразу
         и открывает окно наблюдения, а не предлагает второй шаг поверх;
       • отметки нет, но цена в кабинете уже новая   → то же самое, «поставлено (по кабинету)»;
       • «Поставил», а в кабинете старая цена        → «не подтверждено», история не трогается;
       • стоит другая цена                            → она и записывается в историю;
       • ничего не поставлено                         → «не поставлено», движок повторит рекомендацию.
     Отметка «Не ставлю» с датой превращается в holds → движок (wb_daily.py --feedback) держит
     артикул до даты: «Отложено до …», без подъёмов и откатов.
  3. `feedback.py inject --page page.html` — ПОСЛЕ движка. Записывает рекомендации сегодняшнего
     прогона в recommendations.json (по ним завтра пойдёт сверка) и вставляет в страницу D.feedback:
     сегодняшние отметки, итоги вчерашней сверки, действующие «отложено».

Файлы (в папке скрипта): feedback.json — отметки, holds, итоги сверки; recommendations.json —
рекомендации по дням (60 дней); price_history.json — правится только в apply.
"""
import json, os, sys, fcntl, datetime, argparse, subprocess, re

DIR = os.path.dirname(os.path.abspath(__file__))
FB_FILE = os.path.join(DIR, 'feedback.json')
RECS_FILE = os.path.join(DIR, 'recommendations.json')
HIST_FILE = os.path.join(DIR, 'price_history.json')
LOCK_FILE = os.path.join(DIR, '.feedback.lock')
KINDS = ('up', 'back', 'follow')                      # рекомендации, по которым есть что ставить

# причины «не ставлю»: подпись и на сколько дней отложить артикул (0 — не откладывать)
REASONS = {
    'promo': ('Акция WB — не хочу выходить', 14),
    'few':   ('Мало заказов по артикулу — оставляю старую цену', 7),
    'plan':  ('Заказы и ОП выполняют план — оставляю как есть', 7),
    'stock': ('Артикул заканчивается на остатках — спад заказов', 7),
    'ad':    ('Завели в рекламу — хочу подождать', 7),
    'other': ('Другое', 7),
}
STATUS_RU = {
    'set_ok':          'поставлено, кабинет подтвердил',
    'set_api':         'поставлено (по кабинету, без отметки)',
    'set_unconfirmed': 'отмечено «поставил», но в кабинете старая цена',
    'set_other':       'поставлена другая цена',
    'set_nocheck':     'поставлено по отметке (сверка с кабинетом не удалась)',
    'not_set':         'не поставлено',
    'changed_other':   'цена изменилась не по рекомендации',
    'skipped':         'отложено по отметке',
    'unknown':         'нет сверки',
}


def now_msk():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Europe/Moscow'))
    except Exception:
        return datetime.datetime.now()


def _load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _save(path, obj, **kw):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, **kw)
    os.replace(tmp, path)


class locked:
    """Файловая блокировка: страница пишет отметки в любое время, прогон — в 07:00."""
    def __enter__(self):
        self.f = open(LOCK_FILE, 'w'); fcntl.flock(self.f, fcntl.LOCK_EX); return self
    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


def load_fb():
    fb = _load(FB_FILE, {})
    fb.setdefault('marks', {}); fb.setdefault('holds', {}); fb.setdefault('results', {})
    return fb


def load_recs():
    return _load(RECS_FILE, {})


def latest_run(recs):
    return max(recs) if recs else None


def near(a, b):
    """Цены совпадают с точностью до округления скидки (0,6% или 3 ₽)."""
    return a is not None and b is not None and abs(a - b) <= max(3, abs(b) * 0.006)


def classify(actual, prev, new):
    """Что стоит в кабинете: 'prev' — старая цена, 'new' — рекомендованная, 'other' — иная.
    Шаг бывает меньше допуска округления (пара рублей), поэтому сначала смотрим, к какой
    из двух цен фактическая ближе, и только потом — попадает ли она в допуск."""
    if actual is None: return None
    dp, dn = abs(actual - prev), abs(actual - new)
    if dp <= dn: return 'prev' if near(actual, prev) else 'other'
    return 'new' if near(actual, new) else 'other'


# ---------------------------------------------------------------- отметки со страницы
def own_row(a):
    """Запись для «своей цены» по артикулу, которому движок сегодня ничего не рекомендовал (kind='own'):
    текущая цена со скидкой, цена в кабинете и скидка – из page.html прогона (та же папка)."""
    try:
        src = open(os.path.join(DIR, 'page.html'), encoding='utf-8').read()
        m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
        r = next((x for x in json.loads(m.group(1))['rows'] if str(x['a']) == str(a)), None)
    except Exception:
        return None
    if not r or r.get('disc') is None or not r.get('p'): return None
    return {'kind': 'own', 'new': r['p'], 'nfull': r.get('pfull') or round(r['p'] / (1 - r['disc'] / 100)), 'p': r['p'],
            'act': 'Своя цена', 'exit': False, 'cap': r.get('cap'), 'disc': r.get('disc'), 'pfull': r.get('pfull'), 'own': True}


def mark(run, arts, status, reason=None, until=None, note=None, price_full=None):
    # price_full — «своя цена»: цена ДО скидки продавца, которую сотрудник вписал вместо рекомендованной
    # (шаг алгоритма показался слишком малым). Переводится в цену со скидкой по скидке артикула и становится
    # ожидаемой ценой при завтрашней сверке: сверка запишет её в историю цен как фактический шаг.
    """Отметка сотрудника. status: set | skip | clear | unhold. Возвращает state_for(run)."""
    if isinstance(arts, str): arts = [arts]
    arts = [str(a).strip() for a in arts if str(a).strip()]
    if not arts: raise ValueError('не указан артикул')
    with locked():
        recs = load_recs(); fb = load_fb()
        latest = latest_run(recs)
        if status != 'unhold':
            if not latest or run != latest:
                raise ValueError(f'отмечать можно только рекомендации последнего прогона ({latest or "ещё не было"})')
            for a in arts:
                if a not in recs[run] and status == 'set' and price_full:
                    # своя цена по артикулу без рекомендации: текущая цена и скидка – из страницы этого прогона
                    r = own_row(a)
                    if r: recs[run][a] = r; _save(RECS_FILE, recs, indent=1)
                if a not in recs[run]:
                    raise ValueError(f'артикул {a} не в рекомендациях {run}')
        ts = now_msk(); today = ts.date().isoformat()
        marks = fb['marks'].setdefault(run, {})
        for a in arts:
            if status == 'unhold':
                fb['holds'].pop(a, None)
                for r, mm in fb['marks'].items():
                    if mm.get(a, {}).get('status') == 'skip': mm[a]['until'] = None
                continue
            rec = recs[run][a]
            if status == 'set':
                m = {'status': 'set', 'ts': ts.isoformat(timespec='minutes'), 'kind': rec['kind'],
                     'rec': rec['new'], 'rec_full': rec.get('nfull'), 'prev': rec['p']}
                if price_full:
                    try: pf = float(str(price_full).replace(' ', '').replace(',', '.'))
                    except ValueError: raise ValueError(f'своя цена {price_full!r} — не число')
                    disc = rec.get('disc')
                    if disc is None: raise ValueError(f'у {a} неизвестна скидка продавца — свою цену не пересчитать')
                    base = rec.get('nfull') or rec['p'] / (1 - disc / 100)
                    if not (base * 0.5 <= pf <= base * 3): raise ValueError(f'своя цена {pf:.0f} ₽ для {a} слишком далека от рекомендованной {base:.0f} ₽ (до скидки) — проверьте число')
                    m.update({'custom': True, 'rec_full': round(pf), 'rec': round(pf * (1 - disc / 100)), 'disc': disc})
                marks[a] = m
                if fb['holds'].get(a, {}).get('run') == run: fb['holds'].pop(a, None)
            elif status == 'skip':
                key = reason if reason in REASONS else 'other'
                why, days = REASONS[key]
                if until:
                    try: datetime.date.fromisoformat(until)
                    except ValueError: raise ValueError(f'дата {until!r} не в формате ГГГГ-ММ-ДД')
                elif days:
                    until = (ts.date() + datetime.timedelta(days=days)).isoformat()
                else:
                    until = None
                note = (note or '').strip()[:200] or None
                marks[a] = {'status': 'skip', 'ts': ts.isoformat(timespec='minutes'), 'kind': rec['kind'],
                            'rec': rec['new'], 'rec_full': rec.get('nfull'), 'prev': rec['p'],
                            'reason': key, 'why': why, 'until': until, 'note': note}
                if until:
                    fb['holds'][a] = {'until': until, 'why': why + (f': {note}' if note else ''), 'from': today, 'run': run}
                else:
                    if fb['holds'].get(a, {}).get('run') == run: fb['holds'].pop(a, None)
            elif status == 'clear':
                marks.pop(a, None)
                if recs[run].get(a, {}).get('own'): recs[run].pop(a, None); _save(RECS_FILE, recs, indent=1)   # своя цена снята – убираем и запись
                if fb['holds'].get(a, {}).get('run') == run: fb['holds'].pop(a, None)
            else:
                raise ValueError(f'неизвестный статус {status!r}')
        _save(FB_FILE, fb, indent=1)
    return state_for(run)


def state_for(run=None):
    """Что показать странице: отметки прогона, действующие «отложено», причины."""
    recs = load_recs(); fb = load_fb()
    latest = latest_run(recs)
    run = run or latest
    today = now_msk().date().isoformat()
    holds = {a: h for a, h in fb['holds'].items() if h.get('until') and h['until'] > today}
    return {'run': run, 'latest': latest, 'editable': bool(latest) and run == latest,
            'marks': fb['marks'].get(run, {}), 'holds': holds,
            'reasons': {k: {'label': v[0], 'days': v[1]} for k, v in REASONS.items()}}


# ---------------------------------------------------------------- сверка и учёт (до движка)
def fetch_prices(token):
    """Фактические цены кабинета {артикул: цена со скидкой} — тот же метод, что в wb_daily.py."""
    out = {}; off = 0
    while True:
        cmd = ['curl', '-sS', '-G', 'https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter',
               '-H', f'Authorization: {token}', '--max-time', '120',
               '--data-urlencode', 'limit=1000', '--data-urlencode', f'offset={off}']
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f'API цен: {r.stderr.strip()[:200] or "пустой ответ"}')
        g = json.loads(r.stdout)['data']['listGoods']
        for it in g:
            sz = (it.get('sizes') or [{}])[0]
            if sz.get('discountedPrice'):
                out[str(it['vendorCode'])] = round(sz['discountedPrice'])
        if len(g) < 1000: break
        off += 1000
    return out


def apply(today=None, use_api=True, token=None):
    today = today or now_msk().date().isoformat()
    with locked():
        recs = load_recs(); fb = load_fb()
        pending = sorted(r for r in recs if r < today and r not in fb['results'])
        if not pending:
            print(f'[feedback] прогонов для сверки нет (сегодня {today})'); return 0
        prices = {}; api_ok = False
        if use_api:
            try:
                prices = fetch_prices(token or os.environ.get('WB_TOKEN', '')); api_ok = bool(prices)
            except Exception as e:
                print(f'[feedback] сверка с кабинетом не удалась: {e}', file=sys.stderr)
        hist = _load(HIST_FILE, {})
        patched = 0
        for run in pending:
            res = {'run': run, 'checked': today, 'api': api_ok, 'arts': {}}
            marks = fb['marks'].get(run, {})
            for a, rec in recs[run].items():
                m = marks.get(a); new, prev = rec['new'], rec['p']
                if m and m.get('custom') and m.get('rec'): new = m['rec']      # своя цена сотрудника — её и ждём в кабинете
                actual = prices.get(a) if api_ok else None
                cls = classify(actual, prev, new) if api_ok else None
                st = None; write = None
                if m and m['status'] == 'skip':
                    st = 'skipped'
                    # «не ставлю», а цена в кабинете всё же новая — значит поставили: считаем шаг
                    if cls == 'new':     st, write = 'set_api', new
                    elif cls == 'other': st = 'changed_other'
                elif m and m['status'] == 'set':
                    if cls is None:      st, write = 'set_nocheck', new
                    elif cls == 'new':   st, write = 'set_ok', new
                    elif cls == 'prev':  st = 'set_unconfirmed'
                    else:                st, write = 'set_other', actual
                else:
                    if cls is None:      st = 'unknown'
                    elif cls == 'new':   st, write = 'set_api', new
                    elif cls == 'prev':  st = 'not_set'
                    else:                st = 'changed_other'
                if write is not None:
                    day = hist.setdefault(run, {})
                    if day.get(a) != write:
                        day[a] = write; patched += 1
                res['arts'][a] = {'status': st, 'kind': rec['kind'], 'rec': new, 'rec_full': (m['rec_full'] if m and m.get('custom') else rec.get('nfull')),
                                  'custom': bool(m and m.get('custom')), 'exit': bool(rec.get('exit')),
                                  'prev': prev, 'actual': actual, 'mark': (m['status'] if m else None),
                                  'until': (m.get('until') if m else None)}
            fb['results'][run] = res
            cnt = {}
            for v in res['arts'].values(): cnt[v['status']] = cnt.get(v['status'], 0) + 1
            print(f"[feedback] рекомендации {run}: " + ', '.join(f'{STATUS_RU[k]} {v}' for k, v in sorted(cnt.items(), key=lambda x: -x[1]))
                  + (' (сверка с кабинетом)' if api_ok else ' (без сверки с кабинетом)'))
        if patched:
            _save(HIST_FILE, hist, indent=0, sort_keys=True)
            print(f'[feedback] в историю цен записано шагов: {patched}')
        # итоги старше 60 дней не нужны
        for r in [r for r in fb['results'] if r < (datetime.date.fromisoformat(today) - datetime.timedelta(days=60)).isoformat()]:
            fb['results'].pop(r, None); fb['marks'].pop(r, None)
        _save(FB_FILE, fb, indent=1)
    return 0


# ---------------------------------------------------------------- журнал и вставка в страницу (после движка)
def inject(page):
    src = open(page, encoding='utf-8').read()
    m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
    if not m: raise SystemExit('[feedback] не нашёл const D=... в странице')
    D = json.loads(m.group(1)); today = D['today']
    with locked():
        recs = load_recs()
        keep = {a: x for a, x in (recs.get(today) or {}).items() if x.get('own')}   # «свои цены» за сегодня не теряем при перерисовке
        recs[today] = {r['a']: {'kind': r['kind'], 'new': r['new'], 'nfull': r.get('nfull'), 'p': r['p'], 'act': r['act'],
                                'exit': bool(r.get('exit')), 'cap': r.get('cap'), 'disc': r.get('disc'), 'pfull': r.get('pfull')}
                       for r in D['rows'] if r['kind'] in KINDS and r.get('new')}
        for a, x in keep.items(): recs[today].setdefault(a, x)
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=60)).isoformat()
        for r in [r for r in recs if r < cutoff]: recs.pop(r)
        _save(RECS_FILE, recs, indent=1)
        fb = load_fb()
    prev_runs = sorted(r for r in fb['results'] if r < today)
    prev = fb['results'][prev_runs[-1]] if prev_runs else None
    if prev:
        cnt = {}
        for v in prev['arts'].values(): cnt[v['status']] = cnt.get(v['status'], 0) + 1
        prev = {**prev, 'counts': cnt, 'total': len(prev['arts'])}
    holds = {a: h for a, h in fb['holds'].items() if h.get('until') and h['until'] > today}
    D['feedback'] = {'run': today, 'api_path': '/run/feedback', 'marks': fb['marks'].get(today, {}),
                     'holds': holds, 'prev': prev, 'status_ru': STATUS_RU,
                     'reasons': {k: {'label': v[0], 'days': v[1]} for k, v in REASONS.items()}}
    for r in D['rows']:
        r['fb'] = D['feedback']['marks'].get(r['a'])
        r['fb_prev'] = (prev['arts'].get(r['a']) if prev else None)
    new = src[:m.start(1)] + json.dumps(D, ensure_ascii=False) + src[m.end(1):]
    tmp = page + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(new); os.replace(tmp, page)
    print(f"[feedback] рекомендаций за {today} записано: {len(recs[today])}; отложено артикулов: {len(holds)}"
          + (f"; итоги сверки {prev['run']}: {prev['total']} рекомендаций" if prev else ''))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    a1 = sub.add_parser('apply', help='до движка: сверка вчерашних рекомендаций и запись шагов в историю цен')
    a1.add_argument('--today', help='дата прогона (для тестов), по умолчанию сегодня по Москве')
    a1.add_argument('--no-api', action='store_true', help='не ходить в API цен, верить отметкам')
    a2 = sub.add_parser('inject', help='после движка: журнал рекомендаций + D.feedback в страницу')
    a2.add_argument('--page', default='page.html')
    a3 = sub.add_parser('mark', help='отметка из командной строки (для тестов)')
    a3.add_argument('run'); a3.add_argument('status'); a3.add_argument('arts', nargs='+')
    a3.add_argument('--reason'); a3.add_argument('--until'); a3.add_argument('--note')
    a4 = sub.add_parser('state', help='текущее состояние (json)')
    a4.add_argument('--run')
    A = ap.parse_args()
    if A.cmd == 'apply': sys.exit(apply(A.today, not A.no_api))
    if A.cmd == 'inject': inject(A.page)
    if A.cmd == 'mark': print(json.dumps(mark(A.run, A.arts, A.status, A.reason, A.until, A.note), ensure_ascii=False, indent=1))
    if A.cmd == 'state': print(json.dumps(state_for(A.run), ensure_ascii=False, indent=1))
