"""The break-glass password, for when Home Auth is down.

Kept as a scrypt hash in DATA_DIR/password.json. Set or change it on the NAS (asks twice, shows nothing):
    sudo /usr/local/bin/docker exec -it crypto-tracker python -m tracker.password
Remove it with `--remove`. With no password set, the sign-in page only offers Home Auth.
"""

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

N, R, P = 2**15, 8, 1
MIN_LENGTH = 12


def _file(data_dir):
    return Path(data_dir) / "password.json"


def _hash(password, salt, n=N, r=R, p=P):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def is_set(data_dir):
    return _file(data_dir).is_file()


def set_password(data_dir, password):
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    record = {"n": N, "r": R, "p": P, "salt": base64.b64encode(salt).decode(),
              "hash": base64.b64encode(_hash(password, salt)).decode()}
    path = _file(data_dir)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


def check(data_dir, password):
    try:
        record = json.loads(_file(data_dir).read_text())
        good = base64.b64decode(record["hash"])
        tried = _hash(password, base64.b64decode(record["salt"]), record["n"], record["r"], record["p"])
    except (FileNotFoundError, ValueError, KeyError):
        return False
    return bool(password) and hmac.compare_digest(tried, good)


def main(argv):
    data_dir = os.environ.get("DATA_DIR", "/data")
    if argv == ["--remove"]:
        _file(data_dir).unlink(missing_ok=True)
        print("Break-glass password removed; only Home Auth signs in now.")
        return 0
    first = getpass.getpass("New break-glass password: ")
    if first != getpass.getpass("Again: "):
        print("They don't match; nothing changed.")
        return 1
    try:
        set_password(data_dir, first)
    except ValueError as e:
        print(e)
        return 1
    print("Saved. Existing sign-ins stay signed in.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
