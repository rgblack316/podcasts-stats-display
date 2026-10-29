"""Pulls data from OP3 for one show. Returns a list of (source, kind, ok, payload)."""
import asyncio, base64, datetime as dt, logging, time
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
OP3_EPOCH = dt.date(2021, 1, 1)  # earliest date searched for a show's first download
COUNT_VERSION = OP3_EPOCH.isoformat() + "+ts"  # changes when how the total is counted changes: totals saved with another value are recounted
RECOUNT_DAYS = 30  # the saved lifetime total is counted again from scratch this often, to pick up OP3 corrections


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


OP3_GAP = 0.5      # seconds between requests to OP3
RETRIES_429 = 3    # how often a "429 slow down" is waited out and retried
_last_op3 = 0.0


def retry_delay(r, attempt):
    """Seconds to wait after a 429: OP3's Retry-After if it sent one, else 5, 10, 20, capped at 60."""
    try:
        return min(max(float(r.headers.get("Retry-After")), 1), 60)
    except (AttributeError, TypeError, ValueError):
        return min(5 * 2 ** attempt, 60)


async def get(client, url, **kw):
    """GET json. OP3 requests are spaced out, and a 429 is waited out and retried a few times."""
    global _last_op3
    for attempt in range(RETRIES_429 + 1):
        if url.startswith(OP3):
            wait = _last_op3 + OP3_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _last_op3 = time.monotonic()
        r = await client.get(url, **kw)
        if r.status_code != 429 or attempt == RETRIES_429:
            break
        delay = retry_delay(r, attempt)
        log.warning("OP3 said slow down (429); waiting %ds", delay)
        await asyncio.sleep(delay)
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


def feed_candidates(xml_bytes):
    """{"title", "images"}: the feed's title and every usable cover-art URL, best first (itunes:image,
    then square podcast:image, then <image><url>), without duplicates."""
    channel = ET.fromstring(xml_bytes).find("channel")
    if channel is None:
        return {"title": None, "images": []}
    found = [(e.get("href") or "").strip() for e in channel.findall(ITUNES + "image")]
    found += _podcast_images(channel)
    found += [(e.text or "").strip() for e in channel.findall("image/url")]
    images = []
    for u in found:
        if u.lower().startswith(("http://", "https://")) and u not in images:
            images.append(u)
    title = (channel.findtext("title") or "").strip() or None
    return {"title": title, "images": images}


def feed_image(xml_bytes):
    """Show cover art URL from an RSS feed, or None. A feed missing one namespace still yields art."""
    return next(iter(feed_candidates(xml_bytes)["images"]), None)


