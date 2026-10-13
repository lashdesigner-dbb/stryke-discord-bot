#!/data/data/com.termux/files/usr/bin/bash
set -u

BOT_DIR="${STRYKE_BOT_DIR:-$HOME/stryke-bot}"
mkdir -p "$BOT_DIR/run-logs" "$HOME/.termux/boot"
nohup bash "$BOT_DIR/deploy/start-termux.sh" \
  >> "$BOT_DIR/run-logs/boot.log" 2>&1 < /dev/null &
