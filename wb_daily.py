#!/usr/bin/env python3
"""Ежедневный контроль подъёма цен на товары (WB). Версия 2: якорь — июньская цена, лимит потока — 15% от июньских заказов.
Запуск: python3 wb_daily.py --token <TOKEN> --plan plan.json --state state.json --out page.html --tpl daily_tpl.html
"""
import json, math, argparse, datetime, statistics, subprocess, sys, os, time, re
from collections import defaultdict

AP=argparse.ArgumentParser()
AP.add_argument('--token',required=True); AP.add_argument('--plan',default='plan.json')
AP.add_argument('--state',default='state.json'); AP.add_argument('--out',default='page.html')
AP.add_argument('--tpl',default='daily_tpl.html'); AP.add_argument('--orders',default=None)
AP.add_argument('--prices',default='data/prices.json',
                help='файл {"артикул": цена_со_скидкой} — фактические цены кабинета; '
                     'используется, если API цен недоступен')
AP.add_argument('--disc',default='data/discounts.json',
                help='файл {"артикул": скидка_продавца_в_процентах}; нужен, чтобы показать '
                     'цену ДО скидки — именно её вписывают в кабинет')
AP.add_argument('--pricehist',default='data/price_history.json',
                help='история фактических цен по дням {"ГГГГ-ММ-ДД": {"артикул": цена}}; '
                     'по ней определяются шаги — цена из заказов для этого слишком шумная')
AP.add_argument('--market',default='data/market.json',
                help='рынок и видимость по дням: частотность WB по ядру запросов, показы кабинета, '
                     'позиция. Монитор — на решения алгоритма не влияет')
AP.add_argument('--promos',default=None,
                help='promos.json от promos.py: локомотивы (реклама), акции WB и проходные цены — '
                     'локомотив не выводится из всех акций малым шагом')
AP.add_argument('--feedback',default=None,
                help='обратная связь сотрудника (feedback.json, см. feedback.py): отметки «не ставлю» '
                     'с датой держат артикул — ни подъёмов, ни откатов до этой даты')
AP.add_argument('--stocks',default=None,
                help='остатки WB {артикул: шт} из файла вместо API (вторая кампания берёт файл, который утром сохранила основная)')
AP.add_argument('--stocks-out',default=None,help='куда сохранить полученные из API остатки WB (для второй кампании)')
AP.add_argument('--season',default=None,help='season.json (season_days.py): сезонные коэффициенты и остатки план-факта')
AP.add_argument('--planmargin',default=None,
                help='plan_margin.json (plan_margin.py): маржинальность после налогов по артикулу – ограничитель снижений (plan.json → turnover)')
AP.add_argument('--stockdays',default=None,
                help='stock_days.json (stockdays_sheet.py): остаток дней по артикулу из таблицы продавца – для правила дефицита (plan.json → scarce)')
AP.add_argument('--avail',default=None,
                help='avail.json от avail.py: остаток на фуле из таблицы владельца и число фото в карточке WB. '
                     'Нет на остатках (фул и WB меньше порога) или нет фото — артикул не поднимается')
A=AP.parse_args()

TODAY=datetime.date.today(); LAST=TODAY-datetime.timedelta(days=1)
HIST=45; START=TODAY-datetime.timedelta(days=HIST)

def api(url,params=None,out=None,tries=3):
    cmd=['curl','-sS','-G',url,'-H',f'Authorization: {A.token}','--max-time','300']
    for k,v in (params or {}).items(): cmd+=['--data-urlencode',f'{k}={v}']
    if out: cmd+=['-o',out]
    for i in range(tries):
        r=subprocess.run(cmd,capture_output=True,text=True)
        if r.returncode==0 and (out or r.stdout.strip()):
            return json.load(open(out,encoding='utf-8')) if out else json.loads(r.stdout)
        time.sleep(20)
    raise RuntimeError(f'API failed: {url}')

raw=json.load(open(A.orders,encoding='utf-8')) if (A.orders and os.path.exists(A.orders)) else \
    api('https://statistics-api.wildberries.ru/api/v1/supplier/orders',{'dateFrom':START.isoformat()},out='orders_daily.json')

stock={}
try:
    if A.stocks and os.path.exists(A.stocks):
        _sj=json.load(open(A.stocks,encoding='utf-8'))
        if _sj.get('_date')!=datetime.date.today().isoformat(): raise RuntimeError(f'файл остатков {A.stocks} за {_sj.get("_date")}, а не за сегодня')
        stock={str(k):int(v) for k,v in _sj.items() if not str(k).startswith('_')}
    else:
        t=api('https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains',{'groupByNm':'true','groupBySa':'true'})
        tid=t['data']['taskId']; time.sleep(35)
        for r in api(f'https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains/tasks/{tid}/download'):
            # реальный остаток WB – только склад «Склад WB РФ» (правило остатка); «Всего находится на складах» и
            # региональные склады (Краснодар, Шушары, …) не считаются – по ним товар не продаётся
            q=next((w['quantity'] for w in r['warehouses'] if w['warehouseName']=='Склад WB РФ'),0)
            stock[str(r['vendorCode'])]=stock.get(str(r['vendorCode']),0)+q
        if A.stocks_out and stock:
            json.dump({'_date':datetime.date.today().isoformat(),**stock},open(A.stocks_out,'w',encoding='utf-8'),ensure_ascii=False)
except Exception as e:
    print(f'[warn] остатки не получены: {e}',file=sys.stderr)

# ---------- фактические цены кабинета ----------
# Цена из заказов — это цена последнего оплаченного заказа: у артикула с редкими
# продажами она может отставать на неделю и больше, и шаг тогда считается от эха.
# Поэтому «цена сейчас» берётся из API цен, а заказы остаются только для истории.
price_now={}; disc_now={}; price_src='заказы'
try:
    off=0
    while True:
        j=api('https://discounts-prices-api.wildberries.ru/api/v2/list/goods/filter',
              {'limit':1000,'offset':off})
        g=j['data']['listGoods']
        for it in g:
            sz=(it.get('sizes') or [{}])[0]
            if sz.get('discountedPrice'):
                price_now[str(it['vendorCode'])]=round(sz['discountedPrice'])
            if it.get('discount') is not None: disc_now[str(it['vendorCode'])]=it['discount']
        if len(g)<1000: break
        off+=1000
    price_src='API цен'
except Exception as e:
    print(f'[warn] API цен недоступен: {e}',file=sys.stderr)
    if A.prices and os.path.exists(A.prices):
        price_now={str(k):round(v) for k,v in json.load(open(A.prices,encoding='utf-8')).items()
                   if not str(k).startswith('_') and isinstance(v,(int,float)) and v}
        price_src=f'файл {A.prices}'
if not price_now:
    print('[warn] фактические цены не получены — «цена сейчас» берётся из заказов',file=sys.stderr)
if not disc_now and A.disc and os.path.exists(A.disc):
    disc_now={str(k):v for k,v in json.load(open(A.disc,encoding='utf-8')).items()
              if not str(k).startswith('_') and isinstance(v,(int,float))}
# цена до скидки продавца = цена со скидкой ÷ (1 − скидка). Это то число, которое
# вписывают в кабинет в поле «Цена продавца до скидки»; скидка при этом не меняется.
def full(p,k):
    d=disc_now.get(str(k))
    return round(p/(1-d/100)) if (p and d is not None and d<100) else None

# ---------- история фактических цен ----------
# Шаги раньше определялись по медиане цен заказов за день. На выборке в 2-7 заказов
# единичный заказ по другой цене сдвигал медиану на 1% — скрипт видел повышение там,
# где его не было, и заново открывал окно ожидания. Теперь за день, по которому есть
# снимок цен кабинета, берётся он; для более ранних дней — мода цен заказов (цена
# карточки одна, поэтому самое частое значение вернее середины разброса).
HIST={}
if A.pricehist and os.path.exists(A.pricehist):
    try: HIST={d:{str(k):round(v) for k,v in vv.items()} for d,vv in json.load(open(A.pricehist,encoding='utf-8')).items()}
    except Exception as e: print(f'[warn] история цен не прочитана: {e}',file=sys.stderr)
if price_now:
    HIST[TODAY.isoformat()]=dict(price_now)
    if A.pricehist:
        os.makedirs(os.path.dirname(A.pricehist) or '.',exist_ok=True)
        json.dump(HIST,open(A.pricehist,'w',encoding='utf-8'),ensure_ascii=False,indent=0,sort_keys=True)

META=json.load(open(A.plan,encoding='utf-8'))
plan={r['a']:r for r in META['rows']}
STOCK_LIVE=bool(stock)          # остатки WB получены сегодня из API (а не подставлены из плана)
if not stock: stock={k:v['stock'] for k,v in plan.items() if v.get('stock') is not None}
state=json.load(open(A.state,encoding='utf-8')) if os.path.exists(A.state) else {'arts':{},'meta':{}}
CTRL=set(META['ctrl']); END=datetime.date.fromisoformat(META['end']); FLOOR=META['floor_pct']/100
CAMP=datetime.date.fromisoformat(META['start']); YEL=META.get('yellow_at',0.55); RED=META.get('red_at',0.80)
SUBJ=META.get('subject')
# артикулы, выведенные из оборота или с остатком, который не считаем:
# в кампании не участвуют ни в рекомендациях, ни в расчёте потока
EXCL=set(str(x) for x in META.get('exclude',[]))
# порог оборачиваемости: артикулы с запасом больше COVER_CAP дней не поднимаются.
# null/отсутствие ключа в plan.json = правило выключено (решение владельца 02.09.2026)
COVER_CAP=META.get('cover_days',150)
# заморозка отдельных артикулов на срок: участие в акции WB, съёмка, спорный товар.
# формат в plan.json: "freeze": {"101": {"until": "2026-09-14", "why": "акция WB"}}
# пока дата не наступила, артикул не поднимается и не откатывается.
FREEZE={str(k):v for k,v in (META.get('freeze') or {}).items()}
# обратная связь со страницы (feedback.py): «не ставлю» с датой → артикул отложен до даты.
# Тот же смысл, что у freeze, но ставится сотрудником кнопкой, а не правкой plan.json.
FB_HOLD={}
if A.feedback and os.path.exists(A.feedback):
    try:
        FB_HOLD={str(k):v for k,v in (json.load(open(A.feedback,encoding='utf-8')).get('holds') or {}).items() if v.get('until')}
    except Exception as e: print(f'[warn] обратная связь не прочитана: {e}',file=sys.stderr)
# доступность (avail.py, правило доступности): артикул, которого нет на остатках
# (на фуле меньше FF_MIN И на складах WB меньше WB_MIN), или у которого в карточке WB нет фото,
# в подъёме цен не участвует: ни подъёмов, ни откатов, и его заказы не идут в расчёт бюджета
# потока (их спад вызван не ценой). Дубли берут остаток фула базового артикула.
AVAIL={}
if A.avail and os.path.exists(A.avail):
    try: AVAIL=json.load(open(A.avail,encoding='utf-8'))
    except Exception as e: print(f'[warn] avail.json не прочитан: {e}',file=sys.stderr)
FF_ST={str(k):v for k,v in (AVAIL.get('ff') or {}).items()}
TOWB={str(k):v for k,v in (AVAIL.get('to_wb') or {}).items()}   # «Товары в пути на склад ВБ» из «Рассчет поставки» (владелец 15.10.2026)
PHOTOS={str(k):v for k,v in (AVAIL.get('photos') or {}).items()}
# остаток, выставленный на складах продавца (FBS), по артикулу. Если он 0 на всех складах и на складах WB тоже 0 –
# артикул «не выставлен на складах» (обнулён): продавать нечего, в подъёме цен не участвует (правило владельца 30.09.2026)
FBS={str(k):int(v) for k,v in (AVAIL.get('fbs') or {}).items()}
# остаток на складах WB меньше этого числа штук считается нулём (решение владельца 30.09.2026); plan.json → unlisted_wb_below
UNL_WB=int(META.get('unlisted_wb_below',30))
FF_MIN=int(AVAIL.get('ff_min',20)); WB_MIN=int(AVAIL.get('wb_min',20))
TOT_MIN=int(META.get('nostock_total_below',20))   # общий остаток WB + фул меньше этого – «нет на остатках», строка скрыта со страницы
# дефицит (правило владельца 25.09.2026): если товара осталось на scarce.levels[].max_days дней или меньше, поднимать
# агрессивнее – остаток всё равно распродастся, и терять поток уже нечего; лучше продать остаток дороже.
# Уровни идут от меньшего к большему: ≤30 дней – сильнее, ≤50 – умереннее. Для уровня: цель артикула + tgt_plus п.п.,
# шаг не меньше step_min и не больше step_max (обычный потолок 2,5 не действует), половинный шаг зоны не применяется
# (ignore_half). Дни: days_source = sheet (stock_days.json, WB + свой склад) | cover (только WB, из движка) | auto.
SCARCE=META.get('scarce') or {}
SC_LEVELS=sorted(SCARCE.get('levels',[]),key=lambda l:l['max_days'])
SDAYS={}
if A.stockdays and os.path.exists(A.stockdays):
    try: SDAYS={str(k):float(v) for k,v in (json.load(open(A.stockdays,encoding='utf-8')).get('by_art') or {}).items()}
    except Exception as e: print(f'[warn] stock_days.json не прочитан: {e}',file=sys.stderr)
