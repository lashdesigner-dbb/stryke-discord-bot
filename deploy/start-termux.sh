#!/data/data/com.termux/files/usr/bin/bash
set -u

BOT_DIR="${STRYKE_BOT_DIR:-$HOME/stryke-bot}"
LOG_DIR="$BOT_DIR/run-logs"
mkdir -p "$LOG_DIR"
cd "$BOT_DIR"

# Keep Android from suspending Termux while the bot is connected.
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock >/dev/null 2>&1 || true
fi

while true; do
  printf '[%s] Iniciando STRYKE.BOT\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_DIR/bot.log"
  python -u main.py >> "$LOG_DIR/bot.log" 2>&1
  status=$?
  printf '[%s] Bot encerrou (código %s); nova tentativa em 10s.\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$status" >> "$LOG_DIR/bot.log"
  sleep 10
done
