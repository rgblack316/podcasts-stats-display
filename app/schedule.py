"""When each show is polled: automatic (every N hours), every N hours from a chosen start time,
or at fixed times of day. Times are wall-clock times in the configured time zone."""
import datetime as dt, math, os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_TIMES = 12


def parse_hhmm(s):
    """Minutes after midnight for "HH:MM" (24-hour), or None."""
    if not isinstance(s, str) or len(s.strip()) not in (4, 5) or ":" not in s:
        return None
    h, _, m = s.strip().partition(":")
    if not (h.isdigit() and m.isdigit() and len(m) == 2):
        return None
    h, m = int(h), int(m)
    return h * 60 + m if h < 24 and m < 60 else None


def fmt_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def zone(name):
    """ZoneInfo for `name`, or None if it isn't a valid IANA time zone name."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError, KeyError):
        return None


def default_zone_name():
    tz = os.environ.get("TZ", "")
    return tz if tz and zone(tz) else "UTC"


def _local_epoch(day, minutes, tz):
    return dt.datetime.combine(day, dt.time(minutes // 60, minutes % 60), tzinfo=tz).timestamp()


def slots(show, every_hours, tz, now):
    """(previous_slot, next_slot) as epoch seconds around `now`, or None for automatic scheduling."""
    if show.get("times"):
        today = dt.datetime.fromtimestamp(now, tz).date()
        found = sorted(_local_epoch(today + dt.timedelta(days=d), parse_hhmm(t), tz)
                       for d in (-1, 0, 1) for t in show["times"])
        past = [x for x in found if x <= now]
        future = [x for x in found if x > now]
        return past[-1], future[0]
    if show.get("start_time"):
        today = dt.datetime.fromtimestamp(now, tz).date()
        anchor, step = _local_epoch(today, parse_hhmm(show["start_time"]), tz), every_hours * 3600
        prev = anchor + math.floor((now - anchor) / step) * step
        return prev, prev + step
    return None
