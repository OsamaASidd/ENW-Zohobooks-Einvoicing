#!/usr/bin/env bash
# Starts the ENW Construction Limited finance dashboard (if not already
# running) and opens it in Chrome. Run from Git Bash: ./start-dashboard.bash

set -e
cd "$(dirname "$0")"

PORT=8601
URL="http://127.0.0.1:${PORT}/invoices"
CHROME="/c/Program Files/Google/Chrome/Application/chrome.exe"

is_up() {
  curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" 2>/dev/null | grep -qE "^[23]"
}

if is_up; then
  echo "Dashboard already running on port ${PORT}."
else
  echo "Starting dashboard on port ${PORT}..."
  ./.venv/Scripts/python.exe -m uvicorn main:app --port "${PORT}" > uvicorn.out.log 2>&1 &
  disown

  for i in $(seq 1 20); do
    is_up && break
    sleep 0.5
  done

  if ! is_up; then
    echo "Dashboard did not come up - check uvicorn.out.log" >&2
    exit 1
  fi
  echo "Dashboard is up."
fi

echo "Opening ${URL} in Chrome..."
if [ -f "$CHROME" ]; then
  "$CHROME" "$URL" &
else
  cmd.exe /c start chrome "$URL"
fi
