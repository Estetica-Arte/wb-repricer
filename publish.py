#!/usr/bin/env python3
"""Обёртка страницы в полноценный HTML-документ и публикация.

Шаблон page_template.html собран под артефакт Claude — это фрагмент без
<html>/<head>/<meta charset>. Если положить такой фрагмент на nginx как есть,
он отдаётся без charset и кириллица превращается в кракозябры. Поэтому перед
публикацией фрагмент оборачивается здесь.

Два способа публикации, оба необязательные:
  SITE_DIR   — путь к корню сайта: страница просто копируется файлом
  DEPLOY_URL — приёмник deploy.php: страница уходит HTTPS POST-ом

Запуск:  python3 publish.py page.html [имя_файла_на_сайте]
"""
import os, re, sys, subprocess, pathlib

SRC = sys.argv[1] if len(sys.argv) > 1 else 'page.html'
NAME = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('PAGE_NAME', 'wb-ceny.html')

html = pathlib.Path(SRC).read_text(encoding='utf-8')

if '<!doctype' not in html[:200].lower():
    # <title> переносится в <head>: в теле он валиден не везде и его теряют парсеры-читалки
    title = '<title>Заказы и цены WB</title>'
    m = re.search(r'<title>.*?</title>', html, re.S)
    if m:
        title = m.group(0)
        html = html.replace(title, '', 1)
    html = ('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            + title +
            '</head><body style="margin:0">' + html + '</body></html>')

out = pathlib.Path('page-standalone.html')
out.write_text(html, encoding='utf-8')
print(f'[publish] собран полноценный документ: {out} ({len(html.encode("utf-8"))} байт)')

done = False

site = os.environ.get('SITE_DIR')
if site:
    dst = pathlib.Path(site) / NAME
    tmp = dst.with_suffix(dst.suffix + '.tmp')
    tmp.write_text(html, encoding='utf-8')
    tmp.replace(dst)                      # атомарная подмена: читатель не увидит половину файла
    print(f'[publish] записано в {dst}')
    done = True

url = os.environ.get('DEPLOY_URL')
tok = os.environ.get('DEPLOY_TOKEN')
if url:
    if not tok:
        print('[publish] DEPLOY_URL задан, а DEPLOY_TOKEN нет — POST пропущен', file=sys.stderr)
    else:
        sep = '&' if '?' in url else '?'
        r = subprocess.run(['curl', '-sS', '-X', 'POST', f'{url}{sep}file={NAME}',
                            '-H', f'X-Deploy-Token: {tok}',
                            '--data-binary', f'@{out}', '--max-time', '120'],
                           capture_output=True, text=True)
        print(f'[publish] ответ приёмника: {r.stdout.strip() or r.stderr.strip()}')
        if r.returncode != 0:
            sys.exit(f'[publish] POST не прошёл: {r.stderr.strip()}')
        done = True

if not done:
    print('[publish] ни SITE_DIR, ни DEPLOY_URL не заданы — страница осталась локально')
