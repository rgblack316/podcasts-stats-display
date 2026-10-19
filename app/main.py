import asyncio, json, logging, os, sqlite3, time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import fetch, shape

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stats")
DATA = Path(os.environ.get("DATA_DIR", "/data"))
_lock = asyncio.Lock()


def db():
    c = sqlite3.connect(DATA / "stats.db")
    c.execute("CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, show TEXT, source TEXT,"
              " kind TEXT, at INTEGER, ok INTEGER, payload TEXT)")
    return c


def config():
    return json.loads((DATA / "config.json").read_text())


def purge_podhome():
    """The Podhome integration was removed: delete any snapshots it stored."""
    c = db()
    try:
        n = c.execute("DELETE FROM snapshots WHERE source='podhome'").rowcount
        c.commit()
    finally:
        c.close()
    if n:
        log.info("removed %d old Podhome snapshots", n)


def latest(c, show, source, kind):
    r = c.execute("SELECT payload FROM snapshots WHERE show=? AND source=? AND kind=? AND ok=1"
                  " ORDER BY id DESC LIMIT 1", (show, source, kind)).fetchone()
    return json.loads(r[0]) if r else None


async def refresh():
    """Fetch every show once. Returns how many requests succeeded."""
    async with _lock:
        cfg, good = config(), 0
        async with httpx.AsyncClient(timeout=60) as client:
            for s in cfg["shows"]:
                res = await fetch.fetch_show(client, s, cfg.get("op3_token", ""))
                now = int(time.time())
                c = db()
                try:
                    c.executemany("INSERT INTO snapshots(show,source,kind,at,ok,payload) VALUES(?,?,?,?,?,?)",
                                  [(s["name"], a, k, now, ok, json.dumps(p)) for a, k, ok, p in res])
                    c.execute("DELETE FROM snapshots WHERE at < ?", (now - 90 * 86400,))
                    c.commit()
                finally:
                    c.close()
                good += sum(ok for _, _, ok, _ in res)
        log.info("refresh done, %d requests ok", good)
        return good


async def loop():
    while True:
        try:
            every = config().get("interval_hours", 6) * 3600
            c = db()
            # Timed on the "episodes" snapshot, which only exists in the current data format,
            # so older snapshots left by a previous version trigger an immediate refresh.
            last = c.execute("SELECT MAX(at) FROM snapshots WHERE ok=1 AND kind='episodes'").fetchone()[0] or 0
            c.close()
            wait = last + every - time.time()
            if wait <= 0:
                await asyncio.sleep(every if await refresh() else 1800)
                continue
            await asyncio.sleep(max(wait, 60))
        except Exception:
            log.exception("refresh loop error (is data/config.json in place?)")
            await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    purge_podhome()
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/api/dashboard")
def dashboard():
    cfg, c, shows, newest = config(), db(), [], 0
    try:
        for s in cfg["shows"]:
            g = lambda kind: latest(c, s["name"], "op3", kind)
            info, dls = g("show") or {}, g("downloads")
            problem = None
            if not info:
                r = c.execute("SELECT payload FROM snapshots WHERE show=? AND source='op3' AND kind='show'"
                              " AND ok=0 ORDER BY id DESC LIMIT 1", (s["name"],)).fetchone()
                problem = json.loads(r[0]) if r else None
            shows.append({"name": s["name"], "title": info.get("title") or s["name"],
                          "image": s.get("image_url") or None, "problem": problem,
                          "downloads": dls and dls["total"], "listeners": dls and dls["listeners"],
                          "truncated": bool(dls and dls["truncated"]),
                          "trend": shape.trend(dls) if dls else [],
                          "apps": shape.ranked(g("apps")), "countries": shape.countries(dls),
                          "episodes": shape.top_episodes(g("episodes")),
                          "latest": shape.latest_episode(g("episodes"))})
        newest = c.execute("SELECT MAX(at) FROM snapshots WHERE ok=1").fetchone()[0] or 0
    finally:
        c.close()
    return {"shows": shows, "updated": newest, "interval_hours": cfg.get("interval_hours", 6)}


@app.get("/api/raw/{show}")
def raw(show: str):
    """Latest raw payloads for one show: use this to check parsing."""
    c = db()
    try:
        rows = c.execute("SELECT source, kind, ok, payload FROM snapshots WHERE show=? AND id IN"
                         " (SELECT MAX(id) FROM snapshots WHERE show=? GROUP BY source, kind)", (show, show)).fetchall()
    finally:
        c.close()
    if not rows:
        raise HTTPException(404, "no data for that show name")
    return {f"{a}/{k}": {"ok": bool(ok), "data": json.loads(p)} for a, k, ok, p in rows}


@app.post("/api/refresh")
async def refresh_now(bg: BackgroundTasks):
    bg.add_task(refresh)
    return {"started": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