# оборачиваемость (правило владельца 30.09.2026): цель по цене зависит от остатка дней, а общая цель +15% остаётся
# средневзвешенной по заказам. zones – по возрастанию max_days (null = всё, что больше): tgt – цель артикула к якорю.
# Затоварен (остаток > over_days): не поднимать; если цена выше цели зоны, заказы к базе хуже портфеля и маржинальность
# после налогов не ниже down_min_margin_pct – снижать по down_step_pp раз в down_every_days, не ниже цели зоны / якоря.
# Дефицит (остаток ≤ scarce_ignore_pause_days): паузы и удержания по бюджету не действуют – падение заказов у
# заканчивающегося товара не проблема. Локомотив с малым остатком или низкой маржой получает пометку по рекламе.
# цель по ОП (кампания «ОП 15%», решение владельца 01.10.2026): цель артикула – цена, при которой ОП после налогов
# (маржинальность от выручки до СПП) достигает целевой: остаток ≤ 60 дн. – zones[0].op, ≤ 120 – zones[1].op, локомотив –
# loco_op, затоваренный (> over_days) – over_op (до неё можно снижать). Цена считается по margin_at: расходы, зависящие
# от цены (комиссия, эквайринг, налог), – доля k, остальное – прежняя сумма на штуку. Контроль ДРР: локомотивы не
# поднимаем, если ДРР магазина за 7 дней выше drr_max или ДРР самого артикула вырос больше чем в loco_drr_rise раз к базе.
OPT=META.get('op_target') or {}
OP_ON=bool(OPT.get('enabled'))
STORE={}
if A.planmargin and os.path.exists(A.planmargin):
    try: STORE=json.load(open(A.planmargin,encoding='utf-8')).get('store') or {}
    except Exception: STORE={}
TURN=META.get('turnover') or {}
TURN_ON=bool(TURN.get('enabled'))
TZONES=sorted(TURN.get('zones',[]),key=lambda z:(z.get('max_days') is None, z.get('max_days') or 0))
PMARG={}
if A.planmargin and os.path.exists(A.planmargin):
    try: PMARG=json.load(open(A.planmargin,encoding='utf-8')).get('by_art') or {}
    except Exception as e: print(f'[warn] plan_margin.json не прочитан: {e}',file=sys.stderr)
CPOD={}   # реклама по дням для CPO локомотивов: plan_margin.py → cpo_daily {артикул: [[день, расход, заказы, клики]]}
if A.planmargin and os.path.exists(A.planmargin):
    try: CPOD=json.load(open(A.planmargin,encoding='utf-8')).get('cpo_daily') or {}
    except Exception: CPOD={}
# кампания ОП: основа маржи для решений (решение владельца 13.10.2026 – 7 дней):
#  margin_days=7  – ОП за 7 дней по ЕЖЕДНЕВНЫМ финотчётам (свежие на вчера); если выручка за 7 дней меньше margin_min_rev
#                   (20 000 ₽) – за 14 дней по тем же ежедневным отчётам; нет и их – 14 дней по недельным; иначе 28 дней;
#  margin_days=14 – как было с 30.09.2026: два последних недельных финотчёта.
def _use(_x, k, days):
    if _x.get(f'margin{k}_pct') is None or not _x.get(f'avg_price{k}'): return False
    _x.update({'margin_pct':_x[f'margin{k}_pct'],'avg_price':_x[f'avg_price{k}'],'var_pct':_x.get(f'var_pct{k}') or _x.get('var_pct'),
               'no_cost':_x.get(f'no_cost{k}',False),'margin_days':days}); return True
if OP_ON and int(OPT.get('margin_days',28))==7:
    _min=float(OPT.get('margin_min_rev',20000))
    for _x in PMARG.values():
        ((_x.get('rev7') or 0)>=_min and _use(_x,'7',7)) or _use(_x,'14d',14) or _use(_x,'14',14)
elif OP_ON and int(OPT.get('margin_days',28))==14:
    for _x in PMARG.values(): _use(_x,'14',14)
def margin_of(a):
    """Маржинальность после налогов, % (None – нет данных или не заполнена себестоимость)."""
    x=PMARG.get(str(a)) or {}
    if x.get('no_cost') or x.get('margin_pct') is None:
        m=re.fullmatch(r'(.+)-[12]',str(a)); x=(PMARG.get(m.group(1)) or {}) if m else {}
        if x.get('no_cost'): return None
    return x.get('margin_pct')
def _pm(a):
    x=PMARG.get(str(a)) or {}
    if x.get('no_cost') or x.get('margin_pct') is None or not x.get('avg_price'):
        m=re.fullmatch(r'(.+)-[12]',str(a)); x=(PMARG.get(m.group(1)) or {}) if m else {}
    return x if (x.get('margin_pct') is not None and x.get('avg_price') and not x.get('no_cost')) else None
_VARS=[x['var_pct'] for x in PMARG.values() if x.get('var_pct') and not x.get('no_cost')]
VAR_AVG=(sum(_VARS)/len(_VARS)) if _VARS else 0.35
def margin_at(a,price):
    """Оценка маржинальности после налогов при цене price, %: расходы, зависящие от цены (комиссия, эквайринг, налог),
    остаются долей k, остальные (себестоимость, логистика, хранение, реклама) – прежней суммой на штуку.
    Маржа(p) = (1−k) − p0·(1−k−m)/p, где m и p0 – маржинальность и средняя цена продажи за период финотчёта."""
    x=_pm(a)
    if not x or not price: return None
    k=x.get('var_pct') or VAR_AVG; m=x['margin_pct']/100; p0=x['avg_price']
    return ((1-k)-p0*(1-k-m)/price)*100
def price_for_margin(a,target_pct):
    """Минимальная цена, при которой маржинальность после налогов не ниже target_pct, ₽ (None – нет данных)."""
    x=_pm(a)
    if not x: return None
    k=x.get('var_pct') or VAR_AVG; m=x['margin_pct']/100; p0=x['avg_price']; den=1-k-target_pct/100
    return (p0*(1-k-m)/den) if den>0 else None
def days_of(r):
    """Остаток дней артикула: таблица (WB + свой склад; для дубля A-1/A-2 – значение оригинала A, в таблице у дубля
    часто 0), без таблицы – запас по складам WB из движка."""
    src=TURN.get('days_source') or SCARCE.get('days_source','auto'); d=None
    if src=='calc': return SDC.get(str(r['a']),{}).get('days')      # свой расчёт: (WB + фул) ÷ заказов в день за 5 дней
    a=str(r['a']); m=re.fullmatch(r'(.+)-[12]',a)
    if src in ('auto','sheet') and SDAYS:
        d=SDAYS.get(a)
        if m and m.group(1) in SDAYS: d=SDAYS[m.group(1)]
    elif src in ('auto','cover'): d=r.get('cover')
    return None if d is None else float(d)
def zone_of(d):
    for z in TZONES:
        if z.get('max_days') is None or d<=z['max_days']: return z
    return None
def scarce_of(r):
    """(дней, уровень) если артикул в дефиците, иначе None."""
    if not SC_LEVELS: return None
    d=days_of(r)
    if d is None: return None
    for lv in SC_LEVELS:
        if d<=lv['max_days']: return (d,lv)
    return None
def ff_of(k):
    k=str(k)
    if k in FF_ST: return FF_ST[k]
    m=re.fullmatch(r'(.+)-\d+',k)
    return FF_ST.get(m.group(1)) if m else None
def towb_of(k):
    k=str(k)
    if k in TOWB: return TOWB[k]
    m=re.fullmatch(r'(.+)-\d+',k)
    return TOWB.get(m.group(1)) if m else None
def off_reason(k):
    """('nophoto'|'nostock', текст) если артикул не участвует в подъёме, иначе None."""
    k=str(k)
    if k in EXCL: return None
    if PHOTOS and PHOTOS.get(k)==0: return ('nophoto','в карточке WB нет фото — карточка не работает')
    # правило «Не выставлен на складах» (FBS 0 и WB < 30) снято владельцем 30.09.2026 – осталось только «Нет на остатках»
    f=ff_of(k)
    if f is None: f=0                                           # артикула нет в таблице поставок – на фуле его нет
    # остатки WB получены сегодня из API: артикула нет в отчёте = на складах WB 0 шт.; иначе (подстановка из плана) – как есть
    w=stock.get(k,0) if STOCK_LIVE else (stock.get(k) if stock else None)
    if w is None: return None                                   # остаток WB не получен — не рискуем
    # нет на остатках: общий остаток WB + фулфилмент в Москве меньше TOT_MIN штук (решение владельца 01.10.2026; было: фул < 20 и WB < 20)
    if f+w<TOT_MIN: return ('nostock',f'нет на остатках: WB {w} шт. + фул {f} шт. = {w+f} шт., меньше {TOT_MIN}')
    return None
# локомотивы и акции (promos.py): артикулы с наибольшим расходом на рекламу нельзя малым шагом
# выводить из всех акций WB — теряется бустинг в ранжировании и просаживаются заказы. Пока
# известна проходная цена действующей акции, шаг такого артикула режется до неё; на границе —
# держать, пока владелец не решит выходить сразу большим шагом (plan.json → loco_exit_ok).
LOCO={}; CAPS={}; PROMOS={}
if A.promos and os.path.exists(A.promos):
    try:
        PROMOS=json.load(open(A.promos,encoding='utf-8'))
        LOCO={str(k):v for k,v in (PROMOS.get('loco') or {}).items()}
        CAPS={str(k):v for k,v in (PROMOS.get('cap') or {}).items()}
    except Exception as e: print(f'[warn] promos.json не прочитан: {e}',file=sys.stderr)
LOCO_KEEP=set(str(x) for x in META.get('loco_keep_promo',[]))     # кого всё же держать в акции на границе
# шаг локомотива при выходе из всех акций: удвоенный обычный, но не меньше EXIT_MIN и не больше EXIT_MAX п.п.
EXIT_MULT=float(META.get('loco_exit_mult',2.0)); EXIT_MIN=float(META.get('loco_exit_min_pp',2.5)); EXIT_MAX=float(META.get('loco_exit_max_pp',4.0))
PSTAT={str(k):v for k,v in (PROMOS.get('status') or {}).items()} if PROMOS else {}
# дубли карточек (101-1, 101-2 к 101) — тот же дизайн и размер, разные склейки.
# Собственной лестницы у дубля нет: он всегда встаёт в цену своего оригинала,
# иначе шаги кампании тратятся на карточки с 12% потока, а разрыв цен растёт.
FOLLOW=bool(META.get('follow_original',False))
# насколько дубль должен быть дороже оригинала, чтобы опускать его вручную.
# Разрыв меньше порога закрывается сам: оригинал растёт по лестнице и догоняет дубль,
# и выравнивание не стоит ни рубля маржи. Порог 5% — решение владельца 10.09.2026.
FOLLOW_GAP=float(META.get('follow_down_gap',5))

days=[(START+datetime.timedelta(days=i)).isoformat() for i in range((LAST-START).days+1)]
di={d:i for i,d in enumerate(days)}
art=defaultdict(lambda:{'c':defaultdict(int),'p':defaultdict(list),'nm':set()}); today_cnt=defaultdict(int)
for r in raw:
    d=r['date'][:10]
    if SUBJ and r.get('subject')!=SUBJ: continue
    if d==TODAY.isoformat(): today_cnt[r['supplierArticle']]+=1; continue
    if d not in di: continue
    a=art[r['supplierArticle']]; a['c'][d]+=1; a['nm'].add(r['nmId'])
    if not r.get('isCancel') and r.get('priceWithDisc'): a['p'][d].append(r['priceWithDisc'])

