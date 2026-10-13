#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/stryke-bot"
DATA_DIR="/var/lib/stryke-bot"
ENV_FILE="/etc/stryke-bot.env"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Execute este script com sudo: sudo bash deploy/install-gcp.sh" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/main.py" || ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "Coloque o pacote do bot em $APP_DIR antes de executar este script." >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Crie $ENV_FILE com as variáveis do bot antes de executar este script." >&2
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv
id stryke >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin stryke
install -d -o stryke -g stryke -m 0750 "$APP_DIR" "$DATA_DIR"
chown -R stryke:stryke "$APP_DIR"
chmod 600 "$ENV_FILE"
for state_file in bot_state.json meta_state.json; do
  source_file="$APP_DIR/bootstrap-state/$state_file"
  if [[ -f "$source_file" && ! -e "$DATA_DIR/$state_file" ]]; then
    install -o stryke -g stryke -m 0600 "$source_file" "$DATA_DIR/$state_file"
  fi
done
runuser -u stryke -- python3 -m venv "$APP_DIR/.venv"
runuser -u stryke -- "$APP_DIR/.venv/bin/pip" install --no-cache-dir -r "$APP_DIR/requirements.txt"
install -m 0644 "$APP_DIR/deploy/stryke-bot.service" /etc/systemd/system/stryke-bot.service
systemctl daemon-reload
systemctl enable --now stryke-bot
systemctl --no-pager --full status stryke-bot
