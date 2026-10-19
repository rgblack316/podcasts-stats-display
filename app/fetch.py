"""Pulls data from OP3 for one show. Returns a list of (source, kind, ok, payload)."""
import base64, datetime as dt, logging
import xml.etree.ElementTree as ET

log = logging.getLogger("stats.fetch")
OP3 = "https://op3.dev/api/1"
MAX_PAGES = 20
MAX_FEED_BYTES = 20_000_000
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
UA = {"User-Agent": "podcasts-stats-display/1.0"}
PAGE_SIZE = 10000


async def get(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()


def feed_image(xml_bytes):
    """Channel cover art URL from an RSS feed: <itunes:image href>, else <image><url>. None if absent."""
    channel = ET.fromstring(xml_bytes).find("channel")
    if channel is None:
        return None
    candidates = [(e.get("href") or "").strip() for e in channel.findall(ITUNES + "image")]
    candidates += [(e.text or "").strip() for e in channel.findall("image/url")]
    return next((u for u in candidates if u.lower().startswith(("http://", "https://"))), None)


async def feed_info(client, feed_url):
    r = await client.get(feed_url, headers=UA, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_FEED_BYTES:
        raise ValueError("feed is too large")
    return {"image": feed_image(r.content)}


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

    async def run(source, kind, fn):
        try:
            out.append((source, kind, 1, await fn()))
        except Exception as e:
            log.warning("%s %s/%s failed: %s", show["name"], source, kind, e)
            out.append((source, kind, 0, str(e)))

    if not show.get("feed_url"):
        return [("op3", "show", 0, "feed_url is missing in config.json")]
    await run("feed", "info", lambda: feed_info(client, show["feed_url"]))
    if not op3_token:
        return out + [("op3", "show", 0, "op3_token is missing in config.json")]

    h = {"Authorization": "Bearer " + op3_token}
    key = base64.urlsafe_b64encode(show["feed_url"].encode()).decode().rstrip("=")
    try:
        info = await get(client, f"{OP3}/shows/{key}", headers=h)
        uuid = info["showUuid"]
    except Exception as e:
        log.warning("%s op3/show failed: %s", show["name"], e)
        return out + [("op3", "show", 0, f"OP3 lookup failed: {e}")]
    out.append(("op3", "show", 1, info))

    await run("op3", "downloads", lambda: op3_downloads(client, uuid, h))
    await run("op3", "episodes", lambda: get(client, f"{OP3}/queries/episode-download-counts", params={"showUuid": uuid}, headers=h))
    await run("op3", "apps", lambda: get(client, f"{OP3}/queries/top-apps-for-show", params={"showUuid": uuid}, headers=h))
    return out
