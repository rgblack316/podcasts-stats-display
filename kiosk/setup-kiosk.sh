#!/usr/bin/env bash
# Run on the Ubuntu Desktop display device:
#   curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/kiosk/setup-kiosk.sh | bash -s -- http://<vm-ip>:8080
set -euo pipefail

main() {
  local URL="${1:?Usage: setup-kiosk.sh http://<vm-ip>:8080}"
  sudo snap install chromium </dev/null
  mkdir -p ~/.local/bin ~/.config/autostart

  cat > ~/.local/bin/stats-kiosk.sh <<LAUNCH
#!/usr/bin/env bash
until curl -fs -o /dev/null "$URL/api/dashboard"; do sleep 10; done
exec /snap/bin/chromium --kiosk --noerrdialogs --disable-infobars --no-first-run \\
  --disable-session-crashed-bubble --password-store=basic "$URL"
LAUNCH
  chmod +x ~/.local/bin/stats-kiosk.sh

  cat > ~/.config/autostart/stats-kiosk.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Podcast stats kiosk
Exec=$HOME/.local/bin/stats-kiosk.sh
DESK

  gsettings set org.gnome.desktop.session idle-delay 0
  gsettings set org.gnome.desktop.screensaver lock-enabled false
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'

  if ! grep -q '^AutomaticLoginEnable' /etc/gdm3/custom.conf; then
    sudo sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$USER" /etc/gdm3/custom.conf
  fi
  echo "Done. Reboot to start the kiosk."
}

main "$@"
