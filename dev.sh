#!/usr/bin/env bash
# Локальный запуск соревновательного ТМА для теста В ТЕЛЕГРАМЕ.
# Поднимает бэкенд (фронт + API на одном порту) и cloudflared-туннель,
# печатает публичный HTTPS-URL, который надо вставить в BotFather.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8099}"
PG_BIN="/opt/homebrew/opt/postgresql@16/bin"
PG_DATA="/opt/homebrew/var/postgresql@16"

# venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

# Postgres (поднимаем, если не запущен). LC_ALL — фикс локали на macOS.
if [ -d "$PG_DATA" ] && ! "$PG_BIN/pg_isready" -q 2>/dev/null; then
  echo "▶︎ Поднимаю PostgreSQL ..."
  LC_ALL="en_US.UTF-8" "$PG_BIN/pg_ctl" -D "$PG_DATA" -l /tmp/pg.log start >/dev/null 2>&1 || true
  sleep 3
fi

echo "▶︎ Запускаю бэкенд на :$PORT ..."
PORT="$PORT" ./.venv/bin/python backend.py &
BACKEND_PID=$!

cleanup() { echo; echo "⏹  Останавливаю..."; kill "$BACKEND_PID" 2>/dev/null || true; kill "$TUNNEL_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

sleep 2
echo "▶︎ Поднимаю туннель cloudflared ..."
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | tee /tmp/cf_tunnel.log &
TUNNEL_PID=$!

echo
echo "════════════════════════════════════════════════════════════"
echo "  ТМА (телеграм): жди ссылку https://xxxx.trycloudflare.com"
echo "    → вставь её в BotFather → Bot Settings → Menu Button."
echo "  АДМИНКА (с ноутбука): http://localhost:$PORT/admin"
echo "  Ctrl+C — остановить всё."
echo "════════════════════════════════════════════════════════════"
wait
