#!/bin/bash
# Точка входа для systemd-таймера wb-repricer.timer (07:00 МСК) и кнопки «Обновить сейчас»
# в меню отчётов (http://127.0.0.1:8092/). Вся работа — в run.sh.
cd /opt/wb-repricer || exit 1
mkdir -p logs
./run.sh "$@" 2>&1 | tee -a logs/run.log
rc="${PIPESTATUS[0]}"
# кампания «ОП 15%» (op/) – сразу после основной, на её свежих данных; сбой не влияет на код возврата основной
( cd op && ./run.sh "$@" ) 2>&1 | tee -a logs/run-op.log || true
exit "$rc"
