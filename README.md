# podcasts-stats-display

A simple display dashboard to show stats from any number of podcasts that update on a fixed interval.

Polls Podhome and OP3 every 6 hours and shows three shows in rotation on a kiosk screen.

**Server (Ubuntu Server VM)**
1. `git clone <your-repo-url> && cd podcast-stats`
2. `./scripts/install.sh` (creates `data/config.json`), edit it, run `./scripts/install.sh` again.
3. Open `http://<vm-ip>:8080`. The first fetch runs at startup.
4. Later: `./scripts/update.sh`.

**Display device (Ubuntu Desktop):** `./kiosk/setup-kiosk.sh http://<vm-ip>:8080`, then reboot.

**Checking data:** `http://<vm-ip>:8080/api/raw/<Show Name>` shows the raw API responses;
`curl -X POST http://<vm-ip>:8080/api/refresh` fetches now; `sudo docker compose logs -f` for logs.
Parsing of Podhome's analytics output is in `app/shape.py`.
