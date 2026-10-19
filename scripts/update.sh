#!/usr/bin/env bash
# Pull the latest code from the repo and rebuild. Your data/ folder is untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
sudo docker compose up -d --build
sudo docker image prune -f >/dev/null
echo "Updated."
