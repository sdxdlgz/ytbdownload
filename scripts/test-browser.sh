#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON_BIN=.venv/bin/python; else PYTHON_BIN=python3; fi
fi

PORT="${BROWSER_TEST_PORT:-8765}"
DATA_DIR="$(mktemp -d -t ytbdownload-browser-XXXXXX)"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill -TERM -- "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT INT TERM

export YTDLP_WEB_ENVIRONMENT=test
export YTDLP_WEB_ACCESS_TOKEN=""
export YTDLP_WEB_APP_SECRET="browser-test-secret"
export YTDLP_WEB_COOKIE_SECURE=false
export YTDLP_WEB_ALLOWED_HOSTS="127.0.0.1,localhost"
export YTDLP_WEB_TRUSTED_PROXY=false
export YTDLP_WEB_X_ACCEL_REDIRECT=false
export YTDLP_WEB_ALLOW_PRIVATE_URLS=false
export YTDLP_WEB_DATA_DIR="$DATA_DIR"
export YTDLP_WEB_JS_RUNTIME=""
export YTDLP_WEB_MIN_FREE_DISK_MB=32

setsid "$PYTHON_BIN" -m uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT" --workers 1 --no-access-log &
SERVER_PID=$!

for _attempt in $(seq 1 240); do
  if curl --fail --silent "http://127.0.0.1:$PORT/api/v1/health/live" >/dev/null; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Browser-test server exited early." >&2
    exit 1
  fi
  sleep 0.25
done

if ! curl --fail --silent "http://127.0.0.1:$PORT/api/v1/health/live" >/dev/null; then
  echo "Browser-test server did not become ready." >&2
  exit 1
fi

if [[ "$PORT" != "8765" ]]; then
  echo "tests/browser_smoke.py currently expects port 8765." >&2
  exit 2
fi

"$PYTHON_BIN" tests/browser_smoke.py
