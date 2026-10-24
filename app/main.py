import asyncio, json, logging, os, re, secrets, sqlite3, time
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfgmod, fetch, schedule, shape
from .auth import SESSION_SECONDS, Auth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("stats")
DATA = Path(os.environ.get("DATA_DIR", "/data"))
STATIC = Path(__file__).parent / "static"
ART = DATA / "art"
COOKIE = "psd_session"
MAX_UPLOAD = 5_000_000
RETRY_FAILED = 1800   # seconds before a show whose last poll failed is tried again
RETRY_BACKFILL = 600  # seconds before an unfinished lifetime count carries on
SHOW_GAP = 120        # minimum seconds between polling one show and the next, so OP3 isn't hit in a burst
last_poll = 0.0       # when the last show poll finished

_lock = asyncio.Lock()
WAKE = asyncio.Event()  # set to make the scheduler look at the config again
FORCE = set()           # show ids to poll right away
attempted = {}          # show id -> time of the last poll attempt

store = cfgmod.ConfigStore(DATA / "config.json")
auth = Auth(DATA)


def db():
    c = sqlite3.connect(DATA / "stats.db")
    c.execute("CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, show TEXT, source TEXT,"
              " kind TEXT, at INTEGER, ok INTEGER, payload TEXT)")
    return c


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


def last_error(c, show):
    """Most recent failure message for a show, if its newest poll of that item failed."""
    rows = c.execute("SELECT source, kind, ok, payload FROM snapshots WHERE show=? AND id IN"
                     " (SELECT MAX(id) FROM snapshots WHERE show=? GROUP BY source, kind)", (show, show)).fetchall()
    for a, k, ok, p in rows:
        if not ok:
            return f"{a}/{k}: {json.loads(p)}"
    return None


# ---- polling ---------------------------------------------------------------------------------
async def refresh(ids=None):
    """Fetch the given shows (all active ones when ids is None) once. Returns how many requests succeeded."""
    async with _lock:
        cfg, _ = store.get()
        good = 0
        async with httpx.AsyncClient(timeout=60) as client:
            for s in cfg["shows"]:
                if not s["valid"] or (ids is not None and s["id"] not in ids):
                    continue
                FORCE.discard(s["id"])
                attempted[s["id"]] = time.time()
                c = db()
                try:
                    prev = latest(c, s["id"], "op3", "lifetime")
                finally:
                    c.close()
                res = await fetch.fetch_show(client, s, cfg["op3_token"], prev)
                now = int(time.time())
                c = db()
                try:
                    c.executemany("INSERT INTO snapshots(show,source,kind,at,ok,payload) VALUES(?,?,?,?,?,?)",
                                  [(s["id"], a, k, now, ok, json.dumps(p)) for a, k, ok, p in res])
                    c.execute("DELETE FROM snapshots WHERE at < ?", (now - 90 * 86400,))
                    c.commit()
                finally:
                    c.close()
                good += sum(ok for _, _, ok, _ in res)
        log.info("refresh done, %d requests ok", good)
        return good


def due_in(c, s, cfg, now):
    """Seconds until show `s` should be polled again (<= 0 means now)."""
    if s["id"] in FORCE:
        return 0
    every = (s["interval_hours"] or cfg["interval_hours"]) * 3600
    last_ok = c.execute("SELECT MAX(at) FROM snapshots WHERE show=? AND ok=1 AND kind='lifetime'",
                        (s["id"],)).fetchone()[0] or 0
    last_try = attempted.get(s["id"], 0)
    # Timed on the "lifetime" snapshot, which only exists in the current data format, so data left
    # by an older version triggers an immediate refresh.
    due = last_ok + every
    sch = schedule.slots(s, every / 3600, ZoneInfo(cfg["timezone"]), now)
    if sch:  # a start time or fixed times: due at the latest slot that hasn't been polled yet
        prev, nxt = sch
        due = prev if max(last_ok, last_try) < prev else nxt
    if last_try > last_ok:  # the last attempt did not complete: try again sooner
        due = min(due, last_try + RETRY_FAILED)
    life = latest(c, s["id"], "op3", "lifetime")
    if life and life.get("complete") is False:
        due = min(due, last_try + RETRY_BACKFILL)
    return due - now


async def loop():
    global last_poll
    while True:
        try:
            cfg, _ = store.get()
            now, soonest, due = time.time(), 300, []
            c = db()
            try:
                for s in cfg["shows"]:
                    if s["valid"] and cfg["token_ok"]:
                        left = due_in(c, s, cfg, now)
                        if left <= 0:
                            due.append(s["id"])
                        else:
                            soonest = min(soonest, left)
            finally:
                c.close()
            if due:
                # One show at a time, spaced out. Each show then keeps its own slot in the cycle
                # instead of all of them hitting OP3 together.
                wait = last_poll + SHOW_GAP - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                    continue
                first = min(due, key=lambda i: attempted.get(i, 0))
                await refresh({first})
                last_poll = time.time()
                continue
            WAKE.clear()
            try:
                await asyncio.wait_for(WAKE.wait(), timeout=max(soonest, 5))
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("refresh loop error")
            await asyncio.sleep(60)


