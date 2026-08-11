#!/bin/bash
# Double-click launcher: starts server.py in this folder and opens the app in your default browser.
cd "$(dirname "$0")" || exit 1

PORT="${DCA_PORT:-8765}"
URL="http://localhost:$PORT/index.html"

if lsof -i ":$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "Server is already running on port $PORT — opening browser only."
  open "$URL"
  exit 0
fi

echo "Starting Crypto DCA Tracker on port $PORT"
echo "Folder:  $(pwd)"
echo "Backups: $(pwd)/backups"
echo "URL:     $URL"
echo
echo "To stop: close this Terminal window, or press Ctrl+C."
echo

( sleep 1 && open "$URL" ) &

exec python3 server.py
