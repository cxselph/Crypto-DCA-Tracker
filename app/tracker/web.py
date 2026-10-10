"""The Crypto DCA Tracker's server on the NAS (https://crypto.home).

Serves the one-page app (static/index.html) to one person, and keeps their ledger in DATA_DIR/store.json.
Standard library only, so the image installs no packages.

Sign-in: Home Auth (oidc.py), accepted only for the Home Auth user in HOME_AUTH_USER, or the break-glass
password (password.py) for when Home Auth is down. A signed-in session is an HMAC-signed cookie for 30 days.

Saving: the page sends the whole store with If-Match set to the version it last loaded (a hash of
store.json). If another device saved since, the save is refused (409) with the current copy, so a stale tab
can never overwrite newer data.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backup, oidc, password

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html"}
PUBLIC_FILES = {"/icon.svg": ("icon.svg", "image/svg+xml"), "/build.json": ("build.json", "application/json")}
SESSION_COOKIE = "crypto_session"
FLOW_COOKIE = "crypto_oidc"
SESSION_SECONDS = 30 * 24 * 3600
MAX_BODY = 50 * 1024 * 1024
EMPTY_STORE = {"tokens": {}, "ledger": [], "snapshots": []}
CSV_NAME = re.compile(r"^[A-Za-z0-9._-]+\.csv$")
# Wrong passwords allowed before sign-in by password pauses (there's one person, so it's not per address).
MAX_FAILURES, FAILURE_WINDOW = 5, 15 * 60

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https:; connect-src 'self' https:; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


class Config:
    def __init__(self, env=os.environ):
        self.data_dir = Path(env.get("DATA_DIR", "/data"))
        backup_dir = env.get("BACKUP_DIR", "")
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.backup_time = env.get("NIGHTLY_BACKUP_TIME", "02:30")
        self.https_only = env.get("HTTPS_ONLY") == "1"
        self.home_auth_user = env.get("HOME_AUTH_USER", "").strip().lower()
        self.oidc = oidc.Settings(
            issuer=env.get("OIDC_ISSUER", ""), redirect_uri=env.get("OIDC_REDIRECT_URI", ""),
            client_id=env.get("OIDC_CLIENT_ID", "crypto-tracker"), ca=env.get("OIDC_CA_FILE", ""))
        self.secret = (env.get("SECRET_KEY") or "").encode() or _stored_secret(self.data_dir)


def _stored_secret(data_dir):
    """A random key kept in DATA_DIR/secret.key, made on first start. Deleting it signs everyone out."""
    path = data_dir / "secret.key"
    try:
        return path.read_bytes()
    except FileNotFoundError:
        data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


# ---------- signed cookies ----------

def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret, purpose, payload):
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(secret, purpose, value):
    """The payload of a cookie made by sign() for this purpose, or None if it's forged, mangled or expired."""
    try:
        body, mac = (value or "").split(".")
        good = hmac.new(secret, f"{purpose}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(mac), good):
            return None
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    return payload if payload.get("exp", 0) > time.time() else None


# ---------- the store ----------

class Store:
    """store.json, with its version (a hash of the file) checked on every write."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "store.json"
        self.lock = threading.Lock()

    def _read_bytes(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return json.dumps(EMPTY_STORE).encode()

    @staticmethod
    def version(raw):
        return hashlib.sha256(raw).hexdigest()[:20]

    def read(self):
        with self.lock:
            return self.read_unlocked()

    def read_unlocked(self):
        raw = self._read_bytes()
        return raw, self.version(raw)

    def replace_unlocked(self, data):
        """Put this data in place whole, whatever is there (restore; hold self.lock). Returns the new version."""
        raw = json.dumps(data, separators=(",", ":")).encode()
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        return self.version(raw)

    def write(self, data, expected_version):
        """(True, new version) once saved, or (False, (current bytes, current version)) when it changed."""
        with self.lock:
            current = self._read_bytes()
            if not hmac.compare_digest(self.version(current), expected_version):
                return False, (current, self.version(current))
            return True, self.replace_unlocked(data)


def valid_store(data):
    return (isinstance(data, dict) and isinstance(data.get("tokens"), dict)
            and isinstance(data.get("ledger"), list) and isinstance(data.get("snapshots"), list))


# ---------- the app ----------

class App:
    def __init__(self, config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.data_dir)
        self.csv_dir = config.data_dir / "backups"  # CSVs the page's Backup button saved before downloads
        self.epoch_path = config.data_dir / "session_epoch"  # a restore changes it, signing every device out
        self.failures = []
        self.failures_lock = threading.Lock()

    def password_paused(self):
        with self.failures_lock:
            now = time.monotonic()
            self.failures = [t for t in self.failures if now - t < FAILURE_WINDOW]
            return len(self.failures) >= MAX_FAILURES

    def password_failed(self):
        with self.failures_lock:
            self.failures.append(time.monotonic())

    def epoch(self):
        """The current session epoch; None until the first restore (so older sessions stay valid until then)."""
        try:
            return self.epoch_path.read_text().strip() or None
        except FileNotFoundError:
            return None

    def new_epoch(self):
        tmp = self.epoch_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(8))
        os.replace(tmp, self.epoch_path)

    def backup_files(self):
        """Nightly and before-restore backups, and any older CSV exports, newest first."""
        files = backup.stored_backups(self.config.backup_dir)
        if self.csv_dir.is_dir():
            for p in self.csv_dir.iterdir():
                if p.is_file() and CSV_NAME.match(p.name):
                    st = p.stat()
                    files.append({"name": p.name, "kind": "csv", "size": st.st_size,
                                  "modified": int(st.st_mtime * 1000)})
        files.sort(key=lambda f: f["modified"], reverse=True)
        return files

    def backup_path(self, name):
        """Where a backup with this name lives, or None. Names are matched exactly, never joined as paths."""
        if CSV_NAME.match(name or ""):
            return self.csv_dir / name
        return backup.stored_backup(self.config.backup_dir, name)


class Handler(BaseHTTPRequestHandler):
    server_version = "crypto-tracker"
    sys_version = ""
    timeout = 30  # a stalled client can't hold a thread

    @property
    def app(self):
        return self.server.app

    # ----- responses -----

    def _send(self, code, body=b"", content_type="text/plain; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        if self.app.config.https_only:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, payload, headers=()):
        self._send(code, json.dumps(payload).encode(), "application/json", headers)

    def _redirect(self, location, headers=()):
        self._send(303, b"", headers=(("Location", location), *headers))

    def _cookie(self, name, value, max_age):
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.app.config.https_only:
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k:
                out[k] = v
        return out

    def log_message(self, fmt, *args):
        # Paths only: no query strings (sign-in codes) in the NAS logs.
        super().log_message(fmt, *(re.sub(r"\?\S*", "", a) if isinstance(a, str) else a for a in args))

    # ----- who's asking -----

    def _user(self):
        s = unsign(self.app.config.secret, "session", self._cookies().get(SESSION_COOKIE))
        if not s or s.get("ep") != self.app.epoch():  # signed in before the last restore
            return None
        return s.get("u")

    def _session_cookie(self, user):
        payload = {"u": user, "exp": int(time.time()) + SESSION_SECONDS, "ep": self.app.epoch()}
        return self._cookie(SESSION_COOKIE, sign(self.app.config.secret, "session", payload), SESSION_SECONDS)

    def _same_origin(self):
        """POSTs must come from this site's own pages (the session cookie is SameSite=Lax as well)."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            allowed = {self.headers.get("Host", "")}
            if self.app.config.oidc.redirect_uri:
                allowed.add(urllib.parse.urlparse(self.app.config.oidc.redirect_uri).netloc)
            return urllib.parse.urlparse(origin).netloc in allowed
        return True

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    # ----- routes -----

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok")
        if path in PUBLIC_FILES:
            name, ctype = PUBLIC_FILES[path]
            try:
                return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
            except FileNotFoundError:
                return self._send(404, b"Not found")
        if path == "/login":
            return self._login_page()
        if path == "/oidc/login":
            return self._oidc_login()
        if path == "/oidc/callback":
            return self._oidc_callback()

        user = self._user()
        if not user:
            if path.startswith("/api/"):
                return self._json(401, {"error": "Signed out"})
            return self._redirect("/login")
        if path in STATIC_FILES:
            body = (STATIC_DIR / STATIC_FILES[path]).read_bytes()
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/api/store":
            raw, version = self.app.store.read()
            return self._send(200, raw, "application/json", (("ETag", f'"{version}"'),))
        if path == "/api/backups":
            return self._json(200, {"folder": "backups", "files": self.app.backup_files()})
        if path == "/api/backup/status":
            return self._json(200, {"status": backup.read_status(self.app.config.data_dir),
                                    "time": self.app.config.backup_time,
                                    "nightly": bool(self.app.config.backup_dir)})
        if path == "/api/backup/download":
            raw, _ = self.app.store.read()
            name = f"crypto-tracker-{time.strftime('%Y-%m-%d-%H%M')}.json"
            return self._send(200, raw, "application/json",
                              (("Content-Disposition", f'attachment; filename="{name}"'),))
        if path.startswith("/api/backups/"):
            target = self.app.backup_path(urllib.parse.unquote(path[len("/api/backups/"):]))
            if target is None:
                return self._json(400, {"error": "Invalid filename"})
            try:
                data = target.read_bytes()
            except FileNotFoundError:
                return self._json(404, {"error": "Not found"})
            ctype = "text/csv; charset=utf-8" if target.suffix == ".csv" else "application/json"
            return self._send(200, data, ctype)
        return self._send(404, b"Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._same_origin():
            return self._json(403, {"error": "Cross-site request refused"})
        if parsed.path == "/login":
            return self._password_login()
        if parsed.path == "/logout":
            return self._redirect("/login?out=1", (self._cookie(SESSION_COOKIE, "", 0),))
        if not self._user():
            return self._json(401, {"error": "Signed out"})
        if parsed.path == "/api/store":
            return self._save_store()
        if parsed.path in ("/api/restore/check", "/api/restore"):
            return self._restore(parsed, check_only=parsed.path.endswith("/check"))
        return self._json(404, {"error": "Not found"})

    def _restore(self, parsed, check_only):
        """Check (read-only) or restore a backup: a stored one by ?name=, or the uploaded file as the body."""
        query = urllib.parse.parse_qs(parsed.query)
        name = (query.get("name") or [""])[0]
        if name:
            path = backup.stored_backup(self.app.config.backup_dir, name)
            if path is None:
                return self._json(404, {"error": "There's no backup by that name on the NAS."})
            raw = path.read_bytes()
        else:
            raw = self._body()
            if raw is None:
                return self._json(400, {"error": "No file, or it's over 50 MB."})
        if check_only:
            try:
                _, report = backup.verify(raw)
            except backup.BackupError as e:
                return self._json(422, {"ok": False, "error": str(e)})
            return self._json(200, {"ok": True, "report": report})
        if (query.get("confirm") or [""])[0] != "RESTORE":
            return self._json(400, {"error": "Confirm the restore first."})
        if not self.app.config.backup_dir:
            return self._json(503, {"error": "Restore needs BACKUP_DIR, for the copy of the current data."})
        try:
            report, safety = backup.restore(self.app.store, raw, self.app.config.backup_dir)
        except backup.BackupError as e:
            return self._json(422, {"ok": False, "error": str(e)})
        self.log_error("Restored a backup (%s entries); the data before is in %s", report["entries"], safety)
        self.app.new_epoch()  # every device, this one too, signs in again
        return self._json(200, {"ok": True, "report": report, "before": safety},
                          (self._cookie(SESSION_COOKIE, "", 0),))

    def _save_store(self):
        expected = (self.headers.get("If-Match") or "").strip().strip('"')
        if not expected:
            return self._json(428, {"error": "If-Match (the version you loaded) is required"})
        body = self._body()
        if body is None:
            return self._json(400, {"error": "Empty or oversized body"})
        try:
            data = json.loads(body)
        except ValueError:
            return self._json(400, {"error": "Invalid JSON"})
        if not valid_store(data):
            return self._json(400, {"error": "Not a tracker store (needs tokens, ledger and snapshots)"})
        saved, result = self.app.store.write(data, expected)
        if saved:
            return self._json(200, {"ok": True}, (("ETag", f'"{result}"'),))
        raw, version = result
        return self._send(409, raw, "application/json", (("ETag", f'"{version}"'),))

    # ----- signing in -----

    def _start_session(self, user):
        return self._redirect("/", (self._session_cookie(user),))

    def _login_page(self, message="", code=200):
        if self._user():
            return self._redirect("/")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if not message:
            message = oidc.MESSAGES.get((query.get("error") or [""])[0], "")
        if not message and query.get("out"):
            message = "You're signed out."
        before = (query.get("restored") or [""])[0]
        if not message and before:
            message = "The backup is restored, and every device was signed out. Sign in again."
            if backup.SAFETY_NAME.match(before):
                message += f" The data from just before is saved as {before} (restore that to undo)."
        home_auth = bool(self.app.config.oidc.issuer and self.app.config.oidc.redirect_uri)
        has_password = password.is_set(self.app.config.data_dir)
        self._send(code, login_html(message, home_auth, has_password).encode(), "text/html; charset=utf-8")

    def _password_login(self):
        body = self._body() or b""
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        if self.app.password_paused():
            return self._login_page("Too many wrong passwords. Wait 15 minutes, or sign in with Home Auth.", 429)
        if not password.check(self.app.config.data_dir, (form.get("password") or [""])[0]):
            self.app.password_failed()
            return self._login_page("That password isn't right.", 401)
        return self._start_session("password")

    def _oidc_login(self):
        s = self.app.config.oidc
        if not (s.issuer and s.redirect_uri):
            return self._send(404, b"Not found")
        # Start on the host Home Auth returns to, or the flow cookie wouldn't come back. Only once.
        target = urllib.parse.urlparse(s.redirect_uri)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if self.headers.get("Host") != target.netloc and not query.get("moved"):
            return self._redirect(f"{target.scheme}://{target.netloc}/oidc/login?moved=1")
        try:
            url, flow = oidc.start(s)
        except Exception as e:
            self.log_error("Home Auth unreachable: %s", e)
            return self._redirect("/login?error=unreachable")
        flow["exp"] = int(time.time()) + oidc.FLOW_SECONDS
        cookie = self._cookie(FLOW_COOKIE, sign(self.app.config.secret, "oidc", flow), oidc.FLOW_SECONDS)
        return self._redirect(url, (cookie,))

    def _oidc_callback(self):
        s = self.app.config.oidc
        if not (s.issuer and s.redirect_uri):
            return self._send(404, b"Not found")
        flow = unsign(self.app.config.secret, "oidc", self._cookies().get(FLOW_COOKIE))
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
        clear = self._cookie(FLOW_COOKIE, "", 0)
        user, problem = oidc.finish(s, flow, query, self.app.config.home_auth_user)
        if problem:
            self.log_error("Home Auth sign-in refused: %s", problem)
            return self._redirect(f"/login?error={problem}", (clear,))
        return self._redirect("/", (clear, self._session_cookie(user)))


def login_html(message, home_auth, has_password):
    note = f'<p class="note">{html.escape(message)}</p>' if message else ""
    button = '<a class="btn" href="/oidc/login">Sign in with Home Auth</a>' if home_auth else ""
    pw = ""
    if has_password:
        pw = """<details><summary>Home Auth not working?</summary>
<form method="post" action="/login"><input type="password" name="password" placeholder="Break-glass password"
 autocomplete="current-password" required><button type="submit">Sign in</button></form></details>"""
    if not home_auth and not has_password:
        note += '<p class="note">No way to sign in is set up yet (see the README).</p>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · Crypto DCA Tracker</title>
<link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/icon.svg"><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1419;color:#e6e9ef;
font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{width:min(360px,calc(100% - 32px));background:#1a2029;border:1px solid #2d3543;border-radius:14px;padding:28px;text-align:center}}
img{{width:64px;height:64px}} h1{{font-size:20px;margin:12px 0 20px}}
.btn,button{{display:block;width:100%;box-sizing:border-box;padding:11px;border-radius:8px;border:0;background:#4f8cff;
color:#fff;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}}
.note{{color:#fbbf24;font-size:14px}} details{{margin-top:18px;color:#8b95a7;font-size:13px;text-align:left}}
input{{width:100%;box-sizing:border-box;margin:10px 0;padding:10px;border-radius:8px;border:1px solid #2d3543;
background:#0f1419;color:#e6e9ef;font:inherit}}</style></head>
<body><main><img src="/icon.svg" alt=""><h1>Crypto DCA Tracker</h1>{note}{button}{pw}</main></body></html>"""


def make_server(config, host="0.0.0.0", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.app = App(config)
    return httpd


def main():
    config = Config()
    httpd = make_server(config)
    if config.backup_dir:
        backup.start(config.data_dir / "store.json", config.backup_dir, config.data_dir, config.backup_time)
    print(f"Crypto DCA Tracker on port {httpd.server_address[1]}", flush=True)
    httpd.serve_forever()
