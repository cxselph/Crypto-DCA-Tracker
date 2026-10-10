"""Nightly backups of store.json into BACKUP_DIR (nightly-backups/ in the NAS stack folder).

At NIGHTLY_BACKUP_TIME each day (container time, TZ from .env) the store is copied to
crypto-tracker-YYYY-MM-DD.json, checked by reading it back, then old copies are pruned: the last 14 days are
kept, plus the first of each month for a year. These show up in the page's Restore list next to its own CSV
backups, so restoring one needs no SSH.
"""

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

NAME = re.compile(r"^crypto-tracker-(\d{4}-\d{2}-\d{2})\.json$")
KEEP_DAYS, KEEP_MONTHS_DAYS = 14, 365


def back_up(store_path, backup_dir, today=None):
    """Copy the store to today's file. Returns its path, or None when there's no store yet."""
    today = today or date.today()
    try:
        raw = Path(store_path).read_bytes()
    except FileNotFoundError:
        return None
    json.loads(raw)  # never keep a damaged copy as a backup
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"crypto-tracker-{today.isoformat()}.json"
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    if target.read_bytes() != raw:
        raise RuntimeError(f"{target.name} doesn't match the store after writing")
    return target


def to_remove(names, today):
    """The backup file names to delete: older than 14 days, except the 1st of a month within a year."""
    out = []
    for name in names:
        m = NAME.match(name)
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


def seconds_until(hhmm, now=None):
    now = now or datetime.now()
    hour, minute = (int(x) for x in hhmm.split(":"))
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def _run(store_path, backup_dir, hhmm):
    while True:
        time.sleep(seconds_until(hhmm))
        try:
            made = back_up(store_path, backup_dir)
            removed = prune(backup_dir)
            print(f"Nightly backup: {made.name if made else 'no store yet'}"
                  + (f"; removed {', '.join(removed)}" if removed else ""), flush=True)
        except Exception as e:
            print(f"Nightly backup FAILED: {e}", flush=True)
        time.sleep(61)  # never twice in the same minute


def start(store_path, backup_dir, hhmm):
    seconds_until(hhmm)  # a bad NIGHTLY_BACKUP_TIME stops the app at start, not silently at night
    threading.Thread(target=_run, args=(store_path, backup_dir, hhmm), daemon=True, name="nightly-backup").start()
