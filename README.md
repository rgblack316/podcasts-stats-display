# podcasts-stats-display

A simple display dashboard to show stats from any number of podcasts that update on a fixed interval.

Polls OP3 (every 6 hours by default) and shows each podcast in rotation on a kiosk screen.

**Server (Ubuntu Server VM)**
1. Run `curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/install.sh | bash`.
   It installs git and Docker if needed, clones the repo to `~/podcasts-stats-display`, starts the app, and prints the settings address and a one-time **setup code**.
2. Open `http://<vm-ip>:8080/settings`, enter the setup code, and choose a password. Then add your OP3 token and your shows (name + RSS feed URL) there; no JSON editing needed.
3. The dashboard is at `http://<vm-ip>:8080`. New shows are polled as soon as you save.
4. To upgrade later, run the same command again (or `./scripts/update.sh`). Your `data/` folder is kept.

**Settings page** (`/settings`, protected by your password): add, remove and reorder shows; edit display names; choose artwork (automatic from the feed, one of the images found in the feed, an image address, or an uploaded PNG/JPEG/GIF/WebP up to 5 MB); set the OP3 poll interval and schedule (globally and per show), how many seconds each show stays on screen, and how often the page re-reads data; **Test connection** checks a show's feed, your OP3 token and OP3's record of the show before you save; **Refresh data now** polls immediately.
- Forgot the password, or lost the setup code? Delete `data/admin.json` and run `sudo docker compose restart`; a new setup code is printed in `sudo docker compose logs | grep "SETUP CODE"` and kept in `data/setup-code.txt` until used.
- The password is stored only as a salted hash. Sign-in is rate limited (5 wrong tries locks that address for 15 minutes). The app is meant for your home network; don't expose port 8080 to the internet.
- Cover art is read from the feed (`itunes:image`, then `podcast:image`, then `image/url`) unless you pick something else. A podcast:guid in place of a feed URL works for the stats, but cover art then needs a chosen image.

**`data/config.json`** (written by the settings page; you can still edit it by hand):
```json
{
  "interval_hours": 6, "slide_seconds": 60, "reload_minutes": 15, "op3_token": "...",
  "shows": [{"id": "a1b2c3", "name": "My Show", "feed_url": "https://example.com/feed.xml",
             "image_url": "", "interval_hours": 2}]
}
```
`image_url` and a show's own `interval_hours` are optional. `id` is filled in for you; it ties a show to its stored data, so don't change it.

**Config checks.** The file is checked at startup and whenever it changes. Problems never stop the app: shows that are fine keep working, and each problem is printed in the logs, shown on the settings page, and shown as a "Setup needed" slide on the display. It catches: a missing or placeholder `op3_token`; a missing, placeholder or malformed `feed_url` (must be an `http(s)://` address, podcast:guid or OP3 show ID); duplicate show names; a missing or over-long name; numbers that are the wrong type or out of range (`interval_hours` 1-168, `slide_seconds` 5-3600, `reload_minutes` 1-1440); a bad `image_url`; JSON syntax errors (with line and column); a `shows` that isn't a list. The settings page refuses to save a config that has these problems, keeps `config.json.bak` of the previous file, and warns if the file was changed elsewhere in the meantime.

**Display device (Ubuntu Desktop):**
`curl -fsSL https://raw.githubusercontent.com/rgblack316/podcasts-stats-display/main/kiosk/setup-kiosk.sh | bash -s -- http://<vm-ip>:8080`, then reboot.

**Per-show schedule:** each show can use *Automatic* (every N hours, spaced out), *Every N hours starting at a chosen time* (e.g. 00:20, 06:20, 12:20...), or *Fixed times each day* (e.g. `06:00, 18:30`). Times are 24-hour in the **time zone** set under General (default: the server's `TZ`, else UTC). If a slot was missed (app was down), the show is polled once when it comes back. In `config.json`: `"timezone"`, and per show `"start_time"` or `"times"`.

**Rate limits:** shows are polled one at a time, at least 2 minutes apart (so each keeps its own slot in the cycle), requests to OP3 are spaced out, and a `429 slow down` is waited out (honouring `Retry-After`) and retried. After a show's poll fails it is tried again in 30 minutes. So "Refresh data now" with several shows takes a couple of minutes per show.

**Checking data:** `http://<vm-ip>:8080/api/raw/<Show Name>` shows the raw API responses;
`curl -X POST http://<vm-ip>:8080/api/refresh` fetches now; `sudo docker compose logs -f` for logs.
Lifetime downloads: the first poll counts the show's whole history one month at a time and saves a running total; later polls add only the days leaving the 30-day window (a few polls' worth of progress is kept if one fails). Episode lifetime figures are OP3's own `downloadsAll`.

To recount the show lifetime totals from scratch (the saved running total is otherwise trusted), run this on the server:
```bash
sudo docker compose exec -T stats python - <<'EOF'
import sqlite3
c = sqlite3.connect("/data/stats.db")
c.execute("DELETE FROM snapshots WHERE kind='lifetime'")
c.commit()
EOF
sudo docker compose restart
```

How OP3's responses are turned into the page's numbers is in `app/shape.py`.
