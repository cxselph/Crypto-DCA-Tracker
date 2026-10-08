# Crypto DCA Tracker

A native macOS app for tracking dollar-cost-averaged crypto positions — average
cost basis, realized/unrealized P&L, staking and LP activity, and portfolio
value over time. Runs as a local app with all data stored on your own machine
(no cloud account, no external database).

## Features

- **Transaction ledger** — record Buy, Sell, Stake, Unstake, LP In, LP Out,
  and LP Fees transactions per token.
- **Dashboard** — per-token average cost basis, current value, and realized
  and unrealized gain/loss, plus a portfolio total.
- **Portfolio change pills** — 24h / 7d / 30d portfolio value change,
  computed from periodic snapshots captured automatically as you use the app.
  A snapshot is only recorded once every held position has a resolved price,
  so a partial price-fetch failure can't quietly skew these numbers.
- **Token lookup** — search by name/symbol or paste a contract address to
  find and link a token, backed by the [Dexscreener](https://dexscreener.com)
  API for live prices. If Dexscreener is unavailable for a token on a chain
  with RPC support configured (currently PulseChain), the app falls back to
  reading that token's pool reserves directly from the chain over a set of
  public RPC endpoints, so pricing keeps working through a Dexscreener outage.
- **Backup & restore** — export the full ledger and price-history snapshots
  to CSV, and restore from a previous backup (with legacy-format backups
  still supported).
- **Durable local storage** — data is persisted to `store.json` on disk via
  a small local server, so it survives app restarts independent of the
  browser engine's own storage.

## Requirements

- macOS
- Python 3 (via Homebrew or python.org)

## Setup

```bash
python3 -m venv .venv
./.venv/bin/pip install --require-hashes --only-binary=:all: --no-binary=proxy-tools -r requirements.txt
```

That installs exactly the versions and files recorded in `requirements.txt` (see [Dependency updates](#dependency-updates)).

## Running the app

### Native app window (recommended)

Build the launchable app bundle into `/Applications`:

```bash
./build_app.sh
```

Then launch **Crypto DCA Tracker** from `/Applications`, Launchpad, or the
Dock like any other Mac app.

> macOS blocks app bundles from launching directly out of `~/Documents` (and
> Desktop/Downloads), so `build_app.sh` installs the launcher to
> `/Applications` — the project folder itself stays right here. Re-run this
> script any time the icon needs updating or after a Python upgrade.

### Manual / browser fallback

You can also run the local server directly and open it in a browser:

```bash
./.venv/bin/python3 server.py
```

Then open `http://localhost:8765/index.html`.

## Data & backups

- All ledger and price-history data lives in `store.json` in this folder.
  It's gitignored — this is your financial data, not source code.
- Use the **Backup** button in the Ledger tab to export a timestamped CSV
  into `backups/` (also gitignored). Use **Restore** to import from one.
- Back up `store.json` and/or the `backups/` folder yourself (e.g. Time
  Machine, a synced drive) if you want off-machine redundancy.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | Native window launcher (pywebview) — runs the server in-process and shows the UI in a real macOS window. |
| `server.py` | Local HTTP server: serves the UI and exposes `/api/store` and `/api/backups` endpoints. |
| `index.html` | The entire UI/app logic (ledger, dashboard, lookup, backup/restore). |
| `build_app.sh` | Builds/reinstalls the `/Applications` launcher bundle. |
| `requirements.in` | The two packages the app uses directly. |
| `requirements.txt` | Hashed lockfile generated from `requirements.in`: every package pinned, every file hash-checked. |
| `icon.svg` | App icon source. |
| `store.json` | Durable local data store (gitignored). |
| `backups/` | CSV backups (gitignored). |

## Dependency updates

House rule: dependencies only change through a reviewed PR.

- **Exact installs:** `requirements.txt` is a hashed lockfile, installed with `./.venv/bin/pip install --require-hashes --only-binary=:all: --no-binary=proxy-tools -r requirements.txt`. Never fall back to a plain `pip install -r`/`--upgrade` to get it working; regenerate the lockfile instead.
- **One exception to "wheels only":** `proxy-tools` (needed by pywebview) is published only as source, so `--no-binary=proxy-tools` lets pip build it. Its source archive is still hash-checked, but the setuptools that pip fetches to build it isn't (a pip limitation).
- **To change a version:** edit `requirements.in`, then regenerate the lock with the `uv pip compile` command in its header, using an `--exclude-newer` date about 14 days back. Then recreate `.venv` with the Setup commands and rerun `./build_app.sh`.
- **Dependabot** (`.github/dependabot.yml`) proposes updates weekly. A release has to be public for 14 days first, and major versions are skipped. Security fixes skip both rules. Never auto-merge them.

