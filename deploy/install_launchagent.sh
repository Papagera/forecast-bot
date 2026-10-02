#!/bin/bash
# Установка LaunchAgent forecast-bot. Запускать ТОЛЬКО по слову income (ТЗ этапа 1 §5).
#   bash deploy/install_launchagent.sh          # поставить
#   bash deploy/install_launchagent.sh --remove # снять
set -eu
ROOT="/Users/nikitanikita/Desktop/КЛОД/forecast-bot"
LABEL="com.nikita.forecast-bot"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$DST"
  echo "снят: $LABEL"
  exit 0
fi

[ -x "$ROOT/.venv/bin/python" ] || { echo "нет $ROOT/.venv — сначала: python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt && .venv/bin/pip install --no-deps -e vendor/forecasting-tools-0.3.2"; exit 1; }
[ -f "$ROOT/.env" ] || { echo "нет $ROOT/.env"; exit 1; }
mkdir -p "$HOME/.forecast-bot"
cp "$ROOT/deploy/$LABEL.plist" "$DST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "поставлен: $LABEL (каждые 9000 c), лог ~/.forecast-bot/run.log"