# аномальные дни (plan.json → anomaly_days): самозаказы, спасение товара со складов и т. п. Такой день
# считается «обычным средним»: по каждому артикулу заказы этого дня заменяются средним соседних дней
# (±3 дня, сами аномальные дни в среднее не входят), цены заказов за этот день не учитываются.
# 01.08.2026: 1 962 заказа за сутки (самозаказы из-за пожаров на складах) — решение владельца 16.09.2026.
ANOM=set(str(x) for x in META.get('anomaly_days',[]))
if ANOM & set(days):
    for k,a in art.items():
        for d in days:
            if d not in ANOM: continue
            i=di[d]; nb=[days[j] for j in range(max(0,i-3),min(len(days),i+4)) if days[j] not in ANOM]
            a['c'][d]=round(sum(a['c'].get(x,0) for x in nb)/len(nb)) if nb else 0
            a['p'][d]=[]
    print(f"[info] аномальные дни заменены средним соседних: {', '.join(sorted(ANOM & set(days)))}",file=sys.stderr)

# краткосрочный индекс сезонности. Источник — plan.json → season_source:
#   "control"   — заказы контрольной группы (исходная методика);
#   "frequency" — частотность поисковых запросов WB по всему рынку «товарыь» (market.json → mkt, снимает
#                 панель по товарым из отчёта «Поисковые запросы»). Решение владельца 16.09.2026: контрольные
#                 артикулы переезжают между склейками и то в рекламе, то нет — их заказы не измеряют сезон,
#                 а частотность запросов не зависит от наших цен, рекламы и склеек (корреляция с заказами
#                 портфеля до кампании 0,53 против 0,24 у контроля). Сглаживание то же: ±3 дня, норма — среднее за 45 дней.
SEASON_SRC=META.get('season_source','control'); SEASON_SERIES=META.get('season_series','mkt')
cd=[sum(art[k]['c'].get(d,0) for k in CTRL if k in art) for d in days]
season_used='control'
if SEASON_SRC=='frequency':
    try:
        _mk=json.load(open(A.market,encoding='utf-8')); _md={d:i for i,d in enumerate(_mk['days'])}
        _s=[(_mk[SEASON_SERIES][_md[d]] if d in _md and _md[d]<len(_mk[SEASON_SERIES]) else None) for d in days]
        _have=sum(1 for v in _s if v is not None)
        if _have>=len(days)*0.8:
            for i,v in enumerate(_s):
                if v is None:
                    _p=next((_s[j] for j in range(i-1,-1,-1) if _s[j] is not None),None); _n=next((_s[j] for j in range(i+1,len(_s)) if _s[j] is not None),None)
                    _s[i]=(_p+_n)/2 if _p is not None and _n is not None else (_p if _p is not None else _n)
            cd=_s; season_used='frequency'
        else:
            print(f'[warn] частотности ({SEASON_SERIES}) хватает лишь на {_have} из {len(days)} дней — сезон считается по контрольной группе',file=sys.stderr)
    except Exception as e:
        print(f'[warn] сезон по частотности не собрался: {e} — сезон по контрольной группе',file=sys.stderr)
mc=statistics.mean(cd) or 1
IDX=[max(0.25,statistics.mean(cd[max(0,i-3):min(len(days),i+4)])/mc) for i in range(len(days))]

def cdf(k,lam):
    # P(Poisson(lam) <= k) через логарифмы: прямое lam**i / i! переполняется уже при lam ~ 150
    # (длинное окно после давнего шага), а exp(-lam) обнуляется при lam > 745
    return sum(math.exp(i*math.log(lam)-lam-math.lgamma(i+1)) for i in range(int(k)+1)) if lam>0 else 1.0
def thr_of(E):
    if E<=0: return 0
    c=0.0
    for k in range(0,int(E)+2):
        c+=math.exp(k*math.log(E)-E-math.lgamma(k+1))
        if c>=0.10: return max(k-1,0)
    return int(E)

# ---- CPO локомотива (кампания ОП; правило владельца 30.09.2026) ----
# CPO по формуле рекламного сервиса: расход на артикул во всех его кампаниях ÷ (рекламные заказы артикула + заказы
# других артикулов по этим кампаниям). Решение – по последним 30 рекламным заказам (на дни не смотрим), с поправкой
# на день недели (CPO этих дней к обычному за 8 недель). До 180 ₽ – обычный шаг, 180–220 – половинный, выше 220 – не поднимаем.
# После подъёма: день смены цены не считаем, копим 30 заказов со следующего дня (не дольше 7 дней); CPO выше 220 и
# конверсия клик → заказ значимо ниже недели до шага – откат на цену до шага; выше 220 без падения конверсии – держим (ставки).
def cpo_eval(r,k):
    CR=OPT.get('loco_cpo') or {}
    if not (OP_ON and CR and LOCO.get(k) and CPOD.get(k)): return None
    dd={d:(sp,o,kk) for d,sp,o,kk in CPOD[k]}
    NEED=float(CR.get('orders',30)); MAXD=int(CR.get('max_days',7)); LO,HI=float(CR.get('min',180)),float(CR.get('max',220))
    b0=(TODAY-datetime.timedelta(days=56)).isoformat()
    wk=[[0.0,0.0] for _ in range(7)]
    for d,(sp,o,kk) in dd.items():
        if b0<=d<today_s: w=datetime.date.fromisoformat(d).weekday(); wk[w][0]+=sp; wk[w][1]+=o
    S=sum(x[0] for x in wk); O=sum(x[1] for x in wk); avg=S/O if O else None
    fw=[((x[0]/x[1])/avg if (avg and x[1]>=10) else 1.0) for x in wk]
    def win(ds):
        sp=sum(dd.get(d,(0,0,0))[0] for d in ds); o=sum(dd.get(d,(0,0,0))[1] for d in ds); kk=sum(dd.get(d,(0,0,0))[2] for d in ds)
        wf=(sum(dd.get(d,(0,0,0))[0]*fw[datetime.date.fromisoformat(d).weekday()] for d in ds)/sp) if sp else 1.0
        return sp,o,kk,wf
    ds=[]; o=0.0; d=LAST
    for _ in range(14):
        ds.append(d.isoformat()); o+=dd.get(d.isoformat(),(0,0,0))[1]
        if o>=NEED: break
        d-=datetime.timedelta(days=1)
    sp,o,kk,wf=win(ds)
    res={'min':LO,'max':HI,'need':int(NEED)}
    if o>=NEED*0.5 and sp>0:
        raw=sp/o; adj=raw/wf
        res.update({'now':round(adj),'raw':round(raw),'s':round(sp),'o':round(o,1),'from':ds[-1],'to':ds[0],'wf':round(wf,2),
                    'zone':'stop' if adj>HI else ('half' if adj>LO else 'ok')})
    else:
        res.update({'now':None,'s':round(sp),'o':round(o,1),'zone':'few'})
    # прошлый такой же период – предыдущие NEED рекламных заказов до текущего окна (динамика на плитке, 13.10.2026)
    pds=[]; po=0.0; d=datetime.date.fromisoformat(ds[-1])-datetime.timedelta(days=1)
    for _ in range(14):
        pds.append(d.isoformat()); po+=dd.get(d.isoformat(),(0,0,0))[1]
        if po>=NEED: break
        d-=datetime.timedelta(days=1)
    psp,po,pkk,pwf=win(pds)
    if po>=NEED*0.5 and psp>0:
        padj=psp/po/pwf
        res['prev']={'now':round(padj),'raw':round(psp/po),'o':round(po,1),'from':pds[-1],'to':pds[0],'zone':'stop' if padj>HI else ('half' if padj>LO else 'ok')}
    lu=r.get('last_up')
    # проверка «после шага» – только для свежих шагов (не старше 14 дн.); старые покрывает скользящий CPO
    if lu and lu['d']>=META['start'] and lu['d']<today_s and (TODAY-datetime.date.fromisoformat(lu['d'])).days<=14:
        sd=datetime.date.fromisoformat(lu['d']); aft=[]; o_a=0.0; d=sd+datetime.timedelta(days=1)
        while d<=LAST and len(aft)<MAXD:
            aft.append(d.isoformat()); o_a+=dd.get(d.isoformat(),(0,0,0))[1]
            if o_a>=NEED: break
            d+=datetime.timedelta(days=1)
        bef=[(sd-datetime.timedelta(days=i)).isoformat() for i in range(1,8)]
        sa,oa,ka,wfa=win(aft); sb,ob,kb,wfb=win(bef)
        ready=(oa>=NEED) or (len(aft)>=MAXD)
        cb=(ob/kb) if kb else None
        exp=(ka*cb/wfa) if cb else None
        drop=bool(exp is not None and exp>=3 and oa<=thr_of(exp))
        res['after']={'step':lu['d'],'from':lu['from'],'to':lu['to'],'o':round(oa,1),'days':len(aft),'ready':ready,
                      'cpo':round(sa/oa/wfa) if oa else None,'cpo_before':round(sb/ob/wfb) if ob else None,
                      'conv':round(oa/ka*100,2) if ka else None,'conv_before':round(cb*100,2) if cb else None,
                      'exp':round(exp,1) if exp is not None else None,'drop':drop}
    return res

L7=days[-7:]
rows=[]
for k,a in art.items():
    q=[a['c'].get(d,0) for d in days]
    if sum(q)==0 or k in EXCL: continue
    p=[];carry=None;psrc=[]
    for d in days:
        v=HIST.get(d,{}).get(k)                       # снимок цен кабинета за этот день — точный
        s='hist' if v is not None else 'ord'
        if v is None and a['p'].get(d):
            vals=[round(x) for x in a['p'][d]]
            v=statistics.mode(vals)                   # иначе мода цен заказов, а не медиана
        if v is not None: carry=v
        p.append(carry); psrc.append(s)
    f=next((x for x in p if x is not None),None)
    if f is None: continue
    p=[x if x is not None else f for x in p]
    steps=[];prev=None
    for i,d in enumerate(days):
        # на стыке двух источников цены (последний день по заказам → первый день по снимку)
        # разница источников — не движение цены, поэтому такой переход шагом не считаем
        seam = i>0 and psrc[i]!=psrc[i-1]
        if prev is not None and not seam:
            if p[i]>=prev*1.008: steps.append({'d':d,'from':round(prev),'to':round(p[i]),'up':1})
            elif p[i]<=prev*0.992: steps.append({'d':d,'from':round(prev),'to':round(p[i]),'up':0})
        prev=p[i]
    ups=[s for s in steps if s['up']]
    last_up=ups[-1] if ups else None
    # для решения об откате важны только шаги, сделанные кампанией: то, что цену
    # поднимали до её старта, к нашим действиям отношения не имеет
    ups_camp=[s for s in ups if s['d']>=META['start']]
    pl=plan.get(k,{})
    anchor=pl.get('anchor') or round(p[0]); src=pl.get('src','старт')
    base14=sum(q[-14:])/14; q7=sum(q[-7:])/7
    st=stock.get(str(k))
    if st==0: st=None          # 0 в отчёте WB = товара нет на складах WB (FBS), а не «нет товара»
    cover=(st/base14) if (st is not None and base14>0) else None
    if last_up:
        si=di[last_up['d']]; bd=list(range(max(0,si-14),si))
        base=(sum(q[i]/IDX[i] for i in bd)/len(bd)) if bd else base14
        win=list(range(si,len(days))); O=sum(q[i] for i in win); E=base*sum(IDX[i] for i in win); n=len(win)
    else:
        base=sum(q[i]/IDX[i] for i in range(len(days)-14,len(days)))/14
        O=sum(q[-4:]); E=base*sum(IDX[-4:]); n=4
    tier='A' if base>=3 else ('B' if base>=1 else 'C')
    rows.append({'a':k,'nm':sorted(a['nm'])[0],'q':q,'p':p,'steps':steps,'last_up':last_up,
                 'up_camp':(ups_camp[-1] if ups_camp else None),
      'q5':sum(q[-5:])/5,
      'base':base,'O':O,'E':E,'n':n,'need':{'A':4,'B':7,'C':4}[tier],'tier':tier,
      'stock':st,'cover':cover,'p_now':p[-1],'q7':q7,'base14':base14,'today':today_cnt.get(k,0),
      'anchor':anchor,'src':src,'qjun':pl.get('qjun'),'tgt_pct':pl.get('tgt_pct'),
      'plan_mode':pl.get('mode','поднимать'),'now_pct':(p[-1]/anchor-1)*100,
      'p_ord':p[-1],'p_fact':price_now.get(str(k))})

