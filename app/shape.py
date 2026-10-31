"""Turns OP3 JSON into what the page shows. Tolerant where OP3's docs don't pin the format down."""
import datetime as dt

NUM = (int, float)
VAL = ("downloads", "count", "value", "total")
LAB = ("name", "title", "app", "label", "appName", "agentName")
WINDOWS = ((1, "downloads1", "First 24 hours"), (3, "downloads3", "First 3 days"), (7, "downloads7", "First 7 days"))


def _num(v):
    return isinstance(v, NUM) and not isinstance(v, bool)


def _find_list(o):
    if isinstance(o, list) and o and all(isinstance(x, dict) for x in o):
        return o
    if isinstance(o, dict):
        for v in o.values():
            r = _find_list(v)
            if r:
                return r
    return []


def _find_map(o):
    """First dict whose values are all numbers, e.g. {"Apple Podcasts": 123, ...}."""
    if isinstance(o, dict):
        if o and all(_num(v) for v in o.values()):
            return o
        for v in o.values():
            r = _find_map(v)
            if r:
                return r
    return {}


def _pair(row):
    val = next((row[k] for k in VAL if _num(row.get(k))), None)
    if val is None:
        val = next((v for v in row.values() if _num(v)), 0)
    lab = next((row[k] for k in LAB if isinstance(row.get(k), str)), None)
    if lab is None:
        lab = next((v for v in row.values() if isinstance(v, str)), "")
    return {"label": lab, "value": val}


def ranked(payload, limit=8):
    """Label/value rows, biggest first, from a list of dicts or a {label: number} map."""
    rows = [_pair(r) for r in _find_list(payload)]
    if not rows:
        rows = [{"label": k, "value": v} for k, v in _find_map(payload).items()]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]


def parse_dt(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def trend(downloads, days=30, today=None):
    """Daily download counts for the last `days` days, oldest first, zero-filled."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    per_day = (downloads or {}).get("days", {})
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        out.append({"label": d, "value": per_day.get(d, 0)})
    return out


def top_episodes(episodes, limit=8):
    rows = [{"label": e.get("title") or "Untitled", "value": e["downloads30"],
             "lifetime": e["downloadsAll"] if _num(e.get("downloadsAll")) else None}
            for e in (episodes or {}).get("episodes", []) if _num(e.get("downloads30"))]
    rows = [r for r in rows if r["value"] > 0]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]


def latest_episode(episodes, now=None):
    """Newest already-published episode with its downloads in the first 1, 3 and 7 days.
    A window is marked partial until that much time has passed since publishing."""
    now = now or dt.datetime.now(dt.timezone.utc)
    best, best_pub = None, None
    for e in (episodes or {}).get("episodes", []):
        pub = parse_dt(e.get("pubdate"))
        if pub and pub <= now and (best_pub is None or pub > best_pub):
            best, best_pub = e, pub
    if best is None:
        return None
    windows = [{"label": label, "value": best.get(key) if _num(best.get(key)) else None,
                "partial": now < best_pub + dt.timedelta(days=days)} for days, key, label in WINDOWS]
    windows.append({"label": "Lifetime", "value": best["downloadsAll"] if _num(best.get("downloadsAll")) else None,
                    "partial": False})
    return {"title": best.get("title"), "published": best_pub.isoformat(), "windows": windows}


def lifetime(state, recent):
    """Show lifetime downloads: the saved running total of everything before `state["through"]`, plus
    the downloads from that date on. The two come from separate requests, so the 30-day numbers can be
    from a different poll than the saved total (one of them failed last time). Days counted in both are
    left out, so nothing is counted twice. `approx` is True when the real figure may be higher: the
    saved total is unfinished or older than the 30-day numbers (days in between are unknown), or the
    30-day fetch was cut short."""
    if not state or not recent:
        return None
    if not state.get("complete") and state.get("fallback"):
        # a fresh recount is still running: keep showing the last complete total until it finishes
        state = {**state["fallback"], "complete": False}
    since, through = recent.get("since"), state.get("through")
    start = recent.get("from") or since
    if through and start and start <= through and recent.get("days") and (through != since or start != since):
        # the per-day counts reach back to where the saved total stops (or the total already includes
        # some of the days): use only the days from there on, so nothing is missed or counted twice
        later = sum(n for day, n in recent["days"].items() if day >= through)
        return {"value": state["total"] + later, "approx": not state.get("complete") or bool(recent.get("truncated"))}
    exact = state.get("complete") and through == since and not recent.get("truncated")
    return {"value": state["total"] + recent["total"], "approx": not exact}


def countries(downloads, limit=8):
    rows = [{"label": k, "value": v} for k, v in (downloads or {}).get("countries", {}).items()]
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]
