#!/usr/bin/env python3
"""Подсказка Grok за сегодня для страницы «Подъём цен WB».

Запускается после wb_daily.py и до publish.py. Берёт данные из собранной страницы
(строка `const D={...};`), просит Grok 4.7 High Fast (подписка Cursor, ключ
CURSOR_API_KEY) написать комментарий за сегодня, дописывает его первым элементом
в comments.json и вставляет весь журнал в страницу (поле D.comments – его рисует
блок «Комментарий Grok» в page_template.html).

Движок wb_daily.py и его решения не трогает: комментарий – это чтение готовых данных.
Инструментов у модели нет, файлы она не меняет.

Коды выхода: 0 – комментарий добавлен; 3 – за сегодня уже есть (журнал вставлен
в страницу, ничего нового); 1 – Grok не ответил (в страницу вставлена история,
прогон может продолжаться без нового комментария).

  python3 comment.py --page page.html --comments comments.json
  python3 comment.py --dry-run          напечатать промт и ответ, ничего не менять
  python3 comment.py --inject-only      только вставить журнал в страницу, к Grok не ходить
"""
import json, sys, re, argparse, os, signal, tempfile

ap = argparse.ArgumentParser()
ap.add_argument('--page', default='page.html')
ap.add_argument('--comments', default='comments.json')
ap.add_argument('--model', default='grok-4.7')
ap.add_argument('--timeout', type=int, default=420)
ap.add_argument('--dry-run', action='store_true', help='напечатать промт и ответ, журнал и страницу не менять')
ap.add_argument('--inject-only', action='store_true', help='только вставить журнал в страницу, к Grok не обращаться')
A = ap.parse_args()
A.page = os.path.abspath(A.page)
A.comments = os.path.abspath(A.comments)

def log(msg): print(f'[comment] {msg}', file=sys.stderr)

# --- данные страницы ---
src = open(A.page, encoding='utf-8').read()
m = re.search(r'^const D=(\{.*\});\s*$', src, re.M)
if not m:
    log('не нашёл const D=... в странице'); sys.exit(1)
D = json.loads(m.group(1))
today = D['today']

cj = json.load(open(A.comments, encoding='utf-8')) if os.path.exists(A.comments) else {'comments': []}
cj.setdefault('comments', [])

def inject(comments, err=None):
    """Вставить журнал в страницу: D.comments = [...] (сначала свежие); err – почему комментарий за сегодня не создан."""
    lst = sorted([c for c in comments if c.get('date') and c.get('text')],
                 key=lambda c: c['date'], reverse=True)
    D['comments'] = lst
    D['comment_error'] = err if not any(c['date'] == today for c in lst) else None
    new = src[:m.start(1)] + json.dumps(D, ensure_ascii=False) + src[m.end(1):]
    tmp = A.page + '.tmp'
    open(tmp, 'w', encoding='utf-8').write(new)
    os.replace(tmp, A.page)
    log(f'в страницу вставлено записей: {len(lst)}')

if A.inject_only:
    inject(cj['comments']); sys.exit(0)

if any(c.get('date') == today for c in cj['comments']):
    log(f'за {today} комментарий уже есть — пропускаю')
    inject(cj['comments']); sys.exit(3)

# --- сводка для промта ---
def pct(x):
    return '—' if x is None else f'{x:+.1f}%'
def rub(x):
    return '—' if x is None else f'{round(x):,}'.replace(',', ' ') + ' ₽'