# «цена сейчас» = фактическая цена кабинета, если она известна; иначе цена последнего заказа
stale=0
for r in rows:
    if r['p_fact']:
        if abs(r['p_fact']-r['p_ord'])>=2: stale+=1
        r['p_now']=r['p_fact']; r['now_pct']=(r['p_fact']/r['anchor']-1)*100

# ---------- поток: база кампании и бюджет падения ----------
# БАЗА = уровень заказов артикула за 14 дней до старта кампании, приведённый к сезону
# по контрольной группе (её цены заморожены). Бюджет = FLOOR% от ожидаемых заказов
# с начала кампании; расходуется накопленной недостачей факта против ожидания.
JUN_DAY=META['june_orders_day']
# остаток дней считаем сами (решение владельца 30.09.2026): (штук на складах WB + штук на фулфилменте в Москве) ÷ заказов в день
# за 5 последних полных дней. Фулфилмент – таблица поставок (avail.json → ff). Дубль A-1/A-2 делит физический остаток с
# оригиналом A и сам почти не продаётся, поэтому у дубля остаток дней берётся как у оригинала.
SDC={}
_byrow={str(r['a']):r for r in rows}
# сезонная оборачиваемость (turnover.season=true; решение владельца 09.10.2026): тот же расчёт, что колонка AE «Остаток дней
# ВБ + Фул Москва» в план-факте – (WB + в отгрузке на ВБ + на ФФ Москва) расходуется день за днём, скорость за 5 дней =
# уровень текущего месяца, каждый следующий месяц × коэффициент роста из «Рассчета поставки» (season.json, season_days.py)
SEASON={}
if A.season and os.path.exists(A.season):
    try: SEASON=json.load(open(A.season,encoding='utf-8'))
    except Exception: SEASON={}
SEAS_ON=bool(TURN.get('season')) and bool(SEASON.get('coefs'))
def _season_f():
    co={int(k):float(v) for k,v in SEASON['coefs'].items()}; idx={0:1.0}
    for k in range(1,12):
        m=(TODAY.month-1+k)%12+1; idx[k]=idx[k-1]*co[m]
    idx[-1]=1.0/co[TODAY.month]
    def f(d):
        k=(d.year-TODAY.year)*12+d.month-TODAY.month
        return idx[-1] if k==-1 else idx[k%12]
    return f
SEAS_F=_season_f() if SEAS_ON else None
def season_days_left(stock,speed,window):
    if stock<=0: return 0.0
    win=[TODAY-datetime.timedelta(days=i) for i in range(1,window+1)]
    base=speed/(sum(SEAS_F(d) for d in win)/window); d=TODAY; n=0.0
    while n<3650:
        daily=base*SEAS_F(d)
        if stock<=daily: return n+stock/daily
        stock-=daily; n+=1; d+=datetime.timedelta(days=1)
    return 3650.0
def _sdc(r):
    a=str(r['a'])
    wb=int(stock.get(a,0) or 0) if STOCK_LIVE else int(r.get('stock') or 0)
    ff=ff_of(a); q5=r['q5']; tot=wb+(ff or 0)
    # скорость: за 5 дней; если за 5 дней меньше 3 заказов – за 28 дней (иначе 0–2 заказа дают 999+ дней);
    # нет заказов и за 28 дней – «нет продаж» (для зон движка это по-прежнему огромный запас)
    short=int(OPT.get('speed_short_days',5)) if OP_ON else 5
    long=int(OPT.get('speed_long_days',28)) if OP_ON else 28
    need=int(OPT.get('speed_min_orders',3)) if OP_ON else 3
    qq=r['q']
    q28=sum(qq[-28:])/28
    q_short=sum(qq[-short:])/short if short else q5
    q_long=sum(qq[-long:])/long if long else 0
    if sum(qq[-short:])>=need: sp,win=q_short,short
    elif q_long>0: sp,win=q_long,long
    else: sp,win=0.0,None
    # остаток фула – сумма «Остаток на ФФ 1» (BE) + «Остаток на ФФ с учетом китайской поставки» (BF), с 27.10; до 26.10 – «Остаток на FBO» BC; из «Рассчет поставки» (avail.json → ff, тот же источник, что у правила
    # «нет на остатках»); «в отгрузке на ВБ» и «на ФФ Москва» из план-факта (season.json) не используются (владелец 15.10.2026)
    to_wb=int(towb_of(a) or 0)                                  # «Товары в пути на склад ВБ» («Рассчет поставки», колонка по названию; 26.10 – ADK)
    tot=wb+(ff or 0)+to_wb
    if SEAS_ON:
        days=season_days_left(tot,sp,win) if sp>0 else (9999.0 if tot>0 else 0.0)
    else:
        days=(tot/sp) if sp>0 else (9999.0 if tot>0 else 0.0)
    return {'days':round(min(days,9999.0),1),'wb':wb,'ff':ff,'to_wb':to_wb,'q5':round(q5,1),'q28':round(q28,2),'win':win,
            'nosales':bool(sp==0 and tot>0),'out':tot<=0,'season':SEAS_ON,'of':None}
for r in rows:
    a=str(r['a']); m=re.fullmatch(r'(.+)-[12]',a)
    if m and m.group(1) in _byrow:
        o=_sdc(_byrow[m.group(1)]); own=_sdc(r)
        SDC[a]={**o,'of':m.group(1),'wb_own':own['wb'],'q5_own':own['q5']}
    else: SDC[a]=_sdc(r)
OFF={r['a']:off_reason(r['a']) for r in rows if off_reason(r['a'])}
FLOWROWS=[r for r in rows if r['a'] not in OFF]                  # без «нет на остатках / нет фото»
flow_now=sum(r['q7'] for r in FLOWROWS)
flow_abs=flow_now/JUN_DAY-1                      # справочно: весь поток к июню (в основном сезон)
camp=[i for i,d in enumerate(days) if d>=META['start']]
pre_end=camp[0] if camp else len(days)
for r in rows:
    st_a=state['arts'].setdefault(r['a'],{'strikes':0,'pause_until':None})
    # База и ожидание должны считаться под одну и ту же нормировку сезонного индекса. Индекс нормируется
    # на среднее контроля за скользящие 45 дней и меняется каждый прогон; если базу зафиксировать один раз
    # (как было), то при выходе аномального дня из окна (01.08.2026: 1 962 заказа за сутки, 106 по контрольному
    # 111) ожидание прыгает на треть, а факт «падает» на 24% при растущих заказах — так 16.09.2026 кампания
    # ложно ушла в красную зону. Поэтому пока базовые 14 дней до старта целиком в окне, база пересчитывается
    # каждый прогон; когда окно уедет дальше — остаётся последнее сохранённое значение.
    bd=list(range(max(0,pre_end-14),pre_end))
    if len(bd)==14 and pre_end-14>=0:
        st_a['cbase']=round(sum(r['q'][i]/IDX[i] for i in bd)/14,4)
    elif 'cbase' not in st_a:
        st_a['cbase']=round(sum(r['q'][i]/IDX[i] for i in bd)/len(bd),4) if bd else round(r['base14'],4)
    r['cbase']=st_a['cbase']
    r['cexp']=r['cbase']*sum(IDX[i] for i in camp)
    r['cact']=sum(r['q'][i] for i in camp)
    r['spent']=((r['cexp']-r['cact'])/(FLOOR*r['cexp'])) if r['cexp']>0 else 0.0
    r['cdrop']=(r['cact']/r['cexp']-1) if r['cexp']>0 else 0.0
NC=[r for r in FLOWROWS if r['a'] not in CTRL]
exp_tot=sum(r['cexp'] for r in NC); act_tot=sum(r['cact'] for r in NC)
drop_now=(act_tot/exp_tot-1) if exp_tot>0 else 0.0
spent_tot=((exp_tot-act_tot)/(FLOOR*exp_tot)) if exp_tot>0 else 0.0
# бюджет начинает действовать только когда накопилось достаточно дней — иначе шум первых дней
MINDAYS=4
zone='green' if len(camp)<MINDAYS else ('red' if spent_tot>=RED else ('yellow' if spent_tot>=YEL else 'green'))
if season_used=='frequency':
    # сезон = частотность за последние 7 дней к среднему за 45 дней (июньской частотности у нас нет)
    season=(statistics.mean(cd[-7:])/mc-1) if mc else 0.0
else:
    season=(sum(r['q7'] for r in rows if r['a'] in CTRL)/max(0.01,sum(r['qjun'] or 0 for r in rows if r['a'] in CTRL))-1) if CTRL else 0.0