def wake(ids=()):
    FORCE.update(ids)
    WAKE.set()


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    ART.mkdir(parents=True, exist_ok=True)
    purge_podhome()
    auth.ensure_setup_code()
    _, probs = store.get()
    cfgmod.log_problems(probs)
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


# ---- public: dashboard ------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
    cfg, probs = store.get()
    c, shows, newest = db(), [], 0
    try:
        for s in cfg["shows"]:
            if not s["valid"]:
                continue
            g = lambda kind: latest(c, s["id"], "op3", kind)
            info, dls = g("show") or {}, g("downloads")
            problem = None
            if not info:
                r = c.execute("SELECT payload FROM snapshots WHERE show=? AND source='op3' AND kind='show'"
                              " AND ok=0 ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
                problem = json.loads(r[0]) if r else None
                if not problem and not cfg["token_ok"]:
                    problem = "op3_token is missing or still a placeholder (see Settings)"
            at = c.execute("SELECT MAX(at) FROM snapshots WHERE show=? AND ok=1", (s["id"],)).fetchone()[0] or 0
            newest = max(newest, at)
            shows.append({"name": s["name"], "title": s["name"],
                          "image": s["image_url"] or (latest(c, s["id"], "feed", "info") or {}).get("image"),
                          "problem": problem, "updated": at,
                          "interval_hours": s["interval_hours"] or cfg["interval_hours"],
                          "downloads": dls and dls["total"], "listeners": dls and dls["listeners"],
                          "lifetime": shape.lifetime(g("lifetime"), dls),
                          "truncated": bool(dls and dls["truncated"]),
                          "trend": shape.trend(dls) if dls else [],
                          "apps": shape.ranked(g("apps")), "countries": shape.countries(dls),
                          "episodes": shape.top_episodes(g("episodes")),
                          "latest": shape.latest_episode(g("episodes"))})
    finally:
        c.close()
    return {"shows": shows, "updated": newest, "interval_hours": cfg["interval_hours"],
            "slide_seconds": cfg["slide_seconds"], "reload_minutes": cfg["reload_minutes"],
            "config_problems": probs}


@app.get("/api/raw/{show}")
def raw(show: str):
    """Latest raw payloads for one show (by name): use this to check parsing."""
    cfg, _ = store.get()
    match = next((s["id"] for s in cfg["shows"] if s["name"].casefold() == show.casefold()), show)
    c = db()
    try:
        rows = c.execute("SELECT source, kind, ok, payload FROM snapshots WHERE show=? AND id IN"
                         " (SELECT MAX(id) FROM snapshots WHERE show=? GROUP BY source, kind)", (match, match)).fetchall()
    finally:
        c.close()
    if not rows:
        raise HTTPException(404, "no data for that show name")
    return {f"{a}/{k}": {"ok": bool(ok), "data": json.loads(p)} for a, k, ok, p in rows}


@app.post("/api/refresh")
async def refresh_now():
    wake([s["id"] for s in store.get()[0]["shows"] if s["valid"]])
    return {"started": True}


# ---- uploaded artwork ---------------------------------------------------------------------------
KINDS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MEDIA = {v: k for k, v in KINDS.items()}


def sniff(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return None


@app.get("/art/{name}")
def art(name: str):
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.(png|jpg|gif|webp)", name):
        raise HTTPException(404)
    p = ART / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type=MEDIA[name.rsplit(".", 1)[1]],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600"})


def prune_art(cfg):
    """Delete uploaded images no show uses (leaving fresh ones, which may be waiting for a save)."""
    used = {s["image_url"][5:] for s in cfg["shows"] if s["image_url"].startswith("/art/")}
    for p in ART.glob("*"):
        try:
            if p.name not in used and time.time() - p.stat().st_mtime > 3600:
                p.unlink()
        except OSError:
            pass


# ---- settings: sign-in ------------------------------------------------------------------------
def who(request):
    return request.client.host if request.client else "?"


async def body(request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return data


def start_session(response):
    cookie, csrf = auth.new_session()
    response.set_cookie(COOKIE, cookie, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
    return csrf


def admin(request, mutating=True):
    """Raise unless the request carries a valid session (and, for changes, the matching CSRF token)."""
    csrf = auth.session(request.cookies.get(COOKIE))
    if not csrf:
        raise HTTPException(401, "Sign in first")
    if mutating and not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
        raise HTTPException(403, "Session check failed; reload the page")
    return csrf


@app.get("/settings")
def settings_page():
    return FileResponse(STATIC / "settings.html", headers={"Cache-Control": "no-store"})


@app.get("/api/admin/state")
def admin_state(request: Request, response: Response):
    csrf = auth.session(request.cookies.get(COOKIE))
    return {"configured": auth.configured, "signed_in": bool(csrf), "csrf": csrf or None}


@app.post("/api/admin/setup")
async def admin_setup(request: Request, response: Response):
    d = await body(request)
    ok, msg = auth.setup(who(request), str(d.get("code", "")), str(d.get("password", "")))
    if not ok:
        raise HTTPException(400, msg)
    return {"csrf": start_session(response)}


@app.post("/api/admin/login")
async def admin_login(request: Request, response: Response):
    d = await body(request)
    ok, msg = auth.login(who(request), str(d.get("password", "")))
    if not ok:
        raise HTTPException(429 if msg.startswith("Too many") else 401, msg)
    return {"csrf": start_session(response)}


@app.post("/api/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.post("/api/admin/password")
async def admin_password(request: Request, response: Response):
    admin(request)
    d = await body(request)
    ok, msg = auth.change_password(who(request), str(d.get("old", "")), str(d.get("new", "")))
    if not ok:
        raise HTTPException(400, msg)
    return {"csrf": start_session(response)}


# ---- settings: configuration ------------------------------------------------------------------
@app.get("/api/admin/config")
def admin_config(request: Request):
    admin(request, mutating=False)
    cfg, probs = store.get()
    raw, version = store.raw()
    c = db()
    try:
        shows = []
        for s in cfg["shows"]:
            at = c.execute("SELECT MAX(at) FROM snapshots WHERE show=? AND ok=1", (s["id"],)).fetchone()[0]
            shows.append({"id": s["id"], "name": s["name"], "feed_url": s["feed_url"], "image_url": s["image_url"],
                          "interval_hours": s["interval_hours"], "start_time": s["start_time"], "times": s["times"],
                          "valid": s["valid"], "updated": at,
                          "error": last_error(c, s["id"]),
                          "feed_images": (latest(c, s["id"], "feed", "info") or {}).get("images") or []})
    finally:
        c.close()
    return {"version": version, "problems": probs,
            "settings": {**{k: cfg[k] for k in cfgmod.DEFAULTS}, "timezone": cfg["timezone"]}, "defaults": cfgmod.DEFAULTS, "limits": cfgmod.LIMITS,
            "token_set": cfg["token_ok"], "token_hint": ("…" + cfg["op3_token"][-4:]) if cfg["token_ok"] else "",
            "shows": shows, "file_ok": raw is not None}


@app.put("/api/admin/config")
async def admin_save(request: Request):
    admin(request)
    d = await body(request)
    old_cfg, _ = store.get()
    sub = dict(d.get("config") or {})
    if not sub.get("op3_token"):
        sub["op3_token"] = old_cfg["op3_token"]  # blank means "keep the saved token"
    ok, probs, version = store.save(sub, expect_version=d.get("version"))
    if not ok:
        return Response(json.dumps({"saved": False, "problems": probs, "version": version}),
                        status_code=422, media_type="application/json")
    new_cfg, _ = store.get()
    gone = {s["id"] for s in old_cfg["shows"]} - {s["id"] for s in new_cfg["shows"]}
    changed = {s["id"] for s in new_cfg["shows"] if s["valid"] and any(
        o["id"] == s["id"] and (o["feed_url"], o["image_url"]) != (s["feed_url"], s["image_url"]) for o in old_cfg["shows"])}
    new = {s["id"] for s in new_cfg["shows"] if s["valid"]} - {s["id"] for s in old_cfg["shows"]}
    if gone:
        c = db()
        try:
            c.executemany("DELETE FROM snapshots WHERE show=?", [(i,) for i in gone])
            c.commit()
        finally:
            c.close()
    prune_art(new_cfg)
    wake(changed | new)
    return {"saved": True, "problems": probs, "version": version}


@app.post("/api/admin/test")
async def admin_test(request: Request):
    admin(request)
    d = await body(request)
    ident = str(d.get("feed_url") or "").strip()
    msg = cfgmod.check_ident(ident) if ident else "Enter a feed URL first"
    if msg:
        raise HTTPException(400, "feed_url " + msg if ident else msg)
    token = str(d.get("op3_token") or "").strip() or store.get()[0]["op3_token"]
    async with httpx.AsyncClient(timeout=30) as client:
        return await fetch.test_show(client, ident, token)


@app.post("/api/admin/refresh")
async def admin_refresh(request: Request):
    admin(request)
    d = await body(request)
    cfg, _ = store.get()
    ids = [d["id"]] if d.get("id") else [s["id"] for s in cfg["shows"]]
    wake([i for i in ids if any(s["id"] == i and s["valid"] for s in cfg["shows"])])
    return {"started": True}


@app.post("/api/admin/art")
async def admin_art(request: Request):
    admin(request)
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in KINDS:
        raise HTTPException(400, "Upload a PNG, JPEG, GIF or WebP image")
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD:
        raise HTTPException(413, "Image is larger than 5 MB")
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image is larger than 5 MB")
    kind = sniff(data)
    if not kind:
        raise HTTPException(400, "That file is not a PNG, JPEG, GIF or WebP image")
    ART.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(8)}.{kind}"
    (ART / name).write_bytes(data)
    return {"url": f"/art/{name}"}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
