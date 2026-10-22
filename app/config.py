"""Reads, checks and saves data/config.json.

check() never raises: it returns a usable config plus a list of problems, so a typo in one show
doesn't stop the others from working. Each problem is {level, path, message, show}.
"""
import copy, hashlib, json, logging, os, re, uuid
from urllib.parse import urlsplit

log = logging.getLogger("stats.config")

DEFAULTS = {"interval_hours": 6, "slide_seconds": 60, "reload_minutes": 15}
LIMITS = {"interval_hours": (1, 168), "slide_seconds": (5, 3600), "reload_minutes": (1, 1440)}
LABELS = {"interval_hours": "interval_hours", "slide_seconds": "slide_seconds", "reload_minutes": "reload_minutes"}
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
ID_RE = re.compile(r"^[^\x00-\x1f]{1,200}$")
MAX_NAME = 80


def problem(level, path, message, show=None):
    return {"level": level, "path": path, "message": message, "show": show}


def new_id():
    return uuid.uuid4().hex[:10]


def is_placeholder(s):
    return "PASTE_" in s.upper() or s.strip().upper() in ("", "TODO", "CHANGEME")


def check_ident(s):
    """None if `s` is a usable show identifier, else a message. Accepts a feed URL, a podcast:guid
    or an OP3 show ID."""
    if is_placeholder(s):
        return "still has the PASTE_ placeholder; put the show's RSS feed URL here"
    if s.lower().startswith(("http://", "https://")):
        u = urlsplit(s)
        if not u.hostname or " " in s or "." not in u.hostname and u.hostname != "localhost":
            return "is not a valid web address (expected something like https://example.com/feed.xml)"
        return None
    if GUID_RE.match(s) or HEX32_RE.match(s):
        return None
    if "://" in s or s.lower().startswith(("feed:", "www.")) or "." in s:
        return "must start with http:// or https://"
    return "is not a feed URL, podcast:guid or OP3 show ID"


def check_image(s):
    if s.startswith("/art/"):
        return None if re.fullmatch(r"/art/[A-Za-z0-9_.\-]+", s) else "is not a valid uploaded-image path"
    u = urlsplit(s)
    if u.scheme not in ("http", "https") or not u.hostname or " " in s:
        return "must be a web address starting with http:// or https://"
    return None