flow_price=drop_now
days_left=max(1,(END-TODAY).days); rem_steps=max(1,math.ceil(days_left/3.5))
def fmt(n): return f'{round(n):,}'.replace(',',' ')
today_s=TODAY.isoformat(); acts=[]
for r in rows:
    k=r['a']; s=state['arts'].get(k,{'strikes':0,'pause_until':None})
    thr=thr_of(r['E'])
    tgt=r['tgt_pct'] if r['tgt_pct'] is not None else META['target_pct']
    t_days=days_of(r) if TURN_ON else None; tz=None
    if TURN_ON and t_days is not None and tgt and tgt>0:        # tgt 0 в плане = артикул не поднимаем вовсе
        tz=zone_of(t_days)
        if tz: tgt=float(tz['tgt'])
    op_goal=None
    # кампания ОП (решение владельца 09.10.2026): ОП выше 15%, остаток большой, а продаж мало – агрессивно снижаем до ОП 15%
    # и быстро смотрим, как меняется скорость продаж; цель цены = цена под ОП over_op, подъёма нет
    _mg=margin_of(k)
    _ld=max(1,int(OPT.get('low_days',7))) if OP_ON else 7
    _qw=sum(r['q'][-_ld:])/_ld
    _low_op=float(OPT.get('low_op', OPT.get('over_op',15)))
    lowq=bool(OP_ON and _mg is not None and _mg>_low_op and _qw<float(OPT.get('low_q',3.0))
              and t_days is not None and t_days>float(OPT.get('low_min_days',60)))
    r['lowq']={'q7':round(_qw,2),'none':_qw==0,'days':_ld} if lowq else None
    if OP_ON and tz is not None or (OP_ON and t_days is None):
        _loco=bool(LOCO.get(k))
        if lowq: op_goal=_low_op
        elif t_days is not None and t_days>float(TURN.get('no_raise_days', TURN.get('over_days',250))): op_goal=float(OPT.get('over_op',15))
        elif _loco: op_goal=float(OPT.get('loco_op',8))
        else:
            _z=[z for z in sorted(OPT.get('zones',[]),key=lambda z:z['max_days']) if t_days is not None and t_days<=z['max_days']]
            op_goal=float((_z[0] if _z else (sorted(OPT.get('zones',[{'op':20}]),key=lambda z:z.get('max_days',0))[-1]))['op'])
        _preq=price_for_margin(k,op_goal)
        if _preq and r.get('anchor'):
            tgt=(_preq/r['anchor']-1)*100
            tgt=min(tgt,r['now_pct']+float(OPT.get('max_raise_pp',25)))
        else: op_goal=None                              # нет данных финотчёта – остаётся цель зоны по остатку
    gap=tgt-r['now_pct']
    step=max(0.0,min(float(OPT.get('step_max_pp',2.5)) if OP_ON else 2.5,gap/rem_steps))
    half = (zone=='yellow') or (r['spent']>=YEL and r['cexp']>=8)
    CP=cpo_eval(r,k); r['cpo']=CP
    cpo_half = bool(CP and CP.get('zone')=='half')
    if half or cpo_half: step=step/2
    new=round(r['anchor']*(1+(r['now_pct']+step)/100))
    sc=scarce_of(r); r['scarce']=None
    if sc:
        sc_days,lv=sc
        if not tz: tgt=tgt+float(lv.get('tgt_plus',0))     # при включённой оборачиваемости цель задаёт зона
        gap=tgt-r['now_pct']
        step=max(0.0,min(float(lv.get('step_max',5.0)),max(gap/rem_steps,float(lv.get('step_min',0))),max(gap,0.0)))
        if half and not SCARCE.get('ignore_half',True): step=step/2
        new=round(r['anchor']*(1+(r['now_pct']+step)/100))
        r['scarce']={'days':round(sc_days,1),'level':lv['max_days'],'tgt':round(tgt,1),'step':round(step,1)}
    # Порог «затоварен» – число из настроек (no_raise_days). over_days оставлен запасным:
    # сохранение настроек раньше писало только no_raise_days, и снижение шло по старому over_days.
    NO_UP_DAYS=float(TURN.get('no_raise_days',TURN.get('over_days',250)))
    over=bool(tz) and t_days>NO_UP_DAYS
    no_up=(bool(tz) and t_days>NO_UP_DAYS) or lowq
    sc_nopause=TURN_ON and bool(sc) and sc[0]<=float(TURN.get('scarce_ignore_pause_days',50))
    mgp=margin_of(k); DMIN=(_low_op if lowq else float(OPT.get('over_op',15))) if (OP_ON and op_goal is not None) else float(TURN.get('down_min_margin_pct',12))   # кампания ОП: снижать не ниже своей цели ОП
    r['op_goal']=op_goal
    r['turn']={'days':round(t_days,1),'tgt':tgt,'over':over,'no_up':no_up,'zone':tz.get('max_days'),'down':False} if tz else None
    # снижение затоваренного: цена выше цели зоны, заказы к базе хуже портфеля, маржа позволяет, не чаще раза в N дней
    down_new=None; down_why=None
    # шаг и частота снижения: кампания ОП – мельче и чаще (решение владельца 09.10.2026: ~1% до 3 раз в неделю)
    D_STEP=float(OPT.get('down_step_pp',TURN.get('down_step_pp',2.0))) if OP_ON else float(TURN.get('down_step_pp',2.0))
    D_EVERY=int(OPT.get('down_every_days',TURN.get('down_every_days',7))) if OP_ON else int(TURN.get('down_every_days',7))
    if lowq:
        D_STEP=float(OPT.get('low_step_none_pp',3.0)) if r['q7']==0 else float(OPT.get('low_step_pp',2.0))
        D_EVERY=int(OPT.get('low_every_days',2))
    def _applied(d0):
        # снижение от d0 реально поставлено: после него цена (история + цена кабинета сейчас) опускалась на 1%+
        if d0 not in days: return True
        _i=days.index(d0); _pb=r['p'][_i-1] if _i>0 else None
        _after=[v for v in r['p'][_i:] if v]+([r['p_now']] if r.get('p_now') else [])
        return bool(_pb) and any(v<=_pb*0.99 for v in _after)
    if (over or lowq) and r['now_pct']>tgt+0.5:
        _dl=s.get('down_last'); _ago=(TODAY-datetime.date.fromisoformat(_dl)).days if _dl else 999
        if OP_ON:
            # рекомендованное, но не поставленное снижение не считаем (ошибка 30.09: 40 артикулов заблокированы зря)
            if _dl and _ago>=1 and not _applied(_dl):
                if s.get('down_stop')==_dl: s.pop('down_stop',None)
                if s.get('down_start')==_dl: s.pop('down_start',None)
                s['down_last']=None; _dl=None; _ago=999
            # проверка серии снижений: неделя после первого снижения серии против недели до неё (заказы, шт)
            _ds=s.get('down_start')
            # «мало продаж»: быстрая оценка – скорость продаж с начала серии против 7 дней до неё; выросла вдвое и до low_q в день –
            # цена найдена, держим (down_ok); иначе следующий шаг. Недельная проверка «не дали заказов – стоп» для них не действует
            if lowq and _ds and _applied(_ds) and (TODAY-datetime.date.fromisoformat(_ds)).days>=2:
                _n=(LAST-datetime.date.fromisoformat(_ds)).days
                _qa=sum(q for d,q in zip(days,r['q']) if _ds<d)/max(1,_n)
                _qb=sum(q for d,q in zip(days,r['q']) if (datetime.date.fromisoformat(_ds)-datetime.timedelta(days=7)).isoformat()<=d<_ds)/7
                r['lowq'].update({'q_before':round(_qb,2),'q_after':round(_qa,2),'since':_ds})
                _need=max(float(OPT.get('low_ok_min',1.0)),2*_qb)
                if _qa>=_need and _qa>0: s['down_ok']=_ds; s['down_ok_need']=round(_need,2)
            # цена найдена, но скорость снова упала (меньше 60% от нужной) – начинаем новую серию снижений
            if lowq and s.get('down_ok') and r['q7']<float(s.get('down_ok_need') or 1.0)*0.6:
                for _k in ('down_ok','down_ok_need','down_start'): s.pop(_k,None)
            if lowq: _ds=None
            if _ds and (TODAY-datetime.date.fromisoformat(_ds)).days>=8 and not s.get('down_stop'):
                if not _applied(_ds): s.pop('down_start',None)
                else:
                    _e=(datetime.date.fromisoformat(_ds)+datetime.timedelta(days=7)).isoformat(); _b=(datetime.date.fromisoformat(_ds)-datetime.timedelta(days=7)).isoformat()
                    _qa=sum(q for d,q in zip(days,r['q']) if _ds<d<=_e); _qb=sum(q for d,q in zip(days,r['q']) if _b<=d<_ds)
                    if _qa<=_qb: s['down_stop']=_ds; s['down_stop_q']=[_qa,_qb]
                    else: s.pop('down_start',None)                 # заказы выросли – серия удалась, следующее снижение начнёт новую
        if len(camp)<MINDAYS: down_why='кампания только началась'
        elif r['cdrop']>=drop_now and not OP_ON: down_why=f'заказы к базе {r["cdrop"]*100:+.0f}% не хуже портфеля ({drop_now*100:+.0f}%)'
        elif lowq and s.get('down_ok') and s.get('down_ok')==s.get('down_start'):
            _x=r['lowq'] or {}
            down_why=f'после снижений с {s["down_ok"][8:10]}.{s["down_ok"][5:7]} заказы выросли ({_x.get("q_before","?")} → {_x.get("q_after","?")} в день) – цену держим'
        elif OP_ON and s.get('down_stop') and not lowq:
            _st=s['down_stop']; _q=s.get('down_stop_q')
            down_why=f'снижения с {_st[8:10]}.{_st[5:7]} не дали заказов'+(f' ({_q[0]} шт. за 7 дней после против {_q[1]} до)' if _q else '')+' – дальше не снижаем'
        else:
            _pmin=price_for_margin(k,DMIN); _mnow=margin_at(k,r['p_now'])
            if _pmin is None: down_why='нет данных о маржинальности'
            elif _ago<D_EVERY: down_why=f'снижали {_ago} дн. назад, следующее снижение – через {D_EVERY} дн.'
            else:
                _np=max(r['now_pct']-D_STEP,tgt)
                _bp=max(round(r['anchor']*(1+_np/100)),math.ceil(_pmin))     # не ниже цены, дающей маржинальность DMIN
                if _bp<r['p_now']*0.998: down_new=_bp
                elif _pmin>=r['p_now']*0.998: down_why=f'маржинальность после налогов при текущей цене {_mnow:.1f}% – ниже {DMIN:g}% опускаться нельзя (граница {fmt(_pmin)} ₽)'
                else: down_why='цена уже у цели зоны'
    # индивидуальный бюджет потока к июню
    own=(r['q7']/r['qjun']-1) if (r.get('qjun') and r['qjun']>=0.5) else None
    lab='к июню' if r['src']=='июнь' else 'к цене старта'
    act=kind=note=None
    # ---- CPO локомотива (кампания ОП): проверка после шага и скользящий CPO ----
    cpo_act=None; _dm=lambda d: f'{d[8:10]}.{d[5:7]}'
    if CP and not sc_nopause and gap>0.3:
        AF=CP.get('after')
        if AF and s.get('cpo_done')!=AF['step']:
            if not AF['ready']:
                cpo_act=(f'Ждать – CPO после шага: {AF["o"]:.0f} из {CP["need"]} заказов','wait',
                         f'цену подняли {_dm(AF["step"])} ({fmt(AF["from"])} → {fmt(AF["to"])} ₽); день смены цены не считаем, решение – после {CP["need"]} рекламных заказов (свои + других артикулов), но не позже чем через 7 дн.',None,False)
            elif (AF['o']>=CP['need'] and AF['cpo'] and AF['cpo']>CP['max'] and AF['drop'] and r['up_camp']
                  and (not AF['cpo_before'] or AF['cpo_before']<=CP['max'] or AF['cpo']>=AF['cpo_before']*1.15)):
                # откат – только если CPO вышел за предел из-за шага: до шага был в пределе или вырос на 15% и больше
                _b=min(int(AF['from']),r['p_now'])
                cpo_act=(f'Откатить до {fmt(_b)} ₽','back',
                         f'CPO после подъёма {AF["cpo"]} ₽ при пределе {CP["max"]:g} ₽ (до шага {AF["cpo_before"]} ₽); конверсия клик → заказ {AF["conv"]}% против {AF["conv_before"]}% – падение не случайное, цена мешает рекламе: возвращаем цену до шага, пауза 14 дн.',_b,True)
            elif AF['o']>=CP['need'] and AF['cpo'] and AF['cpo']>CP['max'] and (CP.get('now') is None or CP['now']>CP['max']):
                _why=(f'конверсия клик → заказ не упала ({AF["conv"]}% против {AF["conv_before"]}%)' if not AF['drop']
                      else f'CPO был выше предела и до шага ({AF["cpo_before"]} ₽) и почти не вырос')
                cpo_act=(f'Держать – CPO {CP.get("now") or AF["cpo"]} ₽ выше {CP["max"]:g}','hold',
                         f'CPO сейчас {CP.get("now") or "—"} ₽; после подъёма {_dm(AF["step"])} был {AF["cpo"]} ₽ (до шага {AF["cpo_before"]} ₽), но {_why} – причина не в цене: проверьте ставки и кластеры. Цену не поднимаем, пока CPO не опустится ниже {CP["max"]:g} ₽',None,True)
            elif AF['o']<CP['need'] and AF['drop'] and r['up_camp']:
                _b=min(int(AF['from']),r['p_now'])
                cpo_act=(f'Откатить до {fmt(_b)} ₽','back',
                         f'за 7 дн. после подъёма только {AF["o"]:.0f} рекламных заказов, конверсия клик → заказ {AF["conv"]}% против {AF["conv_before"]}% до шага – падение не случайное: возвращаем цену до шага, пауза 14 дн.',_b,True)
            else:
                s['cpo_done']=AF['step']                          # проверка после шага пройдена
        if cpo_act is None and CP.get('zone')=='stop':
            cpo_act=(f'Держать – CPO {CP["now"]} ₽ выше {CP["max"]:g}','hold',
                     f'CPO за последние {CP["o"]:.0f} рекламных заказов ({_dm(CP["from"])}–{_dm(CP["to"])}) – {CP["now"]} ₽ при пределе {CP["max"]:g} ₽: подъём остановлен, пока CPO не опустится ниже предела',None,False)
    frz=FREEZE.get(k)
    if k in CTRL:
        act,kind='Не трогать','ctrl'; note=f'контрольная группа — по ней считается сезон, цена заморожена до {END.strftime("%d.%m")}'
    elif frz and frz['until']>today_s:
        # артикул в акции WB: выход из акции поднимает ДРР (проверено опытным путём,
        # на комиссию это не влияет), поэтому по нему не считаются ни подъёмы, ни откаты —
        # цену держим до конца срока
        u=datetime.date.fromisoformat(frz['until'])
        act,kind=f'Не трогать до {u.strftime("%d.%m")}','ctrl'
        note=f'{frz.get("why","заморожен")}: цена зафиксирована владельцем, выход из акции поднимает ДРР — лестница по нему не считается'
    elif k in FB_HOLD and FB_HOLD[k]['until']>today_s:
        # сотрудник отметил на странице «не ставлю» (акция WB, спорный товар, своё решение):
        # до указанной даты артикул не поднимается и не откатывается, как при заморозке
        hb=FB_HOLD[k]; u=datetime.date.fromisoformat(hb['until'])
        act,kind=f'Отложено до {u.strftime("%d.%m")}','skip'
        note=f'отметка сотрудника от {hb.get("from","")[8:10]}.{hb.get("from","")[5:7]}: {hb.get("why","не ставим")} — до {u.strftime("%d.%m")} артикул не поднимается и не откатывается'
    elif k in OFF:
        # нет на остатках или нет фото (avail.py, правило доступности): карточка не продаёт
        # по причинам, не связанным с ценой, — лестница по ней не считается, в бюджете потока не участвует
        okind,why=OFF[k]
        act,kind={'nophoto':'Нет фото на WB','unlisted':'Не выставлен на складах'}.get(okind,'Нет на остатках'),'off'
        note=why+' — в подъёме цен не участвует, заказы не идут в расчёт бюджета потока'
    elif down_new is not None:
        new=down_new; step=0.0
        act,kind=f'Снизить до {fmt(down_new)} ₽','back'
        _lq=r.get('lowq')
        note=((f'мало заказов: {_lq["q7"]:g} заказа в день за {_lq.get("days",7)} дней при ОП выше {_low_op:g}% – снижаем быстрее'
               +(f' (с {_lq["since"][8:10]}.{_lq["since"][5:7]}: было {_lq["q_before"]:g}, стало {_lq["q_after"]:g} в день)' if _lq.get('since') else '')+'; ') if _lq else '')+((f'затоварен: остаток на {t_days:.0f} дн. (порог {NO_UP_DAYS:g}), ' if over else f'остаток на {t_days:.0f} дн., ') + ('' if op_goal is not None else f'цена {r["now_pct"]:+.1f}% при цели зоны {tgt:+.0f}%, ') +
              f'заказы к базе {r["cdrop"]*100:+.0f}% против {drop_now*100:+.0f}% по портфелю; маржинальность после налогов после снижения ≈ {margin_at(k,down_new):.1f}% (граница {DMIN:g}% – {fmt(price_for_margin(k,DMIN))} ₽) – '
              f'снижаем на {D_STEP:g} п.п., следующее снижение не раньше чем через {D_EVERY} дн.'+((f' · через {D_EVERY} дн. сверим скорость продаж: выросла вдвое (и не меньше {OPT.get("low_ok_min",1.0):g} в день) – держим цену, нет – следующий шаг' if _lq else ' · через 7 дней после начала серии снижений сверим заказы с неделей до неё') if OP_ON else ''))
        s['down_last']=today_s; s['strikes']=0; s['pause_until']=None; r['turn']['down']=True
        if OP_ON and not s.get('down_start'): s['down_start']=today_s       # начало серии снижений – от него недельная проверка
    elif COVER_CAP is not None and r['cover'] is not None and r['cover']>COVER_CAP:
        act,kind='Не поднимать','deep'; note=f'запас {r["cover"]:.0f} дней при пороге {COVER_CAP} — нужна распродажа, а не подъём'
    elif s.get('pause_until') and s['pause_until']>today_s and not sc_nopause:
        left=(datetime.date.fromisoformat(s['pause_until'])-TODAY).days
        act,kind=f'Пауза, ещё {left} дн.','pause'; note=f'ждём восстановления: факт {r["O"]} при ожидании {r["E"]:.0f}'
    elif zone=='red':
        act,kind='Стоп (лимит потока)','pause'
        note=f'портфель уже на {drop_now*100:+.0f}% к базе — израсходовано {spent_tot*100:.0f}% лимита −{FLOOR*100:.0f}%'
    elif r['spent']>=1.5 and r['cexp']>=20 and r['O']<=thr and r['E']>=3 and r['up_camp'] and not sc_nopause:
        back=round(r['anchor']*(1+(r['now_pct']-max(step,1.0))/100))
        back=min(back,r['p_now']); new=back            # откат — всегда вниз; new_price страницы = цена отката (исправлено 16.09.2026)
        act,kind=f'Откатить до {fmt(back)} ₽','back'
        note=f'бюджет перерасходован: {r["cdrop"]*100:+.0f}% к базе с начала кампании (лимит −{FLOOR*100:.0f}%), падение подтверждено и коротким окном'
        s['pause_until']=(TODAY+datetime.timedelta(days=14)).isoformat(); s['strikes']=0
    elif r['spent']>=1.5 and r['cexp']>=20 and r['O']<=thr and r['E']>=3 and not sc_nopause:
        # Откатывать нечего: с начала кампании цену этого артикула ни разу не поднимали,
        # значит его падение вызвано не нами. Держим и ждём внешней причины.
        act,kind='Держать — цену не поднимали','hold'
        note=f'{r["cdrop"]*100:+.0f}% к базе, но с {CAMP.strftime("%d.%m")} не было ни одного шага кампании — падение не ценовое, откат ничего не вернёт'
        s['pause_until']=(TODAY+datetime.timedelta(days=7)).isoformat(); s['strikes']=0
    elif r['spent']>=1.0 and r['cexp']>=8 and not sc_nopause:
        act,kind='Бюджет исчерпан — держать','hold'
        note=f'{r["cdrop"]*100:+.0f}% к базе с начала кампании при лимите −{FLOOR*100:.0f}%: цену не поднимаем, пока поток не вернётся'
    elif no_up:
        act,kind='Не поднимать','deep'
        note=(f'большой запас: остаток на {t_days:.0f} дн. при пороге {NO_UP_DAYS:g} – цену не поднимаем (цель зоны {tgt:+.0f}%, сейчас {r["now_pct"]:+.1f}%)'
              +(f'; снижение не предлагаем: {down_why}' if down_why else ''))
    elif cpo_act:
        act,kind,note=cpo_act[0],cpo_act[1],cpo_act[2]
        if kind=='back': new=cpo_act[3]; step=0.0; s['strikes']=0; s['pause_until']=(TODAY+datetime.timedelta(days=14)).isoformat()
        if cpo_act[4]: s['cpo_done']=CP['after']['step']
    elif OP_ON and LOCO.get(k) and gap>0.3 and (
            (STORE.get('drr7') is not None and STORE['drr7']>float(OPT.get('drr_max',5)))
            or (not OPT.get('loco_cpo') and (PMARG.get(k) or {}).get('drr7') is not None and (PMARG.get(k) or {}).get('drr_base')
                and PMARG[k]['drr7']>PMARG[k]['drr_base']*float(OPT.get('loco_drr_rise',1.25)))):
        _x=PMARG.get(k) or {}
        act,kind='Держать – ДРР растёт','hold'
        note=(f'локомотив: подъём остановлен, чтобы не разгонять ДРР – ДРР магазина за 7 дн. {STORE.get("drr7")}% (предел {OPT.get("drr_max",5)}%), '
              f'ДРР артикула {_x.get("drr7")}% против {_x.get("drr_base")}% в базе (предел роста ×{OPT.get("loco_drr_rise",1.25)})')
    elif gap<=0.3:
        act,kind='Держать','done'; note=(f'цель ОП {op_goal:g}% достигнута: при текущей цене ОП после налогов ≈ {margin_at(k,r["p_now"]):.1f}%' if op_goal is not None else f'цель {tgt:+.0f}% {lab} достигнута (сейчас {r["now_pct"]:+.1f}%)')
    elif r['n']<r['need'] and r['last_up'] and not (CP and CP.get('after') and s.get('cpo_done')==CP['after']['step']):
        act,kind=f'Ждать {r["need"]-r["n"]} дн.','wait'
        note=f'после шага {r["last_up"]["d"][8:10]}.{r["last_up"]["d"][5:7]} прошло {r["n"]} дн., для тира {r["tier"]} нужно {r["need"]}'
    elif r['O']<=thr and r['E']>=3 and not sc_nopause:
        if s['strikes']>=1 and r['up_camp']:
            back=round(r['anchor']*(1+(r['now_pct']-step)/100)) if step>0 else round(r['p_now']*0.985)
            back=min(back,r['p_now']); new=back          # откат — всегда вниз, никогда вверх; new_price = цена отката
            act,kind=f'Откатить до {fmt(back)} ₽','back'
            note=f'падение подтвердилось дважды: факт {r["O"]} при ожидании {r["E"]:.0f} (порог {thr})'
            s['strikes']=0; s['pause_until']=(TODAY+datetime.timedelta(days=14)).isoformat()
        elif s['strikes']>=1:
            act,kind='Держать — цену не поднимали','hold'
            note=f'падение подтвердилось дважды, но с {CAMP.strftime("%d.%m")} не было ни одного шага — причина не в цене'
            s['strikes']=0; s['pause_until']=(TODAY+datetime.timedelta(days=7)).isoformat()
        else:
            act,kind='Пауза 7 дней','pause'
            note=f'факт {r["O"]} при ожидании {r["E"]:.0f} — ниже порога {thr}, это не шум'
            s['strikes']=1; s['pause_until']=(TODAY+datetime.timedelta(days=7)).isoformat()
    else:
        act,kind=(f'Поднять до {fmt(new)} ₽ (цель ОП {op_goal:g}%)' if op_goal is not None else f'Поднять до {r["now_pct"]+step:+.1f}% {lab} → {fmt(new)} ₽'),'up'
        pre='' if r['O']>=r['E']*0.8 else f'факт {r["O"]} против ожидания {r["E"]:.0f} — в пределах шума (порог {thr}); '
        hl='половинный шаг: ' + (f'израсходовано {r["spent"]*100:.0f}% бюджета артикула' if r['spent']>=YEL else f'портфель израсходовал {spent_tot*100:.0f}% лимита') + '; ' if half else ''
        note=pre+hl+f'шаг +{step:.1f} п.п., осталось {rem_steps} шагов до {END.strftime("%d.%m")}'
        if CP and CP.get('now'): note+=f' · CPO {CP["now"]} ₽' + (f' – половинный шаг ({CP["min"]:g}–{CP["max"]:g} ₽)' if cpo_half else f' – в норме (до {CP["min"]:g} ₽)')
        if sc_nopause and ((s.get('pause_until') and s['pause_until']>today_s) or (r['O']<=thr and r['E']>=3)): note+=' · пауза не действует: товар заканчивается, падение заказов здесь не проблема'; s['pause_until']=None
        if r.get('scarce'): note+=f' · дефицит: остаток на {r["scarce"]["days"]:.0f} дн. (порог {r["scarce"]["level"]}) – шаг усилен, цель артикула +{tgt:.0f}%'
        s['strikes']=0
        # ---- локомотивы и акции WB ----
        # Подъём цены главнее участия в акциях (решение владельца 14.09.2026). Если шаг локомотива
        # выводит его из всех идущих акций, ранжирование теряется один раз — значит выходить надо
        # сразу большим шагом: удвоенный обычный, но не меньше 2,5 и не больше 4 п.п. (plan.json →
        # loco_exit_mult / loco_exit_min_pp / loco_exit_max_pp; решение владельца 14.09.2026), а не на 10–15 ₽.
        # Дата выхода из всех акций считается в promos.py по истории цен и показывается на странице.
        lc=LOCO.get(k); cp=CAPS.get(k)
        cend=(f'{cp["end"][8:10]}.{cp["end"][5:7]}' if cp and cp.get('end') else '—') if cp else None
        crosses=bool(cp) and new>cp['cap']*1.003 and r['p_now']<=cp['cap']*1.003   # шаг выводит из всех идущих акций
        if lc and crosses and k in LOCO_KEEP:
            act,kind='Держать — локомотив на границе акций','hold'
            note=(f'локомотив ({lc["share"]}% рекламы): по plan.json (loco_keep_promo) держим в акции «{cp["promo"]}» '
                  f'(проходная {fmt(cp["cap"])} ₽, до {cend}); подъём на {fmt(new-r["p_now"])} ₽ вывел бы из всех акций')
        elif lc and crosses:
            step0=step
            exit_pp=min(max(step0*EXIT_MULT,EXIT_MIN),EXIT_MAX)
            if half: exit_pp=exit_pp/2
            step=min(max(exit_pp,step0),max(gap,0.0))
            new=round(r['anchor']*(1+(r['now_pct']+step)/100))
            act=(f'Поднять до {fmt(new)} ₽ (цель ОП {op_goal:g}%) · выход из акций' if op_goal is not None else f'Поднять до {r["now_pct"]+step:+.1f}% {lab} → {fmt(new)} ₽ · выход из акций')
            note=(f'локомотив ({lc["share"]}% расхода на рекламу за 14 дн.): шаг выводит из всех идущих акций '
                  f'(проходная «{cp["promo"]}» {fmt(cp["cap"])} ₽, до {cend}) — ранжирование потеряем один раз, поэтому шаг увеличен '
                  f'до +{step:.1f} п.п. вместо +{step0:.1f} (удвоенный, в пределах {EXIT_MIN:g}–{EXIT_MAX:g} п.п.)' + (' (половинный из-за зоны бюджета)' if half else '') + f'; {note}')
            r['promo_exit']=True
        elif crosses:
            note+=f' · шаг выводит из акции «{cp["promo"]}» (проходная {fmt(cp["cap"])} ₽, до {cend})'
            r['promo_exit']=True
        elif lc and cp:
            if r['p_now']>cp['cap']*1.003: note+=f' · локомотив ({lc["share"]}% рекламы): уже вне всех идущих акций (проходная «{cp["promo"]}» {fmt(cp["cap"])} ₽)'
            else: note+=f' · локомотив ({lc["share"]}% рекламы): остаётся в акции «{cp["promo"]}» до {cend}, запас до проходной {fmt(cp["cap"]-new)} ₽'
        elif lc:
            note+=f' · локомотив ({lc["share"]}% расхода на рекламу): проходная цена акций неизвестна (нет выгрузки из Календаря акций)'
    if over and down_why and kind in ('pause','hold','done','wait') and r['now_pct']>tgt+0.5:
        note=(note or '')+f' · затоварен (остаток на {t_days:.0f} дн.), снижение не предлагаем: {down_why}'
    r['ad_warn']=None
    if TURN_ON and LOCO.get(k) and kind not in ('off','ctrl'):
        _sh=LOCO[k].get('share')
        # только по остатку: низкая или нулевая маржа локомотива – норма, за его счёт продаются другие товары (решение владельца 30.09.2026)
        if t_days is not None and t_days<=float(TURN.get('ad_warn_days',60)): r['ad_warn']=f'остаток на {t_days:.0f} дн. при {_sh}% рекламного бюджета – сократить рекламу или ускорить поставку'
    state['arts'][k]=s
    acts.append({**r,'act':act,'kind':kind,'note':note,'thr':thr,'step':round(step,1),
                 'own':(round(own*100,1) if own is not None else None),'tgt':tgt,'new_price':new})

