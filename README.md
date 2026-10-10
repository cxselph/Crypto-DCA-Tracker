# Crypto DCA Tracker

Tracks dollar-cost-averaged crypto positions: average cost basis, realized and unrealized P&L, staking and LP activity, and portfolio value over time. It runs on the home Synology NAS at **https://crypto.home** (LAN, or away from home over Tailscale), for one person, signed in with Home Auth.

Until October 2026 it was a Mac app (a pywebview window around a local server, with the ledger in `store.json` on the Mac). That's retired; see [Moving from the Mac app](#moving-from-the-mac-app).

## Features

- **Transaction ledger:** record Buy, Sell, Stake, Unstake, LP In, LP Out and LP Fees transactions per token.
- **Dashboard:** per-token average cost basis, current value, and realized and unrealized gain/loss, plus a portfolio total.
- **Portfolio change pills:** 24h / 7d / 30d portfolio value change, from snapshots taken as you use it. A snapshot is only recorded once every held position has a price, so a partial price failure can't skew them.
- **Token lookup:** search by name or symbol, or paste a contract address, backed by the [Dexscreener](https://dexscreener.com) API for live prices. If Dexscreener is down for a token on a chain with RPC support (currently PulseChain), it reads the pool reserves from public RPC endpoints instead.
- **Backup and restore:** see [Backups and restore](#backups-and-restore).
- **Any device, one copy:** the ledger lives on the NAS. Every save says which version it builds on, so a tab left open on another device can't overwrite newer changes; it's shown the newer copy instead. Coming back to the page loads what other devices saved.

## How it runs

| Part | What it does |
|---|---|
| `app/static/index.html` | The whole app in the browser (ledger, dashboard, lookup, backup/restore). Prices come straight from Dexscreener and the RPCs. |
| `app/tracker/web.py` | The server: sign-in, the page, `/api/store` (the ledger, version-checked), backup and restore routes. Python standard library only, so the image installs no packages. |
| `app/tracker/oidc.py` | Sign in with Home Auth (OpenID Connect, PKCE). |
| `app/tracker/password.py` | The break-glass password for when Home Auth is down. |
| `app/tracker/backup.py` | Backups: the check every backup and restore uses, nightly copies, pruning, status, restore. |
| `compose.yaml`, `app/Dockerfile` | The NAS stack: port 8093 on the NAS itself only, read-only container, no capabilities. |
| `deploy.sh` | Ships merged code to the NAS (see [Deploying](#deploying)). |

Data on the NAS, in the stack folder `/volume1/docker/crypto-tracker`, owned by the app and closed to other NAS accounts:
- `data/store.json`: the ledger (tokens, entries, snapshots). `data/backup_status.json`: the last nightly run. `data/session_epoch`: changed by a restore, which signs every device out. `data/backups/`: CSVs from before downloads existed, if any. `data/secret.key`: signs sign-ins (made on first start; deleting it signs every device out). `data/password.json`: the break-glass password's hash, if set.
- `nightly-backups/`: nightly copies and before-restore copies (see below). Add the stack folder to Hyper Backup for a copy off the NAS.

## Backups and restore

A backup is one JSON file holding everything (tokens, ledger, portfolio snapshots), in the same format as the old Mac app's `store.json`, so any of them restores exactly.

- **Download backup** (Ledger section) saves `crypto-tracker-YYYY-MM-DD-HHMM.json` to your device, any time.
- **Nightly**, at 02:30 (`NIGHTLY_BACKUP_TIME`, container time from `TZ`), into `nightly-backups/crypto-tracker-YYYY-MM-DD.json`, plus a catch-up about 3 minutes after the app starts when the newest is over a day old. Each one is read back and checked with the same check a restore uses. The last 14 days are kept, plus the 1st of each month for a year.
- **Status:** Restore shows the last nightly backup (when, and how many entries, tokens and snapshots it held) or the last failure. A failure, or no backup for two days, also puts a red warning across the top of the page. Each run is logged too (`docker compose logs`).
- **Restore** (Ledger → Restore): pick a backup on the NAS, or **Browse elsewhere…** for a file on your device. It's checked first and shows what's in it; nothing changes unless you then confirm. A damaged file or one that isn't a tracker backup is refused. Restoring saves the current data first as `nightly-backups/crypto-tracker-before-restore-YYYYMMDD-HHMMSS.json` (never pruned; restore it to undo), puts the backup in place, and signs every device out. Older files without tokens or snapshots get those as empty.
- A CSV picked there (the old export format) is imported into the ledger instead, as before.

## Signing in

- **Home Auth** (https://auth.home): client `crypto-tracker`, whose Home Auth policy lets only the tracker's owner through. The app checks again that the Home Auth username matches `HOME_AUTH_USER` in the NAS `.env`.
- **Break-glass password**, for when Home Auth is down. Set or change it on the NAS (it asks twice and shows nothing):
  `sudo /usr/local/bin/docker exec -it crypto-tracker python -m tracker.password`
  (`… python -m tracker.password --remove` takes it away). Five wrong tries pause password sign-in for 15 minutes.
- Sign-ins last 30 days per device; **Sign out** is in the header.

## Setup (once)

1. UniFi: Host (A) record `crypto.home` → the NAS.
2. A certificate for `crypto.home` from the `.home` CA, imported in DSM, and a DSM reverse-proxy rule `https://crypto.home` (443) → `http://localhost:8093`, using that certificate.
3. Home Auth: the `crypto-tracker` client (Home Auth repo), deployed.
4. On the NAS, `/volume1/docker/crypto-tracker/.env` with `HOME_AUTH_USER=<your Home Auth username>` (`.env.example` lists the optional settings). Without it, Home Auth sign-in is refused.
5. `./deploy.sh` (needs `deploy.local`, below).

## Moving from the Mac app

1. Open https://crypto.home and sign in.
2. **Restore** → **Browse elsewhere…** → pick `store.json` from the old project folder on the Mac. It shows how many entries, tokens and snapshots it found, then restores them exactly.
3. Check the dashboard matches the Mac app, then quit the Mac app and delete **Crypto DCA Tracker** from `/Applications`. The Mac's `store.json`, `.venv/` and `backups/` can be archived or deleted after that.

## Deploying

`./deploy.sh`, from a clean `main` that matches GitHub (it refuses anything else and takes no arguments). It copies `compose.yaml`, `app/` and `lock-folders.sh` to the stack folder (saving the previous files in `backups/` there), locks `data/`, `nightly-backups/` and `.env` to the app's user, rebuilds and starts the container, and fails unless `/healthz` answers. `app/static/build.json` (served at `/build.json`) records the deployed commit. Each machine needs its own gitignored `deploy.local` (copy `deploy.local.example`); it holds the NAS login.

## Tests

`python3 -I -m unittest discover -s tests -v` (standard library only; CI runs it on every PR).

## Dependency updates

House rule: dependencies only change through a reviewed PR.

- The app has no Python packages. What it depends on is the base image in `app/Dockerfile` (pinned by digest) and the CI actions (pinned by commit SHA).
- **Dependabot** (`.github/dependabot.yml`) proposes updates weekly: base-image digests and patch versions, and action updates, each public for 14 days first. Never auto-merge them. A new Python minor version is a deliberate PR of its own.