zone_ru = {'green': 'зелёная', 'yellow': 'жёлтая (шаги половинные)', 'red': 'красная (подъёмы остановлены)'}.get(D['zone'], D['zone'])
c = D['counts']
rows = D['rows']
lines = []
OPM = bool(D.get('op_rule'))   # кампания «ОП 15%»: цель – ОП и заказы магазина, уровень цен к июню только справочно
lines.append(f"Дата отчёта {today}, последний закрытый день {D['last']}. Кампания подъёма цен на товары WB с {D['start']} до {D['end']}, идёт {D['camp_days']} дн., осталось шагов {D['rem_steps']}. Всего артикулов в таблице {len(rows)}.")
if not OPM: lines.append(f"Уровень цен к июню {pct(D['level'])} (цель +{D['goal']}%). Средняя цена июня {rub(D['june_price'])}, целевая {rub(D['june_price']*(1+D['goal']/100))}.")
lines.append(f"Поток: {D['flow_now']} заказов/день за 7 дней" + ('' if OPM else f" против {D['june_day']} в июне ({pct(D['flow_abs'])} — в основном сезон)") + f". К базе кампании {pct(D['drop_now'])}; израсходовано {D['spent']}% лимита падения −{D['floor']}% — зона {zone_ru} (жёлтая от {D['yel']}%, красная от {D['red']}%). " + (f"Сезон по частотности поисковых запросов WB (весь рынок «товарыь»): последние 7 дней к среднему за 45 дней {pct(D['season'])}; контрольной группы нет, все артикулы в подъёме." if D.get('season_src')=='frequency' else f"Сезон по контрольной группе ({', '.join(D['ctrl'])}) {pct(D['season'])}."))
ch = D.get('check') or {}; c7 = D.get('check7') or {}
if c7.get('cur'):
    lines.append(f"Средний чек заказа за 7 дней {c7['cur_from']}–{c7['cur_to']}: {rub(c7['cur'])} ({pct(c7.get('pct'))} к предыдущим 7 дням{'' if OPM else ', ' + pct(c7.get('pct_jun')) + ' к июню'}).")
if ch.get('cur'):
    lines.append(f"Средний чек за период {ch['cur_from']}–{ch['cur_to']}: {rub(ch['cur'])} ({pct(ch.get('pct_eq'))} к равному отрезку перед ним).")
if OPM:
    _dn = sum(1 for r in rows if r['kind'] == 'back' and (r.get('turn') or {}).get('down'))
    lines.append(f"Решения сегодня: поднять {c['up']}, снизить затоваренных {_dn} (плановое снижение, ровно {_dn} арт.), откат после провала {c['back']-_dn}, дубли как оригинал {c.get('follow',0)}, пауза {c['pause']}, ждать {c['wait']}, цель ОП достигнута {c['done']}, без подъёма из-за большого остатка {c['deep']} (их цену не меняем), отложено сотрудником {c.get('skip',0)}.")
else:
    lines.append(f"Решения сегодня: поднять {c['up']}, ставить как оригинал (дубли) {c.get('follow',0)}, пауза {c['pause']}, откат {c['back']}, ждать {c['wait']}, держать (бюджет) {c['hold']}, цель достигнута {c['done']}, затоварены {c['deep']}, контроль/заморозка {c['ctrl']}.")
st = D.get('store', {}); days = D['days']
if st.get('orders'):
    lines.append("Заказы всех товары по дням (последние 14): " + ', '.join(f"{d[5:]}={o}" for d, o in zip(days[-14:], st['orders'][-14:])))
    lines.append("Средняя цена заказа по дням, ₽ (последние 14): " + ', '.join(f"{d[5:]}={p if p is not None else '—'}" for d, p in zip(days[-14:], st['price'][-14:])))
mk = D.get('market')
if mk and mk.get('core'):
    core = [x for x in mk['core'] if x is not None]; op = [x for x in (mk.get('open') or []) if x is not None]
    if len(core) >= 6:
        h = len(core)//2
        dc = (sum(core[h:])/len(core[h:]))/(sum(core[:h])/h)-1
        lines.append(f"Монитор рынка (справочно, на решения не влияет): спрос по ядру запросов WB, вторая половина периода к первой {pct(dc*100)}" + (f"; наши показы {pct(((sum(op[len(op)//2:])/len(op[len(op)//2:]))/(sum(op[:len(op)//2])/(len(op)//2))-1)*100)}" if len(op) >= 6 else '') + ".")
