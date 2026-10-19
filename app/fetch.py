"""Pulls raw data from Podhome and OP3 for one show. Returns (source, kind, ok, payload)."""
import base64, datetime as dt, logging

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


async def get(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()


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