async def read_feed(client, feed_url):
    r = await client.get(feed_url, headers=UA, follow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_FEED_BYTES:
        raise ValueError("feed is too large")
    try:
        return feed_candidates(r.content)
    except ET.ParseError:
        raise ValueError("the address loads, but it is not an RSS feed")


async def feed_info(client, feed_url):
    found = await read_feed(client, feed_url)
    return {"image": next(iter(found["images"]), None), "images": found["images"]}


async def test_show(client, ident, op3_token):
    """Checks one show end to end for the settings page: the feed, the OP3 token and OP3's record of
    the show. Each part is {"ok", "message", ...}; nothing is stored."""
    out = {}
    if is_url(ident):
        try:
            found = await read_feed(client, ident)
            n = len(found["images"])
            msg = "Feed loads" + (f' as "{found["title"]}"' if found["title"] else "")
            msg += f"; {n} cover image{'s' if n != 1 else ''} found" if n else ", but no cover art was found in it"
            out["feed"] = {"ok": True, "title": found["title"], "images": found["images"], "message": msg}
        except Exception as e:
            out["feed"] = {"ok": False, "images": [], "message": f"Feed could not be read: {describe(e)}"}
    else:
        out["feed"] = {"ok": None, "images": [], "message": "Not a feed URL, so the feed itself is not checked"}
    if not op3_token:
        out["op3"] = {"ok": False, "message": "No OP3 token is set"}
        return out
    try:
        info = await lookup_show(client, ident, {"Authorization": "Bearer " + op3_token})
        out["op3"] = {"ok": True, "title": info.get("title"), "message": "OP3 accepted the token and found the show"
                      + (f" ({info['title']})" if info.get("title") else "")}
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            msg = "OP3 rejected the token. Check it at https://op3.dev/api/keys"
        elif status == 404:
            msg = "OP3 has no show for this address. Check the feed URL, or the show may not have had a download through OP3 yet"
        else:
            msg = f"OP3 lookup failed: {describe(e)}"
        out["op3"] = {"ok": False, "message": msg}
    return out


async def op3_downloads(client, uuid, headers, since):
    """Downloads from `since` (a date) until now, tallied per day and per country, plus distinct listeners."""
    days, countries, listeners = {}, {}, set()
    total, token, truncated = 0, None, False
    for _ in range(MAX_PAGES):
        params = {"start": ts(since.isoformat()), "format": "json", "limit": str(PAGE_SIZE)}
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


def ts(d):
    """A date as the start of that UTC day, as a full timestamp. Date-only start/end values are not
    used: a timestamp leaves no doubt that `end` is a moment, so the day it names isn't counted twice."""
    return f"{d}T00:00:00Z"


def next_month(d):
    return (d.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


async def count_rows(client, uuid, headers, start, end):
    """Number of downloads in [start, end) (date strings; start inclusive, end exclusive)."""
    count, token = 0, None
    for _ in range(MAX_BACKFILL_PAGES):
        params = {"start": ts(start), "end": ts(end), "format": "json", "limit": str(BACKFILL_PAGE_SIZE)}
        if token:
            params["continuationToken"] = token
        d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
        count += len(d.get("rows", []) if isinstance(d, dict) else d)
        token = d.get("continuationToken") if isinstance(d, dict) else None
        if not token:
            return count
    raise RuntimeError(f"more than {MAX_BACKFILL_PAGES * BACKFILL_PAGE_SIZE} downloads between {start} and {end}")


async def first_download_date(client, uuid, headers, before):
    params = {"start": ts(OP3_EPOCH.isoformat()), "end": ts(before.isoformat()), "format": "json", "limit": "1"}
    d = await get(client, f"{OP3}/downloads/show/{uuid}", params=params, headers=headers)
    rows = d.get("rows", []) if isinstance(d, dict) else d
    return dt.date.fromisoformat(str(rows[0]["time"])[:10]) if rows else None


async def op3_lifetime(client, uuid, headers, prev, cutoff):
    """Running total of every download BEFORE `cutoff`. Counts one month at a time, starting from the
    show's first download, or resuming from `prev` ({"total", "through"}), so a later poll only counts
    the days that have just left the 30-day window. If it fails part-way, the progress is kept
    ("complete": False) and the next poll carries on."""
    now = int(time.time())
    started = now
    fallback = None  # the previous complete total, kept for display while a recount is under way
    try:
        total, through = int(prev["total"]), dt.date.fromisoformat(prev["through"])
        started = int(prev["started"])
        # A total counted from a different starting date, or long ago, is counted again from scratch.
        if prev.get("epoch") != COUNT_VERSION or now - started > RECOUNT_DAYS * 86400:
            fallback = prev.get("fallback") or {"total": total, "through": prev["through"]}
            raise ValueError("recount")
    except (TypeError, KeyError, ValueError):
        total, through, started = 0, None, now
        if fallback is None and isinstance(prev, dict) and prev.get("fallback"):
            fallback = prev["fallback"]
        if fallback is None and isinstance(prev, dict) and prev.get("complete") and "total" in prev and "through" in prev:
            fallback = {"total": prev["total"], "through": prev["through"]}  # saved by an older version
    if through is None:
        first = await first_download_date(client, uuid, headers, cutoff)
        if first is None:
            return {"total": 0, "through": cutoff.isoformat(), "complete": True, "epoch": COUNT_VERSION, "started": started}
        through = first.replace(day=1)
    first_through = through
    try:
        while through < cutoff:
            nxt = min(next_month(through), cutoff)
            total += await count_rows(client, uuid, headers, through.isoformat(), nxt.isoformat())
            through = nxt
    except Exception:
        if through == first_through:
            raise
        log.warning("lifetime backfill stopped at %s with %d counted; will resume", through, total, exc_info=True)
    out = {"total": total, "through": through.isoformat(), "complete": through >= cutoff,
           "epoch": COUNT_VERSION, "started": started}
    if not out["complete"] and (fallback or (prev or {}).get("fallback") and started == int((prev or {}).get("started", 0))):
        out["fallback"] = fallback or prev["fallback"]
    return out


async def fetch_show(client, show, op3_token, prev_lifetime=None):
    out = []

    async def run(source, kind, fn):
        try:
            out.append((source, kind, 1, await fn()))
        except Exception as e:
            log.warning("%s %s/%s failed: %s", show.get("name"), source, kind, describe(e))
            out.append((source, kind, 0, describe(e)))

    ident = (show.get("feed_url") or "").strip()
    if not ident:
        return [("op3", "show", 0, "feed_url is missing")]
    if is_url(ident):  # cover art is read from the feed, which needs its web address
        await run("feed", "info", lambda: feed_info(client, ident))
    if not op3_token:
        return out + [("op3", "show", 0, "op3_token is missing")]

    h = {"Authorization": "Bearer " + op3_token}
    try:
        info = await lookup_show(client, ident, h)
        uuid = info["showUuid"]
    except Exception as e:
        log.warning("%s op3/show failed: %s", show.get("name"), describe(e))
        return out + [("op3", "show", 0, f"OP3 lookup failed: {describe(e)}")]
    out.append(("op3", "show", 1, info))

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).date()
    await run("op3", "downloads", lambda: op3_downloads(client, uuid, h, since))
    if prev_lifetime and prev_lifetime.get("uuid") not in (None, uuid):
        prev_lifetime = None  # the feed now points at a different OP3 show: count again from scratch

    async def lifetime():
        return {**await op3_lifetime(client, uuid, h, prev_lifetime, since), "uuid": uuid}
    await run("op3", "lifetime", lifetime)
    await run("op3", "episodes", lambda: get(client, f"{OP3}/queries/episode-download-counts", params={"showUuid": uuid}, headers=h))
    await run("op3", "apps", lambda: get(client, f"{OP3}/queries/top-apps-for-show", params={"showUuid": uuid}, headers=h))
    return out
