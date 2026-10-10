"""Backups: a download on demand, nightly backups on the NAS, and checked restores.

A backup is the store itself as one JSON file (tokens, ledger, snapshots), the same format as the old Mac app's
store.json, so any download, nightly copy or that file restores exactly:
- crypto-tracker-YYYY-MM-DD.json: nightly, in BACKUP_DIR (nightly-backups/ in the NAS stack folder), at
  NIGHTLY_BACKUP_TIME (container time, TZ from .env), plus a catch-up a few minutes after start when the newest
  is over a day old. Each is read back and checked with verify(), the same check a restore uses. The last 14
  days are kept, plus the 1st of each month for a year.
- crypto-tracker-before-restore-YYYYMMDD-HHMMSS.json: the data as it was just before a restore, so a restore can
  be undone by restoring that. Never pruned.
- Downloads (crypto-tracker-YYYY-MM-DD-HHMM.json) are made on demand and not kept on the NAS.
The outcome of the last nightly run is kept in DATA_DIR/backup_status.json, which is never part of a backup.
"""

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

NIGHTLY_NAME = re.compile(r"^crypto-tracker-(\d{4}-\d{2}-\d{2})\.json$")
SAFETY_NAME = re.compile(r"^crypto-tracker-before-restore-\d{8}-\d{6}\.json$")
KEEP_DAYS, KEEP_MONTHS_DAYS = 14, 365
CATCH_UP_DELAY = 180  # seconds after start, so a restart loop doesn't back up on every attempt


class BackupError(ValueError):
    """A file that can't be restored; the message says why, in words for the page."""


