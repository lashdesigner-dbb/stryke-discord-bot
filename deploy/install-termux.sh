#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

BOT_DIR="${STRYKE_BOT_DIR:-$HOME/stryke-bot}"
cd "$BOT_DIR"

pkg update -y
pkg install -y python termux-tools
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Criei $BOT_DIR/.env; edite-o e preencha o token do Discord antes de iniciar."
fi

mkdir -p "$HOME/.termux/boot" "$BOT_DIR/run-logs"
cp "$BOT_DIR/deploy/boot-termux.sh" "$HOME/.termux/boot/start-stryke-bot"
chmod +x "$HOME/.termux/boot/start-stryke-bot" "$BOT_DIR/deploy/start-termux.sh"

echo "Instalação concluída. Configure .env e execute: bash $BOT_DIR/deploy/start-termux.sh"
