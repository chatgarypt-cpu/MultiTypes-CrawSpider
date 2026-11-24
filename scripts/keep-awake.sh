#!/usr/bin/env bash
# Hold a macOS power assertion while crawler processes are running.
# Stops caffeinate automatically when no collect.py --site process remains.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/data/keep-awake.pid"
CAF=""

cleanup() {
  if [ -n "$CAF" ]; then kill "$CAF" 2>/dev/null || true; fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

while true; do
  if pgrep -f "collect.py --site" >/dev/null 2>&1; then
    if [ -z "$CAF" ] || ! kill -0 "$CAF" 2>/dev/null; then
      caffeinate -dimsu &
      CAF=$!
      echo "$CAF" > "$PIDFILE"
    fi
  elif [ -n "$CAF" ]; then
    kill "$CAF" 2>/dev/null || true
    CAF=""
    rm -f "$PIDFILE"
  fi
  sleep 30
done