def _number(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def verify(raw):
    """Check that these bytes are a tracker backup, and return (the store with any missing parts added, report).

    Raises BackupError for a damaged file or one that isn't this app's. Older files (the Mac app's first
    versions had no snapshots) restore with the missing parts empty."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BackupError("This file is damaged or isn't JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("ledger"), list):
        raise BackupError("This isn't a Crypto DCA Tracker backup (there's no ledger in it).")
    data.setdefault("tokens", {})
    data.setdefault("snapshots", [])
    if not isinstance(data["tokens"], dict) or not isinstance(data["snapshots"], list):
        raise BackupError("This backup is damaged: its tokens or snapshots aren't in the right form.")
    for i, e in enumerate(data["ledger"], 1):
        if (not isinstance(e, dict) or not isinstance(e.get("symbol"), str) or not e["symbol"]
                or not _number(e.get("quantity"))):
            raise BackupError(f"This backup is damaged: ledger entry {i} has no symbol or quantity.")
    for s in data["snapshots"]:
        if not isinstance(s, dict) or not isinstance(s.get("ts"), str) or not _number(s.get("value")):
            raise BackupError("This backup is damaged: a portfolio snapshot has no time or value.")
    times = sorted(str(e.get("timestamp", "")) for e in data["ledger"] if e.get("timestamp"))
    snaps = sorted(s["ts"] for s in data["snapshots"])
    report = {"entries": len(data["ledger"]), "tokens": len(data["tokens"]),
              "symbols": len({e["symbol"] for e in data["ledger"]}), "snapshots": len(data["snapshots"]),
              "newest_entry": times[-1] if times else None, "newest_snapshot": snaps[-1] if snaps else None}
    return data, report


def _write(path, raw):
    """Write a file whole (temp file + rename), owner-only, and make sure it reached the disk."""
    tmp = path.with_name(f".backup-{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def stored_backups(backup_dir):
    """Nightly and before-restore backups on the NAS, newest first."""
    if not backup_dir or not Path(backup_dir).is_dir():
        return []
    out = []
    for p in Path(backup_dir).iterdir():
        kind = "nightly" if NIGHTLY_NAME.match(p.name) else "before-restore" if SAFETY_NAME.match(p.name) else None
        if kind and p.is_file():
            st = p.stat()
            out.append({"name": p.name, "kind": kind, "size": st.st_size, "modified": int(st.st_mtime * 1000)})
    out.sort(key=lambda f: f["modified"], reverse=True)
    return out


def stored_backup(backup_dir, name):
    """A stored backup by exact file name (no paths), or None."""
    if not backup_dir or not (NIGHTLY_NAME.match(name or "") or SAFETY_NAME.match(name or "")):
        return None
    path = Path(backup_dir) / name
    return path if path.is_file() else None


def restore(store, raw, backup_dir, now=None):
    """Check `raw`, save the current data as a before-restore backup, then put the backup in place.

    Returns (report, before-restore file name). Raises BackupError (nothing changed) for a bad file."""
    data, report = verify(raw)
    now = now or datetime.now()
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    safety = folder / f"crypto-tracker-before-restore-{now:%Y%m%d-%H%M%S}.json"
    with store.lock:
        current, _ = store.read_unlocked()
        _write(safety, current)
        store.replace_unlocked(data)
    return report, safety.name


def back_up(store_path, backup_dir, today=None):
    """Copy the store to today's nightly file and check it. Returns (path, report), or (None, None) without a store."""
    today = today or date.today()
    try:
        raw = Path(store_path).read_bytes()
    except FileNotFoundError:
        return None, None
    verify(raw)  # never keep a damaged copy as a backup
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"crypto-tracker-{today.isoformat()}.json"
    _write(target, raw)
    _, report = verify(target.read_bytes())  # what's on disk, with the same check a restore uses
    return target, report


def to_remove(names, today):
    """The nightly backups to delete: older than 14 days, except the 1st of a month within a year.
    Anything else (before-restore copies, other files) is never touched."""
    out = []
    for name in names:
        m = NIGHTLY_NAME.match(name)
        if not m:
            continue
        day = date.fromisoformat(m.group(1))
        age = (today - day).days
        if age < KEEP_DAYS or (day.day == 1 and age < KEEP_MONTHS_DAYS):
            continue
        out.append(name)
    return sorted(out)


def prune(backup_dir, today=None):
    today = today or date.today()
    removed = to_remove(os.listdir(backup_dir), today)
    for name in removed:
        (Path(backup_dir) / name).unlink(missing_ok=True)
    return removed


# ---------- status ----------

def read_status(data_dir):
    try:
        return json.loads((Path(data_dir) / "backup_status.json").read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save_status(data_dir, status):
    _write(Path(data_dir) / "backup_status.json", json.dumps(status).encode())


def run_nightly(store_path, backup_dir, data_dir, today=None):
    """One nightly run: back up, check, prune, and record the outcome (logged too). Never raises."""
    status = read_status(data_dir)
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        made, report = back_up(store_path, backup_dir, today)
        removed = prune(backup_dir, today)
        if made:
            status["last_ok"] = {"at": at, "file": made.name, "report": report}
            print(f"Nightly backup: {made.name} checked ({report['entries']} entries, {report['tokens']} tokens, "
                  f"{report['snapshots']} snapshots)" + (f"; removed {', '.join(removed)}" if removed else ""),
                  flush=True)
        else:
            print("Nightly backup: no data yet, nothing to back up", flush=True)
        status.pop("last_error", None)
    except Exception as e:
        status["last_error"] = {"at": at, "message": str(e) or e.__class__.__name__}
        print(f"Nightly backup FAILED: {e}", flush=True)
    status["last_run"] = at
    try:
        _save_status(data_dir, status)
    except OSError as e:
        print(f"Couldn't save the backup status: {e}", flush=True)
    return status


def overdue(backup_dir, now=None):
    """True when the newest nightly backup is over a day old, or there's none."""
    now = now or time.time()
    newest = max((f["modified"] for f in stored_backups(backup_dir) if f["kind"] == "nightly"), default=0)
    return now - newest / 1000 > 24 * 3600


def seconds_until(hhmm, now=None):
    now = now or datetime.now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def start(store_path, backup_dir, data_dir, hhmm):
    """The nightly schedule, in a thread of the one server process."""
    seconds_until(hhmm)  # a bad NIGHTLY_BACKUP_TIME stops the app at start, not silently at night

    def loop():
        time.sleep(CATCH_UP_DELAY)
        if overdue(backup_dir):  # the NAS was off at backup time, or this is the first start
            run_nightly(store_path, backup_dir, data_dir)
        while True:
            time.sleep(seconds_until(hhmm))
            run_nightly(store_path, backup_dir, data_dir)
            time.sleep(61)  # never twice in the same minute

    threading.Thread(target=loop, daemon=True, name="nightly-backup").start()