def num(v):
    """int/float (not bool) or None."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def check_number(raw, key, probs, show=None, prefix="", required_default=True):
    """Validated value for an interval-style setting, or the default (or None for a show override)."""
    lo, hi = LIMITS[key]
    path = prefix + key
    lvl = "error" if required_default else "warning"  # a bad per-show override just falls back
    if key not in raw or raw[key] is None:
        return DEFAULTS[key] if required_default else None
    v = num(raw[key])
    if v is None or v != v:
        probs.append(problem(lvl, path, f"{key} must be a number (got {json.dumps(raw[key])[:40]}); using "
                             + (f"{DEFAULTS[key]}" if required_default else "the global value"), show))
        return DEFAULTS[key] if required_default else None
    if not lo <= v <= hi:
        probs.append(problem(lvl, path, f"{key} must be between {lo} and {hi} (got {v}); using "
                             + (f"{DEFAULTS[key]}" if required_default else "the global value"), show))
        return DEFAULTS[key] if required_default else None
    return v


def check(raw):
    """(config, problems). `config` always has interval_hours/slide_seconds/reload_minutes, op3_token,
    token_ok and shows; each show has id, name, feed_url, image_url, interval_hours (or None), valid."""
    probs = []
    cfg = {k: v for k, v in DEFAULTS.items()}
    cfg.update({"op3_token": "", "token_ok": False, "shows": []})
    if not isinstance(raw, dict):
        probs.append(problem("error", "", "config.json must be a JSON object ({ ... }) at the top level"))
        return cfg, probs

    for key in DEFAULTS:
        cfg[key] = check_number(raw, key, probs)
    if "podhome_api_key" in raw:
        probs.append(problem("warning", "podhome_api_key", "podhome_api_key is no longer used; you can delete it"))

    tok = raw.get("op3_token")
    if tok is None or tok == "":
        probs.append(problem("error", "op3_token", "op3_token is missing. Get one at https://op3.dev/api/keys"))
    elif not isinstance(tok, str):
        probs.append(problem("error", "op3_token", "op3_token must be text in quotes"))
    elif is_placeholder(tok):
        probs.append(problem("error", "op3_token", "op3_token still has the PASTE_ placeholder. Get a token at https://op3.dev/api/keys"))
    elif tok != tok.strip() or re.search(r"\s", tok.strip()):
        cfg["op3_token"] = "".join(tok.split())
        cfg["token_ok"] = True
        probs.append(problem("warning", "op3_token", "op3_token contained spaces or line breaks; they were ignored"))
    else:
        cfg["op3_token"], cfg["token_ok"] = tok, True

    shows = raw.get("shows")
    if shows is None:
        probs.append(problem("error", "shows", "\"shows\" is missing; add a list of shows"))
        return cfg, probs
    if not isinstance(shows, list):
        probs.append(problem("error", "shows", "\"shows\" must be a list ([ ... ])"))
        return cfg, probs
    if not shows:
        probs.append(problem("warning", "shows", "No shows are set up yet"))

    names, ids = {}, {}
    for i, s in enumerate(shows):
        p = f"shows[{i}]"
        if not isinstance(s, dict):
            probs.append(problem("error", p, f"Show #{i + 1} must be an object with a name and feed_url"))
            continue
        label = s.get("name") if isinstance(s.get("name"), str) and s.get("name").strip() else f"Show #{i + 1}"
        sp = []
        name = s.get("name")
        valid = True
        if not isinstance(name, str) or not name.strip():
            sp.append(problem("error", p + ".name", f"Show #{i + 1} has no name", label))
            name = ""
        else:
            name = " ".join(name.split())
            if len(name) > MAX_NAME:
                sp.append(problem("error", p + ".name", f"Name is longer than {MAX_NAME} characters", label))
            key = name.casefold()
            if key in names:
                sp.append(problem("error", p + ".name",
                                  f"Duplicate show name \"{name}\" (also show #{names[key] + 1}); names must be unique", label))
            else:
                names[key] = i
        ident = s.get("feed_url")
        if ident is None or (isinstance(ident, str) and not ident.strip()):
            sp.append(problem("error", p + ".feed_url", "feed_url is missing", label))
            ident = ""
        elif not isinstance(ident, str):
            sp.append(problem("error", p + ".feed_url", "feed_url must be text in quotes", label))
            ident = ""
        else:
            ident = ident.strip()
            msg = check_ident(ident)
            if msg:
                sp.append(problem("error", p + ".feed_url", "feed_url " + msg, label))
        image = s.get("image_url")
        if image is None or image == "":
            image = ""
        elif not isinstance(image, str):
            sp.append(problem("error", p + ".image_url", "image_url must be text in quotes", label))
            image = ""
        else:
            image = image.strip()
            msg = check_image(image)
            if msg:
                sp.append(problem("error", p + ".image_url", "image_url " + msg, label))
                image = ""
        every = check_number(s, "interval_hours", sp, label, p + ".", required_default=False)
        sid = s.get("id")
        if sid is None or sid == "":
            sid = name  # shows from older configs keep the name as their id, so stored data stays attached
        elif not isinstance(sid, str) or not ID_RE.match(sid):
            sp.append(problem("error", p + ".id", "id must be 1 to 200 characters of plain text", label))
            sid = ""
        if sid and sid in ids:
            sp.append(problem("error", p + ".id", f"Duplicate id \"{sid}\" (also show #{ids[sid] + 1})", label))
        elif sid:
            ids[sid] = i
        valid = not any(x["level"] == "error" for x in sp) and bool(sid) and bool(name)
        probs.extend(sp)
        cfg["shows"].append({"id": sid, "name": name, "feed_url": ident, "image_url": image,
                             "interval_hours": every, "valid": valid, "index": i})
    return cfg, probs


def serialize(raw):
    """Clean JSON-ready form of a settings submission: only known keys, empty optionals omitted."""
    out = {}
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
        out["op3_token"] = raw.get("op3_token", "")
        out["shows"] = []
        for s in raw.get("shows") if isinstance(raw.get("shows"), list) else []:
            if not isinstance(s, dict):
                continue
            item = {"id": s.get("id") or new_id(), "name": s.get("name", ""), "feed_url": s.get("feed_url", "")}
            if s.get("image_url"):
                item["image_url"] = s["image_url"]
            if s.get("interval_hours") not in (None, ""):
                item["interval_hours"] = s["interval_hours"]
            out["shows"].append(item)
    return out


def version_of(data: bytes):
    return hashlib.sha1(data).hexdigest()[:12]


class ConfigStore:
    """Loads config.json, re-reading only when the file changes."""

    def __init__(self, path):
        self.path = path
        self._sig = None
        self._cached = check(None)
        self._raw = None
        self._version = ""

    def _load(self):
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            sig = None
        if sig is not None and sig == self._sig:
            return
        self._sig = sig
        if sig is None:
            self._raw, self._version = None, ""
            cfg, probs = check({})
            probs = [p for p in probs if p["path"] not in ("op3_token", "shows")]
            probs.insert(0, problem("error", "", f"{self.path.name} was not found; create it on the settings page or copy config.example.json"))
            self._cached = (cfg, probs)
            return
        data = self.path.read_bytes()
        self._version = version_of(data)
        try:
            self._raw = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as e:
            self._raw = None
            cfg, _ = check({})
            where = f" (line {e.lineno}, column {e.colno})" if isinstance(e, json.JSONDecodeError) else ""
            self._cached = (cfg, [problem("error", "", f"{self.path.name} is not valid JSON{where}: "
                                          f"{getattr(e, 'msg', str(e))}. Check for a missing comma, quote or bracket.")])
            return
        self._cached = check(self._raw)

    def get(self):
        """(config, problems) for the current file."""
        self._load()
        cfg, probs = self._cached
        return cfg, list(probs)

    def raw(self):
        """The parsed file as written (None if missing/unreadable) and its version token."""
        self._load()
        return copy.deepcopy(self._raw), self._version

    def active_shows(self):
        return [s for s in self.get()[0]["shows"] if s["valid"]]

    def save(self, submitted, expect_version=None):
        """Validate and write. Returns (ok, problems, version). Refuses when something other than
        a missing token is wrong, or when the file changed since `expect_version` was read."""
        self._load()
        if expect_version is not None and expect_version != self._version:
            return False, [problem("error", "", "config.json was changed by someone else since you opened this page. Reload and try again.")], self._version
        clean = serialize(submitted)
        cfg, probs = check(clean)
        if any(p["level"] == "error" and p["path"] != "op3_token" for p in probs):
            return False, probs, self._version
        data = (json.dumps(clean, indent=2, ensure_ascii=False) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                (self.path.with_suffix(".json.bak")).write_bytes(self.path.read_bytes())
            except OSError:
                log.warning("could not write config backup", exc_info=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._sig = None
        self._load()
        return True, probs, self._version


def log_problems(probs):
    """Print config problems to the container log at startup."""
    if not probs:
        log.info("config.json OK")
        return
    for p in probs:
        where = f" [{p['show']}]" if p.get("show") else ""
        (log.error if p["level"] == "error" else log.warning)("config.json%s %s: %s", where, p["path"] or "-", p["message"])
