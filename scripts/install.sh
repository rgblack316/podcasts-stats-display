#!/usr/bin/env bash
# Run on the Ubuntu Server VM from inside the cloned repo: ./scripts/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
mkdir -p data
if [ ! -f data/config.json ]; then
  cp config.example.json data/config.json
  chmod 600 data/config.json
  echo "Created data/config.json. Edit it with your show names, Podhome keys and OP3 token,"
  echo "then run ./scripts/install.sh again."
  exit 0
fi
sudo docker compose up -d --build
echo "Running. Dashboard: http://$(hostname -I | awk '{print $1}'):8080"
