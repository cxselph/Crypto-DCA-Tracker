"""The NAS server: sign-in, the version-checked store, backups and the nightly copies.

Run: python3 -I -m unittest discover -s tests -v  (standard library only)
"""

import base64
import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from tracker import backup, oidc, password, web  # noqa: E402

SAMPLE = {"tokens": {"PLS": {"chainId": "pulsechain"}}, "ledger": [{"id": "a", "symbol": "PLS", "quantity": 100}], "snapshots": []}


class FakeHomeAuth:
    """Discovery and a token endpoint that hands back an ID token with the claims a test sets."""

    def __init__(self):
        self.claims = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                doc = {"issuer": fake.issuer, "authorization_endpoint": fake.issuer + "/authorize",
                       "token_endpoint": fake.issuer + "/token"}
                self._json(doc)

            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                fake.last_form = form
                body = base64.urlsafe_b64encode(json.dumps(fake.claims).encode()).decode().rstrip("=")
                self._json({"id_token": f"e30.{body}.sig"})

            def _json(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.issuer = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.backups = root / "data", root / "nightly"
        self.home_auth = FakeHomeAuth()
        oidc._discovery.clear()
        self.config = web.Config({
            "DATA_DIR": str(self.data), "BACKUP_DIR": str(self.backups), "HOME_AUTH_USER": "owner",
            "OIDC_ISSUER": self.home_auth.issuer, "OIDC_REDIRECT_URI": "http://crypto.test/oidc/callback"})
        self.httpd = web.make_server(self.config, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home_auth.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def session(self):
        payload = {"u": "owner", "exp": int(time.time()) + 60, "ep": self.httpd.app.epoch()}
        return f"{web.SESSION_COOKIE}={web.sign(self.config.secret, 'session', payload)}"

    def load(self):
        res, data = self.request("GET", "/api/store", cookie=self.session())
        self.assertEqual(res.status, 200)
        return json.loads(data), res.getheader("ETag").strip('"')

    # ----- signed out -----

    def test_health_is_public_and_everything_else_needs_sign_in(self):
        res, data = self.request("GET", "/healthz")
        self.assertEqual((res.status, data), (200, b"ok"))
        res, _ = self.request("GET", "/")
        self.assertEqual((res.status, res.getheader("Location")), (303, "/login"))
        for method, path in (("GET", "/api/store"), ("GET", "/api/backups"), ("POST", "/api/store")):
            res, _ = self.request(method, path, body="{}" if method == "POST" else None)
            self.assertEqual(res.status, 401, path)
        res, data = self.request("GET", "/login")
        self.assertEqual(res.status, 200)
        self.assertIn(b"Sign in with Home Auth", data)
        self.assertNotIn(b"Break-glass", data)  # no password set

    def test_forged_or_expired_sessions_are_refused(self):
        good = web.sign(self.config.secret, "session", {"u": "owner", "exp": int(time.time()) + 60})
        body, mac = good.split(".")
        forged = web.sign(b"another key", "session", {"u": "owner", "exp": int(time.time()) + 60})
        expired = web.sign(self.config.secret, "session", {"u": "owner", "exp": int(time.time()) - 1})
        flow = web.sign(self.config.secret, "oidc", {"u": "owner", "exp": int(time.time()) + 60})
        for value in (forged, expired, flow, body + ".x" + mac, "nonsense"):
            res, _ = self.request("GET", "/api/store", cookie=f"{web.SESSION_COOKIE}={value}")
            self.assertEqual(res.status, 401, value)

    def test_sessions_from_before_restores_existed_stay_signed_in_until_the_first_restore(self):
        old = web.sign(self.config.secret, "session", {"u": "owner", "exp": int(time.time()) + 60})  # no "ep"
        self.assertEqual(self.request("GET", "/api/store", cookie=f"{web.SESSION_COOKIE}={old}")[0].status, 200)
        self.httpd.app.new_epoch()
        self.assertEqual(self.request("GET", "/api/store", cookie=f"{web.SESSION_COOKIE}={old}")[0].status, 401)

    def test_data_files_are_never_served(self):
        self.load()
        (self.data / "store.json").write_text(json.dumps(SAMPLE))
        for path in ("/store.json", "/data/store.json", "/secret.key", "/../data/store.json", "/tracker/web.py",
                     "/api/backups/..%2Fstore.json", "/api/backups/secret.key"):
            res, data = self.request("GET", path, cookie=self.session())
            self.assertIn(res.status, (400, 404), path)
            self.assertNotIn(b"pulsechain", data)

    # ----- the store -----

    def test_saves_must_build_on_the_latest_version(self):
        empty, v0 = self.load()
        self.assertEqual(empty, web.EMPTY_STORE)
        res, _ = self.request("POST", "/api/store", SAMPLE, cookie=self.session())
        self.assertEqual(res.status, 428)  # no If-Match
        res, _ = self.request("POST", "/api/store", SAMPLE, {"If-Match": f'"{v0}"'}, self.session())
        self.assertEqual(res.status, 200)
        v1 = res.getheader("ETag").strip('"')
        self.assertNotEqual(v0, v1)
        self.assertEqual(self.load(), (SAMPLE, v1))
        # A tab still on the first version can't overwrite: it gets the current copy back.
        res, data = self.request("POST", "/api/store", web.EMPTY_STORE, {"If-Match": f'"{v0}"'}, self.session())
        self.assertEqual(res.status, 409)
        self.assertEqual((json.loads(data), res.getheader("ETag").strip('"')), (SAMPLE, v1))
        self.assertEqual(json.loads((self.data / "store.json").read_text()), SAMPLE)

    def test_only_tracker_stores_are_saved(self):
        _, v = self.load()
        for body in ("not json", json.dumps([1]), json.dumps({"ledger": []}), json.dumps({**SAMPLE, "tokens": []})):
            res, _ = self.request("POST", "/api/store", body, {"If-Match": f'"{v}"'}, self.session())
            self.assertEqual(res.status, 400, body)
        self.assertFalse((self.data / "store.json").exists())

    def test_cross_site_posts_are_refused(self):
        _, v = self.load()
        for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            res, _ = self.request("POST", "/api/store", SAMPLE, {"If-Match": f'"{v}"', **headers}, self.session())
            self.assertEqual(res.status, 403, headers)
        ok = {"If-Match": f'"{v}"', "Origin": "http://crypto.test", "Sec-Fetch-Site": "same-origin"}
        res, _ = self.request("POST", "/api/store", SAMPLE, ok, self.session())
        self.assertEqual(res.status, 200)

    # ----- backups -----

    def restore(self, path, body="", cookie=None, query=""):
        return self.request("POST", path + query, body, {"Content-Type": "application/json"}, cookie or self.session())

    def put_store(self, data):
        (self.data / "store.json").write_text(json.dumps(data))

    def test_download_is_an_exact_copy_that_restores(self):
        self.put_store(SAMPLE)
        res, data = self.request("GET", "/api/backup/download", cookie=self.session())
        self.assertEqual(res.status, 200)
        self.assertRegex(res.getheader("Content-Disposition"), r'attachment; filename="crypto-tracker-[\d-]+\.json"')
        self.assertEqual(json.loads(data), SAMPLE)
        res, out = self.restore("/api/restore/check", data)
        self.assertEqual(json.loads(out)["report"]["entries"], 1)

    def test_nightly_backup_is_checked_recorded_and_listed(self):
        self.put_store(SAMPLE)
        status = backup.run_nightly(self.data / "store.json", self.backups, self.data, date(2026, 10, 10))
        self.assertEqual(status["last_ok"]["file"], "crypto-tracker-2026-10-10.json")
        self.assertEqual(status["last_ok"]["report"]["entries"], 1)
        res, data = self.request("GET", "/api/backup/status", cookie=self.session())
        self.assertEqual(json.loads(data)["status"]["last_ok"]["report"]["tokens"], 1)
        res, data = self.request("GET", "/api/backups", cookie=self.session())
        self.assertEqual([(f["name"], f["kind"]) for f in json.loads(data)["files"]],
                         [("crypto-tracker-2026-10-10.json", "nightly")])
        self.assertFalse(backup.overdue(self.backups))

    def test_a_failed_nightly_backup_shows_and_keeps_the_last_good_one(self):
        self.put_store(SAMPLE)
        backup.run_nightly(self.data / "store.json", self.backups, self.data, date(2026, 10, 9))
        (self.data / "store.json").write_text("{damaged")
        status = backup.run_nightly(self.data / "store.json", self.backups, self.data, date(2026, 10, 10))
        self.assertIn("damaged", status["last_error"]["message"])
        self.assertEqual(status["last_ok"]["file"], "crypto-tracker-2026-10-09.json")
        self.assertFalse((self.backups / "crypto-tracker-2026-10-10.json").exists())
        res, data = self.request("GET", "/api/backup/status", cookie=self.session())
        self.assertIn("last_error", json.loads(data)["status"])

    def test_prune_keeps_two_weeks_and_monthly_firsts_and_never_before_restore_copies(self):
        today = date(2026, 10, 10)
        names = [f"crypto-tracker-{d}.json" for d in
                 ("2026-10-09", "2026-09-27", "2026-09-26", "2026-09-01", "2025-11-01", "2025-10-01", "2025-09-15")]
        safety = "crypto-tracker-before-restore-20240101-120000.json"
        self.assertEqual(backup.to_remove(names + [safety, "other.json"], today),
                         ["crypto-tracker-2025-09-15.json", "crypto-tracker-2025-10-01.json",
                          "crypto-tracker-2026-09-26.json"])
        self.assertEqual(backup.run_nightly(self.data / "store.json", self.backups, self.data, today).get("last_ok"),
                         None)  # no data yet: nothing to back up, not a failure

    def test_schedule_maths(self):
        from datetime import datetime
        self.assertEqual(backup.seconds_until("02:30", datetime(2026, 10, 10, 2, 0)), 1800)
        self.assertEqual(backup.seconds_until("02:30", datetime(2026, 10, 10, 2, 30)), 24 * 3600)
        with self.assertRaises(ValueError):
            backup.seconds_until("25:00")
        self.assertTrue(backup.overdue(self.backups))  # none yet

    def test_restore_replaces_data_saves_the_current_copy_and_signs_everyone_out(self):
        self.put_store(SAMPLE)
        backup.run_nightly(self.data / "store.json", self.backups, self.data, date(2026, 10, 9))
        changed = {**SAMPLE, "ledger": SAMPLE["ledger"] + [{"id": "b", "symbol": "HEX", "quantity": 5}]}
        self.put_store(changed)
        other_device = self.session()
        res, data = self.restore("/api/restore", query="?name=crypto-tracker-2026-10-09.json&confirm=RESTORE")
        out = json.loads(data)
        self.assertEqual((res.status, out["report"]["entries"]), (200, 1))
        self.assertIn("Max-Age=0", res.getheader("Set-Cookie"))
        self.assertEqual(json.loads((self.data / "store.json").read_text()), SAMPLE)
        self.assertEqual(json.loads((self.backups / out["before"]).read_text()), changed)
        self.assertRegex(out["before"], backup.SAFETY_NAME)
        res, _ = self.request("GET", "/api/store", cookie=other_device)
        self.assertEqual(res.status, 401)  # signed out
        self.assertEqual(self.request("GET", "/api/store", cookie=self.session())[0].status, 200)  # new sign-in
        res, page = self.request("GET", f"/login?restored={out['before']}")
        self.assertIn(b"every device was signed out", page)
        self.assertIn(out["before"].encode(), page)
        # The status of this NAS's backups isn't touched by a restore.
        self.assertEqual(backup.read_status(self.data)["last_ok"]["file"], "crypto-tracker-2026-10-09.json")
        # The before-restore copy is listed, and restoring it undoes the restore.
        res, data = self.request("GET", "/api/backups", cookie=self.session())
        self.assertIn(("before-restore", out["before"]), [(f["kind"], f["name"]) for f in json.loads(data)["files"]])
        res, _ = self.restore("/api/restore", query=f"?name={out['before']}&confirm=RESTORE")
        self.assertEqual(json.loads((self.data / "store.json").read_text()), changed)

    def test_restore_from_an_uploaded_file_like_the_mac_apps_store(self):
        old_mac_store = json.dumps({"ledger": [{"symbol": "PLS", "quantity": "1000", "timestamp": "2026-05-01"}]})
        res, data = self.restore("/api/restore/check", old_mac_store)
        report = json.loads(data)["report"]
        self.assertEqual((report["entries"], report["snapshots"], report["newest_entry"]), (1, 0, "2026-05-01"))
        res, _ = self.restore("/api/restore", old_mac_store, query="?confirm=RESTORE")
        self.assertEqual(res.status, 200)
        restored = json.loads((self.data / "store.json").read_text())
        self.assertEqual((restored["tokens"], restored["snapshots"]), ({}, []))  # older file: missing parts added

    def test_check_only_and_unconfirmed_restores_change_nothing(self):
        self.put_store(SAMPLE)
        other = json.dumps({**SAMPLE, "ledger": []})
        for path, query in (("/api/restore/check", ""), ("/api/restore", ""), ("/api/restore", "?confirm=yes")):
            res, _ = self.restore(path, other, query=query)
            self.assertIn(res.status, (200, 400), path + query)
        self.assertEqual(json.loads((self.data / "store.json").read_text()), SAMPLE)
        self.assertFalse(list(self.backups.glob("*before-restore*")) if self.backups.exists() else [])
        self.assertEqual(self.request("GET", "/api/store", cookie=self.session())[0].status, 200)  # still signed in

    def test_damaged_or_foreign_files_are_refused(self):
        self.put_store(SAMPLE)
        for body in ("{damaged", "[1, 2]", json.dumps({"kids": []}), json.dumps({"ledger": [{"quantity": 1}]}),
                     json.dumps({"ledger": [], "tokens": []}), json.dumps({"ledger": [], "snapshots": [{"ts": 1}]})):
            for path in ("/api/restore/check", "/api/restore"):
                res, data = self.restore(path, body, query="?confirm=RESTORE")
                self.assertEqual(res.status, 422, (path, body))
                self.assertFalse(json.loads(data)["ok"])
        self.assertEqual(json.loads((self.data / "store.json").read_text()), SAMPLE)

    def test_stored_backups_by_exact_name_only(self):
        for name in ("../data/store.json", "crypto-tracker-2026-10-10.json/../x", "secret.key", "store.json"):
            res, _ = self.restore("/api/restore/check", query="?name=" + urllib.parse.quote(name))
            self.assertEqual(res.status, 404, name)
        for path in ("/api/backups/..%2Fstore.json", "/api/backups/secret.key"):
            self.assertIn(self.request("GET", path, cookie=self.session())[0].status, (400, 404))

    def test_backup_routes_need_sign_in(self):
        for method, path in (("GET", "/api/backup/download"), ("GET", "/api/backup/status"),
                             ("POST", "/api/restore/check"), ("POST", "/api/restore?confirm=RESTORE")):
            res, _ = self.request(method, path, "{}" if method == "POST" else None)
            self.assertEqual(res.status, 401, path)

    # ----- signing in -----

    def test_break_glass_password(self):
        with self.assertRaises(ValueError):
            password.set_password(self.data, "short")
        password.set_password(self.data, "correct horse battery")
        res, data = self.request("GET", "/login")
        self.assertIn(b"Break-glass", data)
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        res, _ = self.request("POST", "/login", "password=correct+horse+battery", form)
        self.assertEqual((res.status, res.getheader("Location")), (303, "/"))
        cookie = res.getheader("Set-Cookie").split(";")[0]
        res, _ = self.request("GET", "/api/store", cookie=cookie)
        self.assertEqual(res.status, 200)
        # Five wrong tries pause password sign-in, even with the right one.
        for _ in range(web.MAX_FAILURES):
            res, _ = self.request("POST", "/login", "password=wrong", form)
            self.assertEqual(res.status, 401)
        res, _ = self.request("POST", "/login", "password=correct+horse+battery", form)
        self.assertEqual(res.status, 429)

    def home_auth_sign_in(self, claims):
        res, _ = self.request("GET", "/oidc/login", headers={"Host": "crypto.test"})
        self.assertEqual(res.status, 303)
        authorize = urllib.parse.urlparse(res.getheader("Location"))
        params = dict(urllib.parse.parse_qsl(authorize.query))
        self.assertEqual((params["client_id"], params["code_challenge_method"]), ("crypto-tracker", "S256"))
        flow_cookie = res.getheader("Set-Cookie").split(";")[0]
        self.home_auth.claims = {"iss": self.home_auth.issuer, "aud": ["crypto-tracker"],
                                 "exp": time.time() + 60, "nonce": params["nonce"], **claims}
        return self.request("GET", f"/oidc/callback?code=abc&state={params['state']}", cookie=flow_cookie)

    def test_home_auth_lets_only_the_owner_in(self):
        res, _ = self.home_auth_sign_in({"preferred_username": "Owner", "groups": ["parents"]})
        self.assertEqual((res.status, res.getheader("Location")), (303, "/"))
        session = [c for c in res.headers.get_all("Set-Cookie") if c.startswith(web.SESSION_COOKIE + "=")][0]
        res, _ = self.request("GET", "/api/store", cookie=session.split(";")[0])
        self.assertEqual(res.status, 200)
        self.assertIn("code_verifier", self.home_auth.last_form)

        for claims, reason in (({"preferred_username": "someone", "groups": ["parents"]}, "denied"),
                               ({"preferred_username": "owner", "nonce": "other"}, "failed"),
                               ({"preferred_username": "owner", "aud": ["budget"]}, "failed"),
                               ({"preferred_username": "owner", "exp": time.time() - 5}, "failed")):
            res, _ = self.home_auth_sign_in(claims)
            self.assertEqual(res.getheader("Location"), f"/login?error={reason}", claims)
            self.assertFalse(any(c.startswith(web.SESSION_COOKIE + "=") and "Max-Age=0" not in c
                                 for c in res.headers.get_all("Set-Cookie")))

    def test_home_auth_callback_without_its_flow_cookie_is_refused(self):
        res, _ = self.request("GET", "/oidc/callback?code=abc&state=guess")
        self.assertEqual(res.getheader("Location"), "/login?error=expired")

    def test_home_auth_needs_the_owner_configured(self):
        self.config.home_auth_user = ""
        res, _ = self.home_auth_sign_in({"preferred_username": "owner"})
        self.assertEqual(res.getheader("Location"), "/login?error=unset")


if __name__ == "__main__":
    unittest.main()
