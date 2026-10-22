"""Protection for the settings page.

First visit: a one-time setup code (printed in the install output and the container log, also kept in
data/setup-code.txt until used) lets you choose a password. After that you sign in with the password.
The password is stored only as a scrypt hash in data/admin.json. To reset it, delete that file and
restart the container: a new setup code is printed.
"""
import base64, hashlib, hmac, json, logging, os, secrets, time

log = logging.getLogger("stats.auth")
SESSION_SECONDS = 12 * 3600
MIN_PASSWORD = 8
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Auth:
    def __init__(self, data_dir):
        self.admin_path = data_dir / "admin.json"
        self.code_path = data_dir / "setup-code.txt"
        self.fails = {}  # client -> (count, first_fail_time or lock_until)
        self.secret = secrets.token_bytes(32)
        self.setup_code = None
        self.reload()

    # ---- state -------------------------------------------------------------------------------
    def reload(self):
        try:
            self.admin = json.loads(self.admin_path.read_text())
            if not all(k in self.admin for k in ("salt", "hash", "secret")):
                raise ValueError("incomplete")
            self.secret = _unb64(self.admin["secret"])
            self.setup_code = None
        except FileNotFoundError:
            self.admin = None
        except (ValueError, OSError):
            log.error("data/admin.json is unreadable; delete it and restart to set a new password")
            self.admin = None

    @property
    def configured(self):
        return self.admin is not None

    def ensure_setup_code(self):
        """Create the one-time code when no password is set yet. Returns it (or None if configured)."""
        self.reload()
        if self.configured:
            try:
                self.code_path.unlink()
            except FileNotFoundError:
                pass
            return None
        try:
            code = self.code_path.read_text().strip()
        except OSError:
            code = ""
        if not code:
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
            code = "-".join(raw[i:i + 4] for i in (0, 4, 8))
            try:
                self.code_path.write_text(code + "\n")
                os.chmod(self.code_path, 0o600)
            except OSError:
                log.warning("could not write data/setup-code.txt", exc_info=True)
        self.setup_code = code
        log.warning("SETTINGS SETUP CODE: %s  (open /settings and enter it to choose a password)", code)
        return code

    # ---- throttling --------------------------------------------------------------------------
    def locked(self, who):
        n, t = self.fails.get(who, (0, 0))
        if n >= MAX_FAILS:
            if time.time() < t:
                return int(t - time.time()) + 1
            self.fails.pop(who, None)
        elif n and time.time() - t > LOCK_SECONDS:
            self.fails.pop(who, None)
        return 0

    def _fail(self, who):
        n, t = self.fails.get(who, (0, time.time()))
        n += 1
        self.fails[who] = (n, time.time() + LOCK_SECONDS if n >= MAX_FAILS else t)
        if len(self.fails) > 1000:
            self.fails.clear()

    # ---- actions -----------------------------------------------------------------------------
    def _save_password(self, password):
        salt = secrets.token_bytes(16)
        self.admin = {"salt": _b64(salt), "hash": _b64(_scrypt(password, salt)),
                      "secret": _b64(secrets.token_bytes(32))}  # new secret signs out old sessions
        tmp = self.admin_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.admin))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.admin_path)
        self.secret = _unb64(self.admin["secret"])
        self.setup_code = None
        try:
            self.code_path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def password_problem(password):
        if len(password) < MIN_PASSWORD:
            return f"Password must be at least {MIN_PASSWORD} characters"
        if len(password) > 200:
            return "Password is too long"
        return None

    def setup(self, who, code, password):
        """Returns (ok, message)."""
        wait = self.locked(who)
        if wait:
            return False, f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes."
        if self.configured:
            return False, "A password is already set. Sign in instead."
        if not self.setup_code:
            self.ensure_setup_code()
        norm = lambda s: "".join(c for c in (s or "").upper() if c.isalnum())
        if not hmac.compare_digest(norm(code).encode(), norm(self.setup_code).encode()):
            self._fail(who)
            return False, "That setup code is not right."
        msg = self.password_problem(password)
        if msg:
            return False, msg
        self._save_password(password)
        self.fails.pop(who, None)
        return True, ""

    def login(self, who, password):
        wait = self.locked(who)
        if wait:
            return False, f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes."
        if not self.configured:
            return False, "No password is set yet."
        ok = hmac.compare_digest(_scrypt(password or "", _unb64(self.admin["salt"])), _unb64(self.admin["hash"]))
        if not ok:
            self._fail(who)
            return False, "Wrong password."
        self.fails.pop(who, None)
        return True, ""

    def change_password(self, who, old, new):
        ok, msg = self.login(who, old)
        if not ok:
            return False, msg if msg != "Wrong password." else "Current password is wrong."
        msg = self.password_problem(new)
        if msg:
            return False, msg
        self._save_password(new)
        return True, ""

    # ---- sessions ----------------------------------------------------------------------------
    def new_session(self):
        """(cookie value, csrf token)"""
        csrf = secrets.token_urlsafe(24)
        body = _b64(json.dumps({"exp": int(time.time()) + SESSION_SECONDS, "csrf": csrf}).encode())
        sig = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}", csrf

    def session(self, cookie):
        """csrf token if the cookie is a valid, unexpired session; else None."""
        if not cookie or not self.configured or "." not in cookie:
            return None
        body, _, sig = cookie.partition(".")
        good = _b64(hmac.new(self.secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(_unb64(body))
            return data["csrf"] if data["exp"] > time.time() else None
        except (ValueError, KeyError, TypeError):
            return None