# скидка продавца: норма 24%
odd = [r for r in rows if r.get('disc') is not None and r['disc'] != 24 and r['kind'] != 'ctrl']
if odd:
    lines.append("Скидка продавца НЕ 24% (норма 24%; если WB завёл товар в акцию — сначала вернуть 24%, потом ставить цены): " + ', '.join(f"{r['a']}={r['disc']}%" for r in odd[:25]) + (f" и ещё {len(odd)-25}" if len(odd) > 25 else ''))
else:
    lines.append("Скидка продавца у всех артикулов 24% — норма.")
important = [r for r in rows if r['kind'] in ('back', 'pause', 'hold', 'deep')]
if important:
    lines.append("Артикулы с проблемами:")
    for r in important[:20]:
        lines.append(f"  {r['a']} (nm {r['nm']}): {r['act']} — {r['note']}; цена {rub(r['p'])}{'' if OPM else ' (' + pct(r['now_pct']) + (' к июню' if r['src']=='июнь' else ' к цене старта') + ')'}, факт {r['O']} при ожидании {r['E']} (порог {r['thr']}), к базе с начала кампании {pct(r['cdrop'])}, бюджет артикула {r['sp']}%.")
sk = D.get('store_kpi') or {}
if D.get('op_rule') and sk:
    R = D['op_rule']
    lines.append((f"Кампания «ОП 15%»: цель по магазину – продажи {R.get('revenue_goal',1500000):,} ₽ в день (продажи в ценах продавца по финотчётам; сейчас {sk.get('rev7_day',0):,} ₽ в день за 7 дней) и ОП после налогов {R.get('store_op',15)}% от продаж при ДРР {'–'.join(str(x) for x in R.get('drr_target',[4,5]))}%. Сейчас: заказы {sk.get('orders_rub_day',0):,} ₽/день за 7 дней, ОП магазина {sk.get('op7', sk.get('op14', sk.get('op28')))}% за 7 дней по ежедневным финотчётам (неделей раньше – {sk.get('op7_prev', sk.get('op14_prev'))}%; решения по артикулам тоже считаются по ОП за 7 дней), ДРР {sk.get('drr7')}% за 7 дней (до этого {sk.get('drr_base')}%). Цель артикула – цена, при которой его ОП достигает цели зоны (остаток " + ', '.join(f"до {z['max_days']} дн. – {z['op']}%" for z in sorted(R['zones'], key=lambda z: z['max_days'])) + f", локомотив – {R.get('loco_op')}%); локомотивы не поднимаются, если CPO выше предела или ДРР магазина выше 5%. Низкая маржа локомотива сама по себе не проблема.").replace(',', ' '))
    lq = [r for r in rows if r.get('lowq') and not re.search(r'-\d+$', r['a'])]
    if lq: lines.append("Мало продаж при ОП выше 15% (меньше 3 заказов в день за 7 дней) – снижаем быстрее, шаг 2–3% раз в 2 дня до ОП 15%: "
                        + ', '.join(f"{r['a']} ({r['lowq']['q7']:g}/день, {r['act'][:30]})" for r in lq[:15]))
    dh = [r for r in rows if r.get('act','').startswith('Держать – ДРР')]
    if dh: lines.append("Локомотивы на удержании из-за роста ДРР: " + ', '.join(r['a'] for r in dh[:15]))
    else: lines.append("Удержаний из-за ДРР сегодня нет: ни один артикул не остановлен из-за рекламы.")
    cl = [r for r in rows if r.get('loco') and (r.get('cpo') or {}).get('now') is not None and not re.search(r'-\d+$', r['a'])]
    if cl:
        C = R.get('loco_cpo') or {}; lo, hi = C.get('min', 180), C.get('max', 220)
        lines.append("CPO – показатель рекламы локомотива (цена рекламного заказа); держать цену из-за CPO – значит беречь рекламу, не противопоставляй CPO и рекламу.")
        lines.append(f"CPO локомотивов (формула рекламного сервиса: расход ÷ (свои рекламные заказы + заказы других артикулов с рекламы), последние 30 рекламных заказов; до {lo} ₽ – обычный шаг, {lo}–{hi} – половинный, выше {hi} – подъём стоит): "
                     + ', '.join(f"{r['a']} {r['cpo']['now']} ₽" for r in sorted(cl, key=lambda r: -r['cpo']['now'])))
        ch = [r for r in rows if 'CPO' in (r.get('act') or '') or ('CPO после подъёма' in (r.get('note') or '') and r['kind'] == 'back')]
        if ch: lines.append("Решения по CPO сегодня: " + '; '.join(f"{r['a']}: {r['act']}" for r in ch[:10]))
    pz = [r for r in rows if r['kind'] == 'pause' and not re.search(r'-\d+$', r['a'])]
    if pz: lines.append("На паузе после шага из-за просадки заказов (не из-за ДРР): " + ', '.join(f"{r['a']} ({r['act']})" for r in pz[:12]))
    dn_ = [r for r in rows if r['kind'] == 'done' and r.get('op_goal') is not None and not re.search(r'-\d+$', r['a'])]
    if dn_: lines.append("Цель ОП уже достигнута, цену держим: " + ', '.join(r['a'] for r in dn_[:12]))