# ---------- дубли идут за оригиналом (правило владельца 10.09.2026) ----------
# 101-1 и 101-2 — тот же дизайн и тот же размер, что 101, просто отдельные карточки
# в других склейках. Своей лестницы у дубля нет: цена дубля = цена оригинала.
# Проверено на 32 семьях: разрыв цены дубля к оригиналу НЕ влияет на то, сколько
# заказов дубль забирает (корреляция +0.00) — то есть держать дубль дороже незачем,
# а шаги кампании на него тратятся. Оригиналы двигают средний чек, дубли следуют.
if FOLLOW:
    import re as _re2
    byname={a['a']:a for a in acts}
    followed=0
    for a in acts:
        m=_re2.fullmatch(r'(.+)-([12])',str(a['a']))
        if not m: continue
        o=byname.get(m.group(1))
        if not o: continue
        if a['a'] in CTRL: continue
        if a['kind'] in ('skip','off'): continue   # отложен сотрудником / нет на остатках или фото — не трогаем
        if o['a'] in CTRL:
            # оригинал в контрольной группе — это измеритель сезона для всей кампании.
            # Двигать его дубль нельзя: сместим спрос между ними и испортим прибор.
            # Но и растить дубль незачем — держим до конца контроля.
            a['kind'],a['act'],a['new_price']='hold',f'Держать — {o["a"]} в контроле',round(a['p_now'])
            a['note']=(f'дубль контрольного {o["a"]}: пока по нему считается сезонность, цену '
                       f'дубля не двигаем ни вверх, ни вниз — иначе спрос перетечёт и индекс поедет')
            a['step']=0.0; followed+=1; continue
        # цена, к которой тянемся: та, что у оригинала будет после сегодняшнего решения
        tgt_p=o['new_price'] if o['kind'] in ('up','back') else o['p_now']
        if tgt_p is None or not a.get('p_now'): continue
        tgt_p=round(tgt_p)
        gap=(a['p_now']/tgt_p-1)*100 if tgt_p else 0
        if abs(tgt_p-a['p_now'])<=max(2,a['p_now']*0.002):
            a['kind'],a['act'],a['new_price']='wait',f'Держать — цена как у {o["a"]}',round(a['p_now'])
            a['note']=f'дубль {o["a"]}: цена совпадает с оригиналом, отдельная лестница не нужна'
        elif tgt_p>a['p_now']:
            # оригинал догнал и перегнал — подтягиваем дубль вверх, это бесплатно
            a['kind'],a['act'],a['new_price']='follow',f'Ставить как {o["a"]} → {fmt(tgt_p)} ₽',tgt_p
            a['note']=(f'дубль {o["a"]} (тот же дизайн и размер): оригинал ушёл выше на '
                       f'{abs(gap):.1f}% — подтягиваем дубль за ним')
        elif gap>FOLLOW_GAP:
            a['kind'],a['act'],a['new_price']='follow',f'Ставить как {o["a"]} → {fmt(tgt_p)} ₽',tgt_p
            a['note']=(f'дубль {o["a"]} (тот же дизайн и размер): дороже оригинала на {gap:.1f}% '
                       f'при пороге {FOLLOW_GAP:.0f}% — опускаем к оригиналу')
        else:
            a['kind'],a['act'],a['new_price']='wait',f'Ждать {o["a"]}',round(a['p_now'])
            a['note']=(f'дубль {o["a"]}: дороже оригинала на {gap:.1f}%, это меньше порога '
                       f'{FOLLOW_GAP:.0f}% — оригинал догонит по лестнице, опускать цену незачем')
        a['step']=0.0
        followed+=1
