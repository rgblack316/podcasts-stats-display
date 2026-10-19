"""Tolerant readers for Podhome's analytics JSON (its response schema is not published).
If the dashboard shows wrong numbers, look at /api/raw/<show> and adjust here."""
import datetime as dt
import re

NUM = (int, float)
VAL = ("downloads", "count", "value", "audience", "listeners", "total")
LAB = ("name", "title", "app", "country", "label", "episode", "date", "day", "period")


def find_list(o):
    if isinstance(o, list) and o and all(isinstance(x, dict) for x in o):
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = find_list(v)
            if r:
                return r
    return []


def _num(v):
    return isinstance(v, NUM) and not isinstance(v, bool)


def pair(row):
    val = next((row[k] for k in VAL if _num(row.get(k))), None)
    if val is None:
        val = next((v for v in row.values() if _num(v)), 0)
    lab = next((row[k] for k in LAB if isinstance(row.get(k), str)), None)
    if lab is None:
        lab = next((v for v in row.values() if isinstance(v, str)), "")
    return {"label": lab, "value": val}


def rows(payload, limit=None):
    out = [pair(r) for r in find_list(payload)]
    return out[:limit] if limit else out


def leaves(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from leaves(v, f"{path}.{k}".lower())
    elif _num(o):
        yield path, o


def totals(overview):
    """(downloads, listeners) from the overview payload, None when not found."""
    flat = list(leaves(overview))
    pick = lambda *words: next((v for p, v in flat if any(w in p for w in words) and "episode" not in p), None)
    return pick("download"), pick("unique", "listener", "audience")


DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
WINDOWS = ((1, "First 24 hours"), (3, "First 3 days"), (7, "First 7 days"))


def parse_dt(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def latest_windows(latest, now=None):
    """Downloads of the newest episode in its first 1, 3 and 7 days.
    Podhome returns daily buckets, so "day 1" is the UTC calendar day of the publish date.
    A window is marked partial until the full time has passed since publishing."""
    ep = (latest or {}).get("episode")
    pub = parse_dt(ep.get("publish_date")) if ep else None
    if pub is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    by_day = {}
    for r in rows(latest.get("trend")):
        m = DATE.match(str(r["label"]))
        if m:
            d = dt.date.fromisoformat(m.group())
            by_day[d] = by_day.get(d, 0) + r["value"]
    start, out = pub.date(), []
    for days, label in WINDOWS:
        total = sum(v for d, v in by_day.items() if 0 <= (d - start).days < days) if by_day else None
        out.append({"label": label, "value": total, "partial": now < pub + dt.timedelta(days=days)})
    return {"title": ep.get("title"), "published": pub.isoformat(), "windows": out}