ts = D.get('turn_sum')
if ts and not OPM:
    lines.append(f"Оборачиваемость (цель по цене зависит от остатка дней; в среднем по заказам цель +{D['goal']:g}%): " + '; '.join(f"{'до '+str(z['max_days'])+' дн.' if z['max_days'] else 'больше 250 дн.'} – {z['n']} арт., {z['share']}% заказов, цель +{z['tgt']:g}%" for z in ts) + ". Затоваренные (больше 250 дн.) не поднимаем; при заказах хуже портфеля снижаем по 2 п.п. в неделю до цели зоны, но не ниже цены, при которой маржинальность после налогов 12%.")
dn = [r for r in rows if r.get('turn') and r['turn'].get('down')]
if dn:
    lines.append("Снижения по оборачиваемости сегодня (это не откат, а плановое снижение затоваренных): " + ', '.join(f"{r['a']} ({rub(r['p'])} → {rub(r['new'])}, запас {r['turn']['days']:.0f} дн., в кабинет {rub(r['nfull']) if r.get('nfull') else '—'})" for r in dn[:20]))
aw = [r for r in rows if r.get('ad_warn')]
if aw:
    lines.append("Локомотивы, которые заканчиваются на остатках – нужно решение по рекламе или поставке (назови их владельцу отдельно; низкая или нулевая маржинальность локомотива – норма, за его счёт продаются другие товары, из-за маржи рекламу резать не советуй): " + '; '.join(f"{r['a']}: {r['ad_warn']}" for r in aw[:12]))
scr = [r for r in rows if r.get('scarce')]
if scr:
    lines.append("Дефицитные артикулы (остаток на 50 дней и меньше – шаг усилен, цель артикула выше общей; остаток всё равно распродастся, поэтому продаём его дороже): " + ', '.join(f"{r['a']} (остаток {r['scarce']['days']:.0f} дн., шаг +{r['scarce']['step']} п.п.)" for r in scr[:20]))
ups = [r for r in rows if r['kind'] == 'up']
if ups:
    lines.append("На подъём сегодня (артикул: сейчас → новая цена до СПП; в кабинет ставится цена ДО скидки продавца): " + ', '.join(f"{r['a']} ({rub(r['p'])} → {rub(r['new'])}, в кабинет {rub(r['nfull']) if r.get('nfull') else '—'}, шаг +{r['step']} п.п.)" for r in ups[:40]) + (f" и ещё {len(ups)-40}" if len(ups) > 40 else ''))
