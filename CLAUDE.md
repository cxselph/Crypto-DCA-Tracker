# Project instructions

Crypto DCA Tracker: one person's crypto ledger on the Synology NAS at https://crypto.home (README).

## Sensitive data
- This repo is PUBLIC. Never commit `store.json`, `data/`, `backups/`, `nightly-backups/`, `.env`, `deploy.local`, real holdings, the Home Auth username, or the NAS address.
- LAN/VPN only: no public reverse proxy or port forward without an explicit decision. The container listens only on the NAS itself (127.0.0.1:8093) behind the DSM proxy.

## App
- `app/tracker/` is standard library only, on purpose: no pip packages in the image. Ask before adding one.
- Sign-in: Home Auth client `crypto-tracker` (Home Auth repo; its policy names the one allowed user) plus the `HOME_AUTH_USER` check in `oidc.claims_problem`; break-glass password in `password.py`. Every route except `/healthz`, `/login`, `/oidc/*`, `/icon.svg` and `/build.json` needs a session.
- `/api/store` saves only with `If-Match` equal to the current version (hash of store.json); keep that, it's what stops a stale tab overwriting newer data. Only explicitly listed files are served; never serve a directory.
- Tests: `python3 -I -m unittest discover -s tests -v`; CI runs them.

## Deploying
- Here, "ship it" includes the deploy: after the merge, run `./deploy.sh` (from an up-to-date `main`), then verify on the NAS that `/healthz` answers, a signed-out request to `/` redirects to `/login`, sign-in works, and the container log has no errors. That's standing approval for this deploy; you don't need to ask again.
- `./deploy.sh` is the only allowed way to change the NAS from here (exact permission rule in the committed `.claude/settings.json`). Don't add broader SSH rules there, and don't change what the script does without a reviewed PR.
- `deploy.sh` runs `lock-folders.sh` (the `synology-deploy` skill's template; keep them in step) on `data/`, `nightly-backups/` and `.env`.
- Each machine also needs its own gitignored `deploy.local` (copy `deploy.local.example`); it holds the NAS login, so it isn't committed.

## Dependency updates (house rules)
- Base image pinned by digest, actions by commit SHA; Dependabot with a 14-day cooldown; never auto-merge.
