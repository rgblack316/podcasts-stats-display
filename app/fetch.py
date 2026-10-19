"""Pulls data from OP3 for one show. Returns a list of (source, kind, ok, payload)."""
import base64, datetime as dt, logging

log = logging.getLogger("stats.fetch")
OP3 = "https://op3.dev/api/1"
MAX_PAGES = 20
PAGE_SIZE = 10000


async def get(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()


async def op3_downloads(client, uuid, headers):
    """Last 30 days of downloads, tallied per day and per country, plus distinct listeners."""
    start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    days, countries, listeners = {}, {}, set()
    total, token, truncated = 0, None, False
    for _ in range(MAX_PAGES):
        params = {"start": start, "format": "json", "limit": str(PAGE_SIZE)}
        if token:
            params["continuationToken"] = token
        d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
        rows = d.get("rows", []) if isinstance(d, dict) else d
        for r in rows:
            total += 1
            day = str(r.get("time") or r.get("timestamp") or "")[:10]
            if day:
                days[day] = days.get(day, 0) + 1
            cc = r.get("countryCode")
            if cc:
                countries[cc] = countries.get(cc, 0) + 1
            if r.get("hashedIpAddress"):
                listeners.add(r["hashedIpAddress"])
        token = d.get("continuationToken") if isinstance(d, dict) else None
        if not token:
            break
    else:
        truncated = True
    return {"total": total, "days": days, "countries": countries,
            "listeners": len(listeners), "truncated": truncated}


async def fetch_show(client, show, op3_token):
    out = []

    async def run(kind, fn):
        try:
            out.append(("op3", kind, 1, await fn()))
        except Exception as e:
            log.warning("%s op3/%s failed: %s", show["name"], kind, e)
            out.append(("op3", kind, 0, str(e)))

    if not op3_token:
        return [("op3", "show", 0, "op3_token is missing in config.json")]
    if not show.get("feed_url"):
        return [("op3", "show", 0, "feed_url is missing in config.json")]

    h = {"Authorization": "Bearer " + op3_token}
    key = base64.urlsafe_b64encode(show["feed_url"].encode()).decode().rstrip("=")
    try:
        info = await get(client, f"{OP3}/shows/{key}", headers=h)
        uuid = info["showUuid"]
    except Exception as e:
        log.warning("%s op3/show failed: %s", show["name"], e)
        return [("op3", "show", 0, f"OP3 lookup failed: {e}")]
    out.append(("op3", "show", 1, info))

    await run("downloads", lambda: op3_downloads(client, uuid, h))
    await run("episodes", lambda: get(client, f"{OP3}/queries/episode-download-counts", params={"showUuid": uuid}, headers=h))
    await run("apps", lambda: get(client, f"{OP3}/queries/top-apps-for-show", params={"showUuid": uuid}, headers=h))
    return out