fol = [r for r in rows if r['kind'] == 'follow']
if fol:
    lines.append("Дубли, которые ставятся в цену оригинала: " + ', '.join(f"{r['a']} → {rub(r['new'])} (в кабинет {rub(r['nfull']) if r.get('nfull') else '—'})" for r in fol[:30]))
offs = [r for r in rows if r['kind'] == 'off']
if offs:
    lines.append("Не участвуют в подъёме (нет на остатках или нет фото в карточке WB — правило владельца): " + ', '.join(f"{r['a']} ({r['act'].lower()})" for r in offs[:30]))
waits = [r for r in rows if r['kind'] == 'wait']
if waits:
    lines.append(f"Ждут окна наблюдения: {len(waits)} артикулов.")
fbk = D.get('feedback') or {}
pv = fbk.get('prev')
if pv:
    sru = fbk.get('status_ru', {})
    cnt = ', '.join(f"{sru.get(k, k)} {v}" for k, v in sorted(pv['counts'].items(), key=lambda x: -x[1]))
    lines.append(f"Обратная связь по рекомендациям прошлого прогона {pv['run']} ({pv['total']} шт., сверка с кабинетом {'была' if pv.get('api') else 'не удалась'}): {cnt}.")
    ns = [a for a, v in pv['arts'].items() if v['status'] == 'not_set']
    if ns: lines.append("Не поставлены (движок повторяет их сегодня, если условия те же): " + ', '.join(ns[:30]) + (f" и ещё {len(ns)-30}" if len(ns) > 30 else ''))
    cus = [a for a, v in pv['arts'].items() if v.get('custom')]
    if cus: lines.append("Сотрудник ставил свою цену (выше рекомендованной): " + ', '.join(f"{a} → {rub(pv['arts'][a]['rec'])}" for a in cus[:15]))
    unc = [a for a, v in pv['arts'].items() if v['status'] in ('set_unconfirmed', 'set_other', 'changed_other')]
    if unc: lines.append("Расхождения отметки и кабинета: " + '; '.join(f"{a} — {sru.get(pv['arts'][a]['status'])}, в кабинете {rub(pv['arts'][a].get('actual'))}" for a in unc[:15]))
hl = fbk.get('holds') or {}
if hl:
    lines.append("Отложены сотрудником (движок их не трогает): " + ', '.join(f"{a} до {h['until']} ({h.get('why','')})" for a, h in list(hl.items())[:20]))
lines.append("Обратная связь: на странице по каждой рекомендации есть кнопки «Поставил» и «Не ставлю». «Поставил» учитывается как шаг днём рекомендации, завтра сверяется с кабинетом; «Не ставлю» с датой откладывает артикул. Если прошлые рекомендации массово не поставлены — скажи об этом прямо и напомни отмечать.")
pm = D.get('promos') or {}
if pm.get('loco'):
    lc = sorted(pm['loco'].items(), key=lambda kv: -kv[1]['spend'])
    lines.append("Локомотивы (артикулы с наибольшим расходом на рекламу за 14 дней; выход локомотива из всех акций WB снимает бустинг и роняет заказы, поэтому их шаг режется до проходной цены акции, на границе — держим): "
                 + ', '.join(f"{a} ({x['share']}%, {x['spend']} ₽" + (f", проходная {pm['cap'][a]['cap']} ₽" if a in (pm.get('cap') or {}) else ', проходная неизвестна') + ')' for a, x in lc[:12]))
    lines.append(f"Акций WB сейчас идёт {pm.get('active_count', 0)} (все автоматические), проходные цены известны по {len(pm.get('cap') or {})} артикулам"
                 + ('' if pm.get('cap') else ' — их нужно выгрузить из Календаря акций; пока их нет, по локомотивам напоминай проверять выход из акций перед установкой цены') + '.')
