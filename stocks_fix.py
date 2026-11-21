"""Остатки WB для движка цен: только склад «Склад WB РФ» (правило остатка). Пишет stocks_live.json."""
import json, urllib.request, urllib.error, time, datetime, os, sys, shutil
D='/opt/wb-repricer'; os.environ['TZ']='Europe/Moscow'; time.tzset()
tok=[l.split('=',1)[1].strip() for l in open(D+'/.env') if l.startswith('WB_TOKEN=')][0]; H={'Authorization':tok}
def J(url,data=None,tries=5):
    for i in range(tries):
        try:
            req=urllib.request.Request(url,headers=H,data=data,method='POST' if data is not None else 'GET'); return json.load(urllib.request.urlopen(req,timeout=120))
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(30); continue
            raise
        except Exception as e: print('  retry',i,type(e).__name__,file=sys.stderr); time.sleep(10)
    raise SystemExit('API не ответил')
tid=J('https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains?groupByNm=true&groupBySa=true',b'')['data']['taskId'] if False else J('https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains?groupByNm=true&groupBySa=true')['data']['taskId']
for i in range(30):
    time.sleep(5)
    if J(f'https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains/tasks/{tid}/status')['data']['status']=='done': break
rows=J(f'https://seller-analytics-api.wildberries.ru/api/v1/warehouse_remains/tasks/{tid}/download')
old=json.load(open(D+'/stocks_live.json',encoding='utf-8'))
new={}; tot={}
for r in rows:
    a=str(r['vendorCode']); ws={w['warehouseName']:w['quantity'] for w in r.get('warehouses',[])}
    new[a]=new.get(a,0)+int(ws.get('Склад WB РФ',0) or 0); tot[a]=tot.get(a,0)+int(ws.get('Всего находится на складах',0) or 0)
shutil.copy(D+'/stocks_live.json', D+'/backup/stocks_live.2026-10-15-total.json')
json.dump({'_date':datetime.date.today().isoformat(),**new},open(D+'/stocks_live.json','w',encoding='utf-8'),ensure_ascii=False)
ch=[(a,old.get(a),tot.get(a),new[a]) for a in sorted(new, key=lambda x:-(tot.get(x,0)-new[x])) if old.get(a)!=new[a]]
print('артикулов:',len(new),'| изменилось:',len(ch)); print('арт: было (всего на складах) → стало (Склад WB РФ):'); print(', '.join(f'{a}: {o}→{n}' for a,o,t,n in ch[:60]))
print('524:', old.get('524'), '→', new.get('524'), '| 308:', old.get('308'), '→', new.get('308'))
