#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON_BIN=.venv/bin/python; else PYTHON_BIN=python3; fi
fi
DATA_DIR="$(mktemp -d -t ytbdownload-browser-real-XXXXXX)"
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
export YTDLP_WEB_DATA_DIR="$DATA_DIR"
export YTDLP_WEB_ACCESS_TOKEN=""
export YTDLP_WEB_APP_SECRET="browser-real-test-secret"
export YTDLP_WEB_COOKIE_SECURE=false
export YTDLP_WEB_ALLOWED_HOSTS="127.0.0.1,localhost"
export YTDLP_WEB_TRUSTED_PROXY=false
export YTDLP_WEB_X_ACCEL_REDIRECT=false
# Localhost is allowed only for the generated, isolated media fixture.
export YTDLP_WEB_ALLOW_PRIVATE_URLS=true
export YTDLP_WEB_JS_RUNTIME=""
export YTDLP_WEB_MIN_FREE_DISK_MB=32
export YTDLP_WEB_MAX_FILESIZE_MB=100

setsid "$PYTHON_BIN" -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8766 --workers 1 --no-access-log &
SERVER_PID=$!
for _attempt in $(seq 1 480); do
  curl --fail --silent http://127.0.0.1:8766/api/v1/health/live >/dev/null && break
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "Server exited early" >&2; exit 1; }
  sleep 0.25
done
curl --fail --silent http://127.0.0.1:8766/api/v1/health/live >/dev/null
if [[ -n "${BROWSER_E2E_EXPECT_BACKEND:-}" ]]; then
  ACTUAL_BACKEND="$(curl --fail --silent http://127.0.0.1:8766/api/v1/config | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["delivery"]["default_backend"])')"
  if [[ "$ACTUAL_BACKEND" != "$BROWSER_E2E_EXPECT_BACKEND" ]]; then
    echo "Expected backend $BROWSER_E2E_EXPECT_BACKEND, got $ACTUAL_BACKEND" >&2
    exit 1
  fi
fi
"$PYTHON_BIN" tests/browser_real_e2e.py