exits_today = [r for r in rows if r.get('exit')]
if exits_today:
    lines.append("Сегодняшние рекомендации, которые выводят артикул из всех идущих акций WB (по решению владельца подъём важнее акций; локомотивы при выходе получают увеличенный шаг): "
                 + ', '.join(f"{r['a']} → {rub(r['new'])} (проходная {rub(r.get('cap'))})" for r in exits_today[:15]))
ps = (pm.get('status') or {}) if pm else {}
rec_ex = sorted([(a, s['since']) for a, s in ps.items() if not s['in'] and s.get('since')], key=lambda x: x[1], reverse=True)[:12]
if rec_ex:
    lines.append("Вышли из всех акций (дата выхода по истории цен — сопоставь с заказами по этим артикулам, если после этой даты был провал): " + ', '.join(f"{a} с {d}" for a, d in rec_ex))
big = sorted(rows, key=lambda r: -(r.get('cbase') or 0))[:12]
lines.append("Крупнейшие по базе артикулы (артикул: база шт/день, решение, к базе кампании, заказы за последние 7 дней): " + '; '.join(f"{r['a']}: {r['cbase']}, {r['act']}, {pct(r['cdrop'])}, {sum(r['c'][-7:])} зак." for r in big))
prev = [x for x in cj['comments'] if x.get('date') and x.get('text')]
prev.sort(key=lambda x: x['date'], reverse=True)
if prev:
    lines.append("Предыдущие комментарии (для преемственности, не повторять дословно):")
    for x in prev[:3]:
        lines.append(f"  {x['date']}: {x['text']}")

prompt_op = (
    "Ты ведёшь ежедневный отчёт по ценам на Wildberries (товары, ваш бренд) для владельца магазина и его сотрудника, "
    "который вписывает цены в кабинет. Цель кампании: заказы 1,5 млн ₽ в день при ОП магазина 18% после налогов и ДРР 4–5%. "
    "Напиши короткий комментарий за сегодня: 2–4 предложения обычным текстом по-русски, только про цель. (1) Где магазин к цели: "
    "заказы в день к 1,5 млн, ОП магазина к 18%, ДРР к коридору 4–5% и что изменилось. (2) Что сегодня сделать в кабинете ради цели: "
    "сколько артикулов поднять к целевой ОП и сколько затоваренных снизить (назови 2–3 самых важных), есть ли локомотивы на удержании из-за ДРР. "
    "(3) Одна вещь, на которую смотреть завтра. Не пересказывай бюджет падения, сезон, уровень цен к июню и акции, если это не мешает цели. "
    "Правила: решения принимает алгоритм, ты их не пересматриваешь; низкая маржа локомотива – норма, рекламу из-за неё резать не советуй. "
    "Называй артикулы и цифры из данных, причины решений бери только из данных, не додумывай; проценты пиши с запятой (10,4%). Сразу текст комментария, без вступления и без фразы, что ты сейчас его соберёшь. Без заголовков, без markdown, без кавычек и списков – только текст комментария, "
    "вместо длинного тире ставь короткое «–».\n\n"
    + '\n'.join(lines)
)
prompt = prompt_op if OPM else (
    "Ты ведёшь ежедневный отчёт подъёма цен на Wildberries (товары, ваш бренд) для владельца магазина и его сотрудника, "
    "который вписывает цены в кабинет. Ниже — данные сегодняшнего прогона алгоритма. Напиши комментарий-подсказку за сегодня: "
    "3–6 предложений обычным текстом по-русски. Что должно быть в комментарии: (1) что видно в данных именно сегодня и что изменилось "
    "со вчерашнего дня — уровень цен, поток, зона бюджета, локальная это просадка (отдельные артикулы) или портфельная; "
    "(2) что сделать в кабинете: сначала проверить скидку продавца 24% (назови артикулы, где она другая, если есть), затем вписать "
    "цены из колонки «Ставить в кабинет» — назови число артикулов на подъём и самые весомые по базе; если есть откаты — выдели это "
    "отдельно, откат всегда повод сообщить владельцу; (3) на что смотреть завтра. "
    "Правила: решения принимает алгоритм, ты их не пересматриваешь и не предлагаешь менять пороги, шаг, окна наблюдения или "
    "контрольную группу; не советуй поднимать или откатывать то, чего нет в решениях. Называй артикулы и цифры из данных, "
    "проценты пиши в виде +N% / -N%. Сразу текст комментария, без вступления и без фразы, что ты сейчас его соберёшь. Без заголовков, без markdown, без кавычек и списков – только текст комментария.\n\n"
    + '\n'.join(lines)
)
if A.dry_run:
    print(prompt); print('-----')

