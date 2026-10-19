"""Pulls raw data from Podhome and OP3 for one show. Returns (source, kind, ok, payload)."""
import base64, datetime as dt, logging

from .shape import parse_dt

log = logging.getLogger("stats.fetch")
PODHOME = "https://serve.podhome.fm"
OP3 = "https://op3.dev/api/1"
P30 = {"period": "30d"}
PODHOME_JOBS = {
    "show": ("/api/show", {}),
    "overview": ("/api/analytics", P30),
    "trend": ("/api/analytics", {**P30, "metric": "downloads", "granularity": "days"}),
    "apps": ("/api/analytics", {**P30, "dimension": "apps"}),
    "countries": ("/api/analytics", {**P30, "dimension": "countries"}),
    "episodes": ("/api/analytics", {**P30, "dimension": "episodes"}),
}
MAX_PAGES = 20
PERIODS = ((7, "7d"), (14, "14d"), (30, "30d"), (60, "60d"), (90, "90d"), (180, "180d"), (365, "365d"))


def period_for(age_days):
    """Smallest Podhome period that still covers the episode's first week."""
    return next((p for d, p in PERIODS if d >= age_days + 8), "all")


async def get(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()


async def podhome_latest(client, headers):
    """Newest published episode (trailers and future dates ignored) plus its daily downloads."""
    now = dt.datetime.now(dt.timezone.utc)
    eps = await get(client, PODHOME + "/api/episodes", params={"status": 2}, headers=headers)
    eps = [e for e in eps if isinstance(e, dict) and e.get("episode_id")
           and parse_dt(e.get("publish_date")) and parse_dt(e["publish_date"]) <= now
           and "trailer" not in str(e.get("episode_type")).lower()]
    if not eps:
        return {"episode": None}
    ep = max(eps, key=lambda e: parse_dt(e["publish_date"]))
    params = {"episode_id": ep["episode_id"], "metric": "downloads", "granularity": "days",
              "period": period_for((now - parse_dt(ep["publish_date"])).days)}
    trend = await get(client, PODHOME + "/api/analytics", params=params, headers=headers)
    return {"episode": {k: ep.get(k) for k in ("episode_id", "title", "publish_date")}, "trend": trend}


async def op3_downloads(client, uuid, headers):
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    days, total, token, truncated = {}, 0, None, False
    for _ in range(MAX_PAGES):
        params = {"start": start, "format": "json", "limit": "10000"}
        if token:
            params["continuationToken"] = token
        d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
        rows = d.get("rows", []) if isinstance(d, dict) else d
        for r in rows:
            total += 1
            day = str(r.get("time") or r.get("timestamp") or "")[:10]
            if day:
                days[day] = days.get(day, 0) + 1
        token = d.get("continuationToken") if isinstance(d, dict) else None
        if not token:
            break
    else:
        truncated = True
    return {"total": total, "days": days, "truncated": truncated}


async def fetch_show(client, show, op3_token):
    out, name = [], show["name"]

    async def run(source, kind, fn):
        try:
            out.append((source, kind, 1, await fn()))
        except Exception as e:
            log.warning("%s %s/%s failed: %s", name, source, kind, e)
            out.append((source, kind, 0, str(e)))

    h = {"X-API-KEY": show["podhome_api_key"]}
    for kind, (path, params) in PODHOME_JOBS.items():
        await run("podhome", kind, lambda p=path, q=params: get(client, PODHOME + p, params=q, headers=h))

    await run("podhome", "latest", lambda: podhome_latest(client, h))

    feed = next((p.get("rss_feed_url") for s, k, ok, p in out if k == "show" and ok), None)
    if not op3_token or not feed:
        return out
    oh = {"Authorization": "Bearer " + op3_token}
    key = base64.urlsafe_b64encode(feed.encode()).decode().rstrip("=")
    try:
        info = await get(client, f"{OP3}/shows/{key}", headers=oh)
        out.append(("op3", "show", 1, info))
        uuid = info["showUuid"]
    except Exception as e:
        log.warning("%s op3/show failed: %s", name, e)
        out.append(("op3", "show", 0, str(e)))
        return out
    await run("op3", "apps", lambda: get(client, f"{OP3}/queries/top-apps-for-show", params={"showUuid": uuid}, headers=oh))
    await run("op3", "downloads", lambda: op3_downloads(client, uuid, oh))
    return out