else:
    followed=0

# «План заказов» считается своим скриптом и этой лестницы не вызывает.
# Здесь только подмена уже посчитанной рекомендации у артикула, где план включён.
try:
    _plan=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'op','order_plan.json'),encoding='utf-8'))
except Exception:
    _plan={}
_plan_on={str(a):v for a,v in (_plan.get('articles') or {}).items() if isinstance(v,dict) and v.get('on') and v.get('per_day') and not v.get('test')}
_plan_live=_plan.get('live') or {}
try:
  if _plan_on:
    for a in acts:
        spec=_plan_on.get(str(a['a']))
        if not spec: continue
        info=_plan_live.get(str(a['a'])) or {}
        price=info.get('price'); cur=a.get('p_now')
        floor=0
        try:
            floor=int(round(float(spec.get('min_price') or 0)))
        except (TypeError, ValueError):
            floor=0
        if price and floor and price<floor:
            price=cur if cur and floor>=cur else floor
        a['plan_orders']={'per_day':spec.get('per_day'),'min_price':floor or None,'actual':info.get('actual'),'proj':info.get('proj'),'base':info.get('base'),'at':info.get('at')}
        if price and cur and price!=cur:
            a['kind']='up' if price>cur else 'back'
            a['new_price']=price
            a['act']=(f'Поднять до {fmt(price)} ₽' if price>cur else f'Снизить до {fmt(price)} ₽')
        else:
            a['kind'],a['new_price'],a['act']='hold',cur,'Держать'
        proj=info.get('proj'); plan_n=spec.get('per_day'); fill=info.get('fill')
        move={'down':'опускаем','up':'поднимаем'}.get(info.get('dir'),'оставляем')
        step='' if info.get('dir')=='same' else f" на {info.get('pct') or 0:g}%"
        if floor and info.get('price') and info.get('price')<floor:
            if cur and price==cur:
                move, step='оставляем', ''
            elif price and cur and price<cur:
                move, step='опускаем', f" на {round((cur-price)/cur*100, 2):g}%"
        tail=(f'выполнение около {fill:g}%' if fill is not None else 'проверка плана ещё не прошла')
        base=info.get('base')
        floor_txt=(f' Ниже {floor:g} ₽ не опускаем.' if floor and (info.get('at_floor') or (info.get('price') and info.get('price')<floor)) else '')
        a['note']=(f'План заказов: {tail}. Цену {move}{step} '
                   +(f'от цены {base:g} ₽, зафиксированной вчера в 23:50. ' if base else 'от цены вчерашнего 23:50. ')
                   +'Правила оборачиваемости не действуют.'+floor_txt)
        a['step']=0.0
except Exception:
    pass

# ---------- общий ценник у склеек — ОТКЛЮЧЕНО 08.09.2026 ----------
# Правило исходило из того, что 108 / 108-1 / 108-2 — одна карточка WB с общим ценником.
# Проверка по content-api (/content/v2/get/cards/list, поле imtID) это опровергла:
#   • все 33 группы «по префиксу» лежат в РАЗНЫХ карточках — 102, 102-1 и 102-2 это три
#     разные карточки, суффикс -1/-2 обозначает размер, а не вариант внутри склейки;
#   • настоящая склейка объединяет разные артикулы одного дизайна: в карточке 469257976
#     18 артикулов (102-1, 101-1, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112 и др.);
#   • ограничения «одна цена на карточку» не существует: в той же карточке стоят
#     18 разных цен от 2 542 до 3 888 ₽, WB это допускает.
# Правило связывало цены несвязанных артикулов и навязывало решение по самому осторожному:
# на прогоне 08.09 оно затормозило 10 артикулов (11,5 зак/день), которые должны были расти.
# Настоящая группировка по карточкам сохранена в data/imt.json — для анализа карточки
# целиком, но не для цен. Чтобы вернуть правило, поставьте "merge_glue": true в plan.json.
MERGE=bool(META.get('merge_glue', False))
import re as _re
grp=defaultdict(list)
if MERGE:
    for r in acts:
        m=_re.match(r'^(.+)-\d+$',r['a'])
        if m and r.get('p_fact'): grp[(m.group(1),r['p_fact'])].append(r)
PRIO=['back','pause','deep','ctrl','off','hold','done','wait','up']
merged=0
for (pref,pf),members in grp.items():
    if len(members)<2: continue
    kind=min((m['kind'] for m in members),key=lambda k:PRIO.index(k) if k in PRIO else 99)
    lead=min((m for m in members if m['kind']==kind),key=lambda m:m['new_price'])
    names=', '.join(sorted(m['a'] for m in members))
    lact,lnew,lstep,lnote=lead['act'],lead['new_price'],lead['step'],lead['note']
    for m in members:
        m['kind'],m['act'],m['new_price'],m['step']=kind,lact,lnew,lstep
        m['note']=f'общий ценник со склейкой ({names}) — решение по самому осторожному: {lnote}'
    merged+=1

# ---------- выравнивание разошедшихся склеек — ОТКЛЮЧЕНО 08.09.2026 ----------
# Отменено по той же причине, что и сведение выше: 108-1 и 108-2 — не один товар с двумя
# ценами, а разные карточки. Подтягивать их друг к другу незачем. Включается тем же
# флагом "merge_glue": true в plan.json.
ALIGN=[]
if MERGE and price_now:
    known={str(r['a']) for r in rows}
    gp=defaultdict(dict)
    for k,v in price_now.items():
        m=_re.match(r'^(.+)-\d+$',str(k))
        if m: gp[m.group(1)][str(k)]=v
    for pref,mem in sorted(gp.items()):
        if len(mem)<2: continue
        if not any(a in known for a in mem): continue          # не наша категория
        if pref in CTRL or any(a in CTRL for a in mem): continue  # контроль заморожен до конца
        if pref in EXCL or any(a in EXCL for a in mem): continue
        top=max(mem.values())
        for a,v in sorted(mem.items()):
            if v<top*0.998:
                ALIGN.append({'a':a,'pref':pref,'from':round(v),'to':round(top),
                              'ffull':full(v,a),'tfull':full(top,a),
                              'gap':round((top/v-1)*100,1),
                              'lead':sorted(x for x,y in mem.items() if y==top)[0]})

order={'back':0,'pause':1,'up':2,'wait':3,'hold':4,'deep':5,'done':6,'ctrl':7,'off':8}
acts.sort(key=lambda r:(order.get(r['kind'],9),-r['base']))
TSUM=None
if TURN_ON:
    _or=[r for r in acts if not re.search(r'-\d+$',str(r['a']))]; _tot=sum(r['q7'] for r in _or) or 1
    TSUM=[{'max_days':z.get('max_days'),'tgt':z['tgt'],'n':sum(1 for r in _or if r.get('turn') and r['turn']['zone']==z.get('max_days')),
           'share':round(sum(r['q7'] for r in _or if r.get('turn') and r['turn']['zone']==z.get('max_days'))/_tot*100)} for z in TZONES]
state['meta'].update({'last_run':today_s,'flow_abs':round(flow_abs,4),'flow_price':round(flow_price,4),'season':round(season,4)})
json.dump(state,open(A.state,'w',encoding='utf-8'),ensure_ascii=False,indent=1)

# ---------- монитор рынка ----------
# Частотность поисковых запросов WB (спрос) и показы карточек кабинета (наша видимость).
# Это НЕ входит в расчёт: контрольная группа остаётся регулятором бюджета. Монитор нужен, чтобы
# отделять падение рынка от падения нашей видимости и от влияния цены — три разные причины.
MARKET=None
if A.market and os.path.exists(A.market):
    try:
        mk=json.load(open(A.market,encoding='utf-8'))
        md={d:i for i,d in enumerate(mk['days'])}
        pick=lambda key:[ (mk[key][md[d]] if d in md else None) for d in days[-21:] ]
        MARKET={'core':pick('core'),'open':pick('open'),'pos':pick('pos_avg'),'vis':pick('vis'),
                'queries':mk.get('core_queries',[]),'weekly':mk.get('weekly_index')}
    except Exception as e:
        print(f'[warn] рынок не прочитан: {e}',file=sys.stderr)

