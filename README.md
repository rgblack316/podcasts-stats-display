# podcasts-stats-display

A simple display dashboard to show stats from any number of podcasts that update on a fixed interval.

Polls OP3 every 6 hours and shows each podcast in rotation on a kiosk screen.

**Server (Ubuntu Server VM)**
1. Run `curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/install.sh | bash`.
   It installs git and Docker if needed, clones the repo to `~/podcasts-stats-display`, and creates `data/config.json`.
2. Edit `~/podcasts-stats-display/data/config.json` (OP3 token; each show's name and RSS feed URL). Cover art is read from the feed (`itunes:image`, then `podcast:image`, then `image/url`); add `"image_url"` to a show only to override it, then run the same command again.
3. Open `http://<vm-ip>:8080`. The first fetch runs at startup.
4. To upgrade later, run the same command again (or `./scripts/update.sh`). Your `data/` folder is kept.

**Display device (Ubuntu Desktop):**
`curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/kiosk/setup-kiosk.sh | bash -s -- http://<vm-ip>:8080`, then reboot.

**Checking data:** `http://<vm-ip>:8080/api/raw/<Show Name>` shows the raw API responses;
`curl -X POST http://<vm-ip>:8080/api/refresh` fetches now; `sudo docker compose logs -f` for logs.
How OP3's responses are turned into the page's numbers is in `app/shape.py`.
