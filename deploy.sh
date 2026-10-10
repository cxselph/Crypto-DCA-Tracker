#!/bin/sh
# Deploy to the NAS: copy compose.yaml and app/ into the stack, fix ownership, create missing
# bind-mount folders (Synology's Docker won't), make them and .env private (lock-folders.sh),
# then build, `docker compose up -d`, and check the app actually came up.
# Run from a clean, up-to-date main so only merged (reviewed) code ships. Takes no arguments on purpose:
# it's allowed by an exact permission rule, so what it does is fixed by this file.
# Settings come from deploy.local (gitignored); see deploy.local.example.
set -eu
cd "$(dirname "$0")"

[ $# -eq 0 ] || { echo "deploy.sh takes no arguments" >&2; exit 2; }
[ -f deploy.local ] || { echo "Missing deploy.local (copy deploy.local.example and fill it in)" >&2; exit 1; }
. ./deploy.local
: "${NAS:?set NAS in deploy.local}" "${STACK_DIR:?set STACK_DIR in deploy.local}"
OWNER="${OWNER:-1027:100}"
case "$NAS" in *[!A-Za-z0-9@._-]*) echo "NAS has unexpected characters" >&2; exit 1;; esac
case "$STACK_DIR" in /*) ;; *) echo "STACK_DIR must be absolute" >&2; exit 1;; esac
case "$STACK_DIR$OWNER" in *[!A-Za-z0-9/_.:-]*) echo "STACK_DIR/OWNER have unexpected characters" >&2; exit 1;; esac

# Only ship what's merged: on main, nothing uncommitted, identical to GitHub.
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "Not on main" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Uncommitted changes; commit or stash them first" >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || { echo "main differs from origin/main; pull or push first" >&2; exit 1; }
echo "Deploying $(git log -1 --format='%h %s') to $NAS"
printf '{"commit": "%s", "committed": "%s", "deployed": "%s"}\n' \
  "$(git rev-parse --short HEAD)" "$(git log -1 --format=%cI)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > app/static/build.json

# data/ and .env live only on the NAS and are never shipped or overwritten.
FILES="compose.yaml app lock-folders.sh"
# After `up`, wait for this URL to answer (on the NAS itself) before calling the deploy good.
# Empty: only check that the containers stay up.
HEALTH_URL="http://127.0.0.1:8093/healthz"
case "$HEALTH_URL" in *[!A-Za-z0-9/_.:?=-]*) echo "HEALTH_URL has unexpected characters" >&2; exit 1;; esac
# Back up what's there, copy the new files over, keep the owner, apply the stack.
COPYFILE_DISABLE=1 tar --no-xattrs -czf - $FILES | ssh "$NAS" "set -e
  sudo -n mkdir -p '$STACK_DIR/backups'
  cd '$STACK_DIR'
  B='$STACK_DIR/backups/src-'\$(date +%Y%m%d-%H%M%S).tar.gz
  sudo -n tar czf \"\$B\" --ignore-failed-read $FILES
  echo \"Previous files saved to \$B\"
  sudo -n tar xzf - --no-same-owner
  sudo -n chown -R '$OWNER' $FILES
  C='sudo -n /usr/local/bin/docker compose'
  \$C config --quiet
  # Synology's Docker doesn't create missing bind-mount folders; make any under the stack folder.
  DIRS=\$(\$C config --format json | python3 -c 'import json, sys
for svc in json.load(sys.stdin)[\"services\"].values():
    for v in svc.get(\"volumes\", []):
        if v.get(\"type\") == \"bind\" and v[\"source\"].startswith(\"$STACK_DIR/\") and \".\" not in v[\"source\"].rsplit(\"/\", 1)[-1]:
            print(v[\"source\"])')
  for dir in \$DIRS; do
    [ -d \"\$dir\" ] || { sudo -n mkdir -p \"\$dir\"; echo \"Created \$dir\"; }
  done
  # Data folders: owned by the app's user, no access for any other NAS account. .env: owner only.
  sudo -n sh ./lock-folders.sh '$OWNER' \$DIRS
  sudo -n sh ./lock-folders.sh --secret .env
  \$C up -d --build
  # A container that can't start (crash loop) must fail the deploy, not pass silently.
  ok=
  for i in \$(seq 1 30); do
    if [ -n '$HEALTH_URL' ]; then
      python3 -c \"import sys, urllib.request, urllib.error
try: urllib.request.urlopen('$HEALTH_URL', timeout=3)
except urllib.error.HTTPError as e: sys.exit(e.code >= 500)\" 2>/dev/null && { ok=1; break; }
    elif [ \$i -ge 8 ]; then ok=1; break; fi
    sleep 2
  done
  for id in \$(\$C ps -q); do
    [ \"\$(sudo -n /usr/local/bin/docker inspect -f '{{.State.Running}} {{.State.Restarting}}' \"\$id\")\" = 'true false' ] || ok=
  done
  \$C ps
  [ -n \"\$ok\" ] || { echo \"The app didn't come up; last log lines:\" >&2; \$C logs --tail 30 >&2; exit 1; }
  echo 'App is up'"
