#!/bin/bash
# Прогон по расписанию (LaunchAgent). Работает из ОСНОВНОГО чекаута: там .env, .venv и журнал.
# Оба турнира, режим submit — но без FORECAST_SUBMIT=1 в .env раннер откажется отправлять (код 3).
set -u
ROOT="/Users/nikitanikita/Desktop/КЛОД/forecast-bot"
cd "$ROOT" || exit 1
echo "=== $(date '+%F %T') старт"
"$ROOT/.venv/bin/python" -m forecast_bot.run --mode submit --tournament both
echo "=== $(date '+%F %T') код $?"
