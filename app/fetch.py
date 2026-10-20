"""Pulls data from OP3 for one show. Returns a list of (source, kind, ok, payload)."""
import base64, datetime as dt, logging
import xml.etree.ElementTree as ET

log = logging.getLogger("stats.fetch")
OP3 = "https://op3.dev/api/1"
MAX_PAGES = 20
MAX_FEED_BYTES = 20_000_000
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
PODCAST_NS = ("https://podcastindex.org/namespace/1.0", "http://podcastindex.org/namespace/1.0")
UA = {"User-Agent": "podcasts-stats-display/1.0"}
PAGE_SIZE = 10000
BACKFILL_PAGE_SIZE = 20000   # the API maximum
MAX_BACKFILL_PAGES = 500      # per one-month window
OP3_EPOCH = dt.date(2022, 9, 1)  # OP3 launched in September 2022


def describe(e):
    """Short, readable error text (httpx's own message is long and ends with a docs link)."""
    r = getattr(e, "response", None)
    if r is not None:
        try:
            body = " ".join((r.text or "").split())[:160]
        except Exception:
            body = ""
        return f"HTTP {r.status_code}" + (f": {body}" if body else "")
    return (str(e) or type(e).__name__)[:200]


def is_url(ident):
    return ident.lower().startswith(("http://", "https://"))


def lookup_keys(ident):
    """What goes after /shows/ in the OP3 API: a feed URL as urlsafe base64 (tried without, then
    with, "=" padding, since the docs don't say which), or a podcast:guid / OP3 show ID as is."""
    if not is_url(ident):
        return [ident]
    b64 = base64.urlsafe_b64encode(ident.encode()).decode()
    return [b64.rstrip("="), b64] if b64.endswith("=") else [b64]


async def lookup_show(client, ident, headers):
    keys = lookup_keys(ident)
    for i, key in enumerate(keys):
        try:
            return await get(client, f"{OP3}/shows/{key}", headers=headers)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if i == len(keys) - 1 or status != 400:
                raise