def ask_grok(text):
    key = os.environ.get('CURSOR_API_KEY', '').strip()
    if not key:
        log('нет CURSOR_API_KEY'); return None, 'на сервере нет ключа Grok'
    try:
        from cursor_sdk import (Agent, AgentOptions, Bridge, Client, CursorAgentError,
                                LocalAgentOptions, ModelParameterValue, ModelSelection)
    except ImportError:
        log('пакет cursor-sdk не установлен'); return None, 'на сервере не установлен клиент Grok'
    cwd = tempfile.mkdtemp(prefix='wb-comment-')
    # Мост SDK берёт рабочей папкой текущий каталог. Пустой каталог, чтобы модель
    # не открывала проект с ценами. Пути страницы уже абсолютные.
    back = os.getcwd()
    os.chdir(cwd)
    bridge = client = None
    def _alarm(signum, frame):
        raise TimeoutError()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(A.timeout)
    try:
        bridge = Bridge.launch(workspace=cwd)
        client = Client(bridge.endpoint, unary_timeout=float(A.timeout), timeout=float(A.timeout))
        result = Agent.prompt(text, AgentOptions(
            api_key=key,
            model=ModelSelection(id=A.model, params=[
                ModelParameterValue(id='context', value='256k'),
                ModelParameterValue(id='reasoning_effort', value='high'),
                ModelParameterValue(id='fast', value='true'),
            ]),
            tools=[],
            local=LocalAgentOptions(cwd=cwd),
        ), client=client)
    except TimeoutError:
        log(f'Grok не ответил за {A.timeout} с'); return None, 'Grok не ответил вовремя'
    except CursorAgentError as e:
        msg = str(e)[:300]
        log(f'Grok не запустился: {msg}')
        return None, f'Grok вернул ошибку: {msg[:150]}'
    except Exception as e:
        msg = str(e)[:300]
        log(f'Grok не запустился: {msg}')
        return None, f'Grok вернул ошибку: {msg[:150]}'
    finally:
        signal.alarm(0)
        if client is not None:
            client.close()
        if bridge is not None:
            bridge.close()
        os.chdir(back)
    if getattr(result, 'status', '') != 'finished':
        log(f'Grok завершился со статусом {getattr(result, "status", "")}')
        return None, 'Grok не закончил комментарий'
    return (getattr(result, 'result', None) or ''), None

raw, err = ask_grok(prompt)
if err:
    if not A.dry_run:
        inject(cj['comments'], err)
    sys.exit(1)
text = ' '.join(raw.split()).strip().strip('"«»')
text = text.replace('—', '–')   # правило владельца: в текстах только короткое тире
if len(text) < 80 or len(text) > 4000 or text.startswith('#'):
    log(f'ответ не похож на комментарий ({len(text)} симв.): {text[:200]}')
    inject(cj['comments'], 'ответ Grok не похож на комментарий'); sys.exit(1)

if A.dry_run:
    print(text); sys.exit(0)
cj['comments'].insert(0, {'date': today, 'text': text})
tmp = A.comments + '.tmp'
json.dump(cj, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
os.replace(tmp, A.comments)
inject(cj['comments'])
print(f'[comment] {today}: {text}')
