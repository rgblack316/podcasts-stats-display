#!/usr/bin/env bash
# Podcast stats display: installer and updater for Ubuntu Server.
#
#   curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/install.sh | bash
#
# It installs git and Docker if missing, clones (or updates) the repository, and
# builds and starts the app. Safe to re-run: re-running pulls the latest code and
# rebuilds. Your data/ folder (config.json and stats.db) is never touched.
#
# Optional environment variables: INSTALL_DIR (default ~/podcasts-stats-display),
# BRANCH (default main), REPO_URL.
set -euo pipefail

# Path of this file when run from disk; empty when piped from curl. Captured here,
# outside main(), because inside a function a piped script reports BASH_SOURCE as "main".
SCRIPT="${BASH_SOURCE[0]:-}"

main() {
  local REPO_URL="${REPO_URL:-https://github.com/rgblack316/podcasts-stats-display.git}"
  local BRANCH="${BRANCH:-main}"
  local INSTALL_DIR="${INSTALL_DIR:-$HOME/podcasts-stats-display}"
  local SUDO="sudo" APP_DIR
  [ "$(id -u)" -eq 0 ] && SUDO=""
  say() { printf '\n==> %s\n' "$*"; }

  local pkgs=()
  command -v git  >/dev/null || pkgs+=(git)
  command -v curl >/dev/null || pkgs+=(curl ca-certificates)
  if [ "${#pkgs[@]}" -gt 0 ]; then
    say "Installing ${pkgs[*]}"
    $SUDO apt-get update -qq </dev/null
    $SUDO apt-get install -y -qq "${pkgs[@]}" </dev/null
  fi

  if ! command -v docker >/dev/null; then
    say "Installing Docker"
    curl -fsSL https://get.docker.com | $SUDO sh
  fi

  # Use the checkout this script lives in; otherwise clone/update INSTALL_DIR.
  if [ -f "$SCRIPT" ] && [ -f "$(dirname "$SCRIPT")/docker-compose.yml" ]; then
    APP_DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
    say "Updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only </dev/null || { echo "Could not update $APP_DIR (local changes?)."; exit 1; }
  elif [ -d "$INSTALL_DIR/.git" ]; then
    APP_DIR="$INSTALL_DIR"
    say "Updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only </dev/null || { echo "Could not update $APP_DIR (local changes?)."; exit 1; }
  else
    APP_DIR="$INSTALL_DIR"
    say "Cloning into $APP_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR" </dev/null
  fi
  cd "$APP_DIR"

  mkdir -p data
  [ -f data/config.json ] || { cp config.example.json data/config.json; chmod 600 data/config.json; }
  if grep -q podhome_api_key data/config.json; then
    say "Config needs updating"
    echo "Podhome is no longer used. In $APP_DIR/data/config.json, replace each show's"
    echo '"podhome_api_key" with "feed_url": "<the show RSS feed URL>" (optional: "image_url").'
    echo "  nano $APP_DIR/data/config.json"
    echo "Then run the same install command again. (The running app is unchanged until then.)"
    exit 0
  fi
  if grep -q PASTE_ data/config.json; then
    say "One more step"
    echo "Edit $APP_DIR/data/config.json: set your OP3 token and each show's name and RSS feed URL."
    echo "  nano $APP_DIR/data/config.json"
    echo "Then run the same install command again."
    exit 0
  fi

  say "Building and starting"
  $SUDO docker compose up -d --build </dev/null
  $SUDO docker image prune -f >/dev/null </dev/null
  say "Running. Dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8080"
}

main "$@"