async def get(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.json()


def _is_square(ratio):
    """True when a podcast:image aspect-ratio ("1/1", "1:1", "1") is square; absent means square."""
    if not ratio:
        return True
    try:
        parts = [float(x) for x in ratio.replace(":", "/").replace(" ", "").split("/")]
        return len(parts) in (1, 2) and abs(parts[0] / (parts[1] if len(parts) == 2 else 1) - 1) < 0.01
    except (ValueError, ZeroDivisionError):
        return False


def _podcast_images(channel):
    """href of each channel-level <podcast:image> that is plain square show artwork, in feed order.
    Skips banners, social, canvas, circular-only, publisher logos, posters and video."""
    out = []
    for e in channel:
        if not isinstance(e.tag, str) or not e.tag.startswith("{"):
            continue
        uri, _, local = e.tag[1:].partition("}")
        if local != "image" or uri not in PODCAST_NS:
            continue
        purpose = (e.get("purpose") or "").lower().split()
        mime = (e.get("type") or "").lower()
        if (purpose and "artwork" not in purpose) or not _is_square(e.get("aspect-ratio")) \
                or (mime and not mime.startswith("image/")):
            continue
        out.append((e.get("href") or "").strip())
    return out


def feed_image(xml_bytes):
    """Show cover art URL from an RSS feed, or None. Tries <itunes:image href>, then a square
    <podcast:image>, then <image><url>, so a feed missing one namespace still yields art."""
    channel = ET.fromstring(xml_bytes).find("channel")
    if channel is None:
        return None
    candidates = [(e.get("href") or "").strip() for e in channel.findall(ITUNES + "image")]
    candidates += _podcast_images(channel)
    candidates += [(e.text or "").strip() for e in channel.findall("image/url")]
    return next((u for u in candidates if u.lower().startswith(("http://", "https://"))), None)


async def feed_info(client, feed_url):
    r = await client.get(feed_url, headers=UA, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_FEED_BYTES:
        raise ValueError("feed is too large")
    return {"image": feed_image(r.content)}


async def op3_downloads(client, uuid, headers, since):
    """Downloads from `since` (a date) until now, tallied per day and per country, plus distinct listeners."""
    days, countries, listeners = {}, {}, set()
    total, token, truncated = 0, None, False
    for _ in range(MAX_PAGES):
        params = {"start": since.isoformat(), "format": "json", "limit": str(PAGE_SIZE)}
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
    return {"total": total, "days": days, "countries": countries, "since": since.isoformat(),
            "listeners": len(listeners), "truncated": truncated}


def next_month(d):
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


async def count_rows(client, uuid, headers, start, end):
    """Number of downloads in [start, end) (date strings; start inclusive, end exclusive)."""
    count, token = 0, None
    for _ in range(MAX_BACKFILL_PAGES):
        params = {"start": start, "end": end, "format": "json", "limit": str(BACKFILL_PAGE_SIZE)}
        if token:
            params["continuationToken"] = token
        d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
        count += len(d.get("rows", []) if isinstance(d, dict) else d)
        token = d.get("continuationToken") if isinstance(d, dict) else None
        if not token:
            return count
    raise RuntimeError(f"more than {MAX_BACKFILL_PAGES * BACKFILL_PAGE_SIZE} downloads between {start} and {end}")


async def first_download_date(client, uuid, headers, before):
    params = {"start": OP3_EPOCH.isoformat(), "end": before.isoformat(), "format": "json", "limit": "1"}
    d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
    rows = d.get("rows", []) if isinstance(d, dict) else d
    return dt.date.fromisoformat(str(rows[0]["time"])[:10]) if rows else None


async def op3_lifetime(client, uuid, headers, prev, cutoff):
    """Running total of every download BEFORE `cutoff`. Counts one month at a time, starting from the
    show's first download, or resuming from `prev` ({"total", "through"}), so a later poll only counts
    the days that have just left the 30-day window. If it fails part-way, the progress is kept
    ("complete": False) and the next poll carries on."""
    try:
        total, through = int(prev["total"]), dt.date.fromisoformat(prev["through"])
    except (TypeError, KeyError, ValueError):
        total, through = 0, None
    if through is None:
        first = await first_download_date(client, uuid, headers, cutoff)
        if first is None:
            return {"total": 0, "through": cutoff.isoformat(), "complete": True}
        through = first.replace(day=1)
    started = through
    try:
        while through < cutoff:
            nxt = min(next_month(through), cutoff)
            total += await count_rows(client, uuid, headers, through.isoformat(), nxt.isoformat())
            through = nxt
    except Exception:
        if through == started:
            raise
        log.warning("lifetime backfill stopped at %s with %d counted; will resume", through, total, exc_info=True)
    return {"total": total, "through": through.isoformat(), "complete": through >= cutoff}


async def fetch_show(client, show, op3_token, prev_lifetime=None):
    out = []

    async def run(source, kind, fn):
        try:
            out.append((source, kind, 1, await fn()))
        except Exception as e:
            log.warning("%s %s/%s failed: %s", show["name"], source, kind, describe(e))
            out.append((source, kind, 0, describe(e)))

    ident = (show.get("feed_url") or "").strip()
    if not ident:
        return [("op3", "show", 0, "feed_url is missing in config.json")]
    if is_url(ident):  # cover art is read from the feed, which needs its web address
        await run("feed", "info", lambda: feed_info(client, ident))
    if not op3_token:
        return out + [("op3", "show", 0, "op3_token is missing in config.json")]

    h = {"Authorization": "Bearer " + op3_token}
    try:
        info = await lookup_show(client, ident, h)
        uuid = info["showUuid"]
    except Exception as e:
        log.warning("%s op3/show failed: %s", show["name"], describe(e))
        return out + [("op3", "show", 0, f"OP3 lookup failed: {describe(e)}")]
    out.append(("op3", "show", 1, info))

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).date()
    await run("op3", "downloads", lambda: op3_downloads(client, uuid, h, since))
    await run("op3", "lifetime", lambda: op3_lifetime(client, uuid, h, prev_lifetime, since))
    await run("op3", "episodes", lambda: get(client, f"{OP3}/queries/episode-download-counts", params={"showUuid": uuid}, headers=h))
    await run("op3", "apps", lambda: get(client, f"{OP3}/queries/top-apps-for-show", params={"showUuid": uuid}, headers=h))
    return out
