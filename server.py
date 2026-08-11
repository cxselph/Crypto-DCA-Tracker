#!/usr/bin/env python3
# Tiny localhost server for the Crypto DCA Tracker. Serves index.html, exposes
# /api/backups for listing/reading/writing CSV backups in ./backups/, and
# /api/store for the durable on-disk copy of the ledger (store.json) so the
# native app window's data survives even if the browser engine's own storage
# (localStorage) doesn't.
import json
import os
import re
import sys
from http.server import SimpleHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("DCA_PORT", "8765"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(SCRIPT_DIR, "backups")
STORE_PATH = os.path.join(SCRIPT_DIR, "store.json")
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.csv$")
MAX_BODY = 50 * 1024 * 1024
EMPTY_STORE = {"tokens": {}, "ledger": [], "snapshots": []}


def safe_backup_path(name: str) -> str:
    if not FILENAME_RE.match(name or ""):
        raise ValueError("Invalid filename")
    target = os.path.normpath(os.path.join(BACKUP_DIR, name))
    if not target.startswith(BACKUP_DIR + os.sep):
        raise ValueError("Path traversal blocked")
    return target


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code, message):
        self._send_json(code, {"error": message})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/store":
            try:
                if os.path.isfile(STORE_PATH):
                    with open(STORE_PATH, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                else:
                    data = EMPTY_STORE
                self._send_json(200, data)
            except Exception as e:
                self._send_error_json(500, str(e))
            return
        if path == "/api/backups":
            try:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                files = []
                for name in os.listdir(BACKUP_DIR):
                    if not name.lower().endswith(".csv"):
                        continue
                    full = os.path.join(BACKUP_DIR, name)
                    if not os.path.isfile(full):
                        continue
                    st = os.stat(full)
                    files.append({
                        "name": name,
                        "size": st.st_size,
                        "modified": int(st.st_mtime * 1000),
                    })
                files.sort(key=lambda f: f["modified"], reverse=True)
                self._send_json(200, {"folder": "backups", "files": files})
            except Exception as e:
                self._send_error_json(500, str(e))
            return
        if path.startswith("/api/backups/"):
            name = path[len("/api/backups/"):]
            try:
                target = safe_backup_path(name)
                with open(target, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self._send_error_json(404, "Not found")
            except ValueError as e:
                self._send_error_json(400, str(e))
            except Exception as e:
                self._send_error_json(500, str(e))
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/store":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send_error_json(400, "Empty or oversized body")
                return
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except Exception:
                self._send_error_json(400, "Invalid JSON")
                return
            try:
                tmp_path = STORE_PATH + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp_path, STORE_PATH)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_error_json(500, str(e))
            return
        if parsed.path != "/api/backup":
            self._send_error_json(404, "Not found")
            return
        name = (parse_qs(parsed.query).get("name") or [""])[0]
        try:
            target = safe_backup_path(name)
        except ValueError as e:
            self._send_error_json(400, str(e))
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send_error_json(400, "Empty or oversized body")
            return
        body = self.rfile.read(length)
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(body)
            self._send_json(200, {"ok": True, "name": name, "size": len(body)})
        except Exception as e:
            self._send_error_json(500, str(e))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def build_server(port=PORT):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    httpd = build_server()
    print(f"Serving {SCRIPT_DIR} on http://127.0.0.1:{PORT}/")
    print(f"Backups folder: {BACKUP_DIR}")
    print("Open: http://localhost:%d/index.html" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        httpd.server_close()


if __name__ == "__main__":
    main()