# ---------- средний чек заказа: текущая половина месяца против предыдущей ----------
# Периоды — половины месяца: 1–15 и 16–конец. Текущий считается по завершённые дни
# (сегодняшний день WB отдаёт частично), предыдущий — целиком. Дополнительно
# считается равный по длине отрезок предыдущего периода: внутри периода цены растут
# по лестнице, поэтому неполный текущий период честнее сравнивать с таким же куском.
def _half(d):
    """Половина месяца, в которую попадает дата: (начало, конец)."""
    import calendar
    if d.day<=15: return d.replace(day=1), d.replace(day=15)
    return d.replace(day=16), d.replace(day=calendar.monthrange(d.year,d.month)[1])
def _prev_half(start):
    d=start-datetime.timedelta(days=1)
    return _half(d)
def _check(d1,d2):
    """Средний чек заказа (цена до СПП) за период: выручка ÷ число заказов."""
    tot=0.0; n=0
    a1,a2=d1.isoformat(),d2.isoformat()
    for r in raw:
        d=r['date'][:10]
        if d<a1 or d>a2 or d in ANOM: continue
        if SUBJ and r.get('subject')!=SUBJ: continue
        if str(r.get('supplierArticle')) in EXCL: continue
        if r.get('isCancel') or not r.get('priceWithDisc'): continue
        tot+=r['priceWithDisc']; n+=1
    return (round(tot/n), n) if n else (None,0)

_cs,_ce=_half(LAST); _ce=min(_ce,LAST)
_ps,_pe=_prev_half(_cs)
_cur,_cn=_check(_cs,_ce)
_prv,_pn=_check(_ps,_pe)
# Равный отрезок отсчитывается НАЗАД от первого дня текущего периода: столько же
# закрытых дней, сколько уже прошло, но вплотную перед ним. Так сравниваются два
# соседних окна одинаковой длины, а не разные по длине половины месяца.
_ndays=(_ce-_cs).days+1
_eq_end=_cs-datetime.timedelta(days=1)
_eq_start=_eq_end-datetime.timedelta(days=_ndays-1)
if _eq_start.isoformat()<days[0]:
    _eq,_en=None,0                      # окно выходит за глубину выгрузки заказов
else:
    _eq,_en=_check(_eq_start,_eq_end)
# Скользящее окно 7 дней: тот же чек, но короткой памятью — реакция на последние шаги
# видна раньше, чем в периоде с начала месяца. Сравнивается с предыдущими 7 днями
# (равный отрезок вплотную перед ним) и с июньским уровнем.
_w1s=LAST-datetime.timedelta(days=6); _w1e=LAST
_w0e=_w1s-datetime.timedelta(days=1); _w0s=_w0e-datetime.timedelta(days=6)
_c7,_n7=_check(_w1s,_w1e)
_p7,_pn7=(_check(_w0s,_w0e) if _w0s.isoformat()>=days[0] else (None,0))
_jun=META.get('june_price')
CHECK7={'cur':_c7,'cur_n':_n7,'cur_from':_w1s.isoformat(),'cur_to':_w1e.isoformat(),
        'prev':_p7,'prev_n':_pn7,'prev_from':_w0s.isoformat(),'prev_to':_w0e.isoformat(),
        'june':_jun,
        'pct':(round((_c7/_p7-1)*100,1) if (_c7 and _p7) else None),
        'pct_jun':(round((_c7/_jun-1)*100,1) if (_c7 and _jun) else None)}

CHECK={'cur':_cur,'cur_n':_cn,'cur_from':_cs.isoformat(),'cur_to':_ce.isoformat(),
       'prev':_prv,'prev_n':_pn,'prev_from':_ps.isoformat(),'prev_to':_pe.isoformat(),
       'eq':_eq,'eq_n':_en,'eq_from':_eq_start.isoformat(),'eq_to':_eq_end.isoformat(),
       'full':(_ce==_half(LAST)[1]),
       'pct':(round((_cur/_prv-1)*100,1) if (_cur and _prv) else None),
       'pct_eq':(round((_cur/_eq-1)*100,1) if (_cur and _eq) else None)}

_heat=None
try:
    _hj=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'op','order_plan.json'),encoding='utf-8'))
    if _hj.get('curves'):
        _heat={'from':_hj.get('curves_from'),'through':_hj.get('curves_through'),'shop':_hj.get('shop_slots') or [],
               'by':{a:{'n':v.get('n'),'days':v.get('days'),'slots':v.get('slots')} for a,v in _hj['curves'].items() if isinstance(v,dict)}}
except Exception:
    _heat=None
W=sum(r['base14'] for r in rows) or 1
lvl=sum((r['p_now']/r['anchor'])*r['base14'] for r in rows)/W-1
D={'today':today_s,'last':LAST.isoformat(),'days':days[-21:],'end':META['end'],
   'level':round(lvl*100,1),'goal':META['goal_pct'],'june_price':META['june_price'],
   'avail':({'date':AVAIL.get('date'),'ff_date':AVAIL.get('ff_date'),'photos_date':AVAIL.get('photos_date'),'ff_min':FF_MIN,'wb_min':WB_MIN,'ff_col':AVAIL.get('ff_col'),'to_wb_col':AVAIL.get('to_wb_col'),
             'off':{a:v[0] for a,v in OFF.items()}} if AVAIL else None),
   'campaign':META.get('campaign'),'op_rule':(OPT if OP_ON else None),'heat':_heat,'store_kpi_eng':STORE,
   'scarce_rule':SCARCE,'turn_rule':(TURN if TURN_ON else None),'turn_sum':TSUM,
   'june_day':JUN_DAY,'flow_now':round(flow_now,1),'flow_abs':round(flow_abs*100,1),
   'flow_price':round(flow_price*100,1),'season':round(season*100,1),'zone':zone,
   'drop_now':round(drop_now*100,1),'spent':round(spent_tot*100),'camp_days':len(camp),
   'yel':round(YEL*100),'red':round(RED*100),'start':META['start'],
   'floor':META['floor_pct'],'rem_steps':rem_steps,'check':CHECK,'check7':CHECK7,'ctrl':sorted(CTRL),'align':ALIGN,'market':MARKET,
   'season_src':season_used,'season_series':SEASON_SERIES,'idx_last':[round(x,2) for x in IDX[-14:]],
   'promos':({'date':PROMOS.get('date'),'ad':{kk:vv for kk,vv in PROMOS.get('ad',{}).items() if kk!='by_art'},'loco':PROMOS.get('loco',{}),
              'loco_rule':PROMOS.get('loco_rule',{}),'promos':[{kk:vv for kk,vv in p.items() if kk!='advantages'} for p in PROMOS.get('promos',[])],
              'active_count':PROMOS.get('active_count',0),'cap':PROMOS.get('cap',{}),'caps_all':PROMOS.get('caps_all',{}),
              'cap_sources':PROMOS.get('cap_sources',{}),'warnings':PROMOS.get('warnings',[]),'status':PROMOS.get('status',{}),'uncovered':PROMOS.get('uncovered',[]),'exit_rule':{'mult':EXIT_MULT,'min':EXIT_MIN,'max':EXIT_MAX},
              'top_ad':sorted(((a,x) for a,x in PROMOS.get('ad',{}).get('by_art',{}).items()),key=lambda kv:-kv[1]['spend'])[:25]} if PROMOS else None),
   'counts':{k:sum(1 for r in acts if r['kind']==k) for k in ('up','pause','back','wait','hold','deep','done','ctrl','follow','skip','off')},
   'store':{'orders':[sum(r['q'][-21:][i] for r in rows) for i in range(21)],
            'price':[round(sum(r['p'][-21:][i]*r['q'][-21:][i] for r in rows)/max(1,sum(r['q'][-21:][i] for r in rows))) if sum(r['q'][-21:][i] for r in rows)>0 else None for i in range(21)]},
   'rows':[{'a':r['a'],'nm':r['nm'],'act':r['act'],'kind':r['kind'],'note':r['note'],'tier':r['tier'],
            'base':round(r['base'],1),'O':r['O'],'E':round(r['E'],1),'thr':r['thr'],'n':r['n'],
            'p':round(r['p_now']),'anchor':r['anchor'],'src':r['src'],'now_pct':round(r['now_pct'],1),
            'tgt':round(r['tgt'],1),'new':r['new_price'],'step':r['step'],'own':r['own'],
            'disc':disc_now.get(str(r['a'])),'pfull':full(r['p_now'],r['a']),'nfull':full(r['new_price'],r['a']),
            'sp':round(r['spent']*100),'cdrop':round(r['cdrop']*100,1),'cbase':round(r['cbase'],2),
            'cov':(round(r['cover']) if r['cover'] is not None else None),'stock':r['stock'],
            'sd':SDC.get(str(r['a']),{}).get('days'),'sd_info':SDC.get(str(r['a'])),
            'ff':ff_of(r['a']),'fbs':FBS.get(r['a']),'photos':PHOTOS.get(r['a']),'off':(OFF[r['a']][0] if r['a'] in OFF else None),
            'scarce':r.get('scarce'),'turn':r.get('turn'),'op_goal':r.get('op_goal'),'cpo':r.get('cpo'),'lowq':r.get('lowq'),'ad_warn':r.get('ad_warn'),'loco':(r['a'] in LOCO),'ad':(PROMOS.get('ad',{}).get('by_art',{}).get(r['a']) if PROMOS else None),
            'exit':bool(r.get('promo_exit')),'promo_in':(PSTAT[r['a']]['in'] if r['a'] in PSTAT else None),
            'promo_since':(PSTAT[r['a']].get('since') if r['a'] in PSTAT else None),'promo_known_from':(PSTAT[r['a']].get('known_from') if r['a'] in PSTAT else None),
            'promo_unknown':(PSTAT[r['a']].get('unknown',[]) if r['a'] in PSTAT else []),
            'cap':(CAPS[r['a']]['cap'] if r['a'] in CAPS else None),'cap_promo':(CAPS[r['a']].get('promo') if r['a'] in CAPS else None),'cap_end':(CAPS[r['a']].get('end') if r['a'] in CAPS else None),
            'c':r['q'][-21:],'pr':[round(x) for x in r['p'][-21:]],
            'inc':[s['d'] for s in r['steps'] if s['up'] and s['d'] in days[-21:]],
            'dec':[s['d'] for s in r['steps'] if not s['up'] and s['d'] in days[-21:]]} for r in acts]}
try:
    _sj=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'season.json'),encoding='utf-8'))
    D['coefs']={str(k):float(v) for k,v in (_sj.get('coefs') or {}).items()}
except Exception:
    D['coefs']=D.get('coefs') or {}
open(A.out,'w',encoding='utf-8').write(open(A.tpl,encoding='utf-8').read().replace('__DATA_JSON__',json.dumps(D,ensure_ascii=False)))

c=D['counts']
print(f"{today_s} | уровень цен к июню {D['level']:+.1f}% (цель +{D['goal']:.0f}%), осталось {rem_steps} шагов")
print(f"цена сейчас: {price_src}; расходилась с ценой последнего заказа у {stale} артикулов; "
      + (f"склеек с общим ценником сведено {merged}" if MERGE else "сведение склеек выключено — каждый артикул идёт своим темпом"))
print(f"поток: {D['flow_now']:.0f} зак/день ({D['flow_abs']:+.1f}% к июню — сезон). К базе кампании: {drop_now*100:+.1f}%, израсходовано {spent_tot*100:.0f}% лимита −{FLOOR*100:.0f}% (зона {zone}), дней кампании {len(camp)}")
print(f"поднять {c['up']}, пауза {c['pause']}, откат {c['back']}, ждать {c['wait']}, держать {c['hold']+c['done']}, затоварены {c['deep']}, контроль {c['ctrl']}")
if ALIGN:
    print(f"выровнять склейки: {len(ALIGN)} артикулов")
    for g in ALIGN:
        ff=f", до скидки {g['ffull']} → {g['tfull']} ₽" if g['ffull'] and g['tfull'] else ''
        print(f"  {g['a']:>8} {g['from']} → {g['to']} ₽ (+{g['gap']}%, до цены {g['lead']}{ff})")
for r in acts[:10]:
    if r['kind'] in ('up','pause','back','hold'): print(f"  {r['a']:>6} {r['act']}  [{r['note']}]")
