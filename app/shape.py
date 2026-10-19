"""Tolerant readers for Podhome's analytics JSON (its response schema is not published).
If the dashboard shows wrong numbers, look at /api/raw/<show> and adjust here."""
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
