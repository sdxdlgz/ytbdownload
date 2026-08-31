#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x .venv/bin/python ]]; then PYTHON_BIN=.venv/bin/python; else PYTHON_BIN=python3; fi
fi

PORT="${MINIO_TEST_PORT:-9000}"
CONTAINER="signal-minio-test-$$"
IMAGE="quay.io/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e"
ACCESS_KEY="signalminio"
SECRET_KEY="signal-minio-password-123"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:${PORT}:9000" \
  -e MINIO_ROOT_USER="$ACCESS_KEY" \
  -e MINIO_ROOT_PASSWORD="$SECRET_KEY" \
  "$IMAGE" server /data --console-address ':9001' >/dev/null

for _attempt in $(seq 1 120); do
  if curl --fail --silent "http://127.0.0.1:${PORT}/minio/health/ready" >/dev/null; then
    break
  fi
  if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "MinIO test container exited early." >&2
    exit 1
  fi
  sleep 0.5
done
curl --fail --silent "http://127.0.0.1:${PORT}/minio/health/ready" >/dev/null

MINIO_ENDPOINT="http://127.0.0.1:${PORT}" \
MINIO_ACCESS_KEY="$ACCESS_KEY" \
MINIO_SECRET_KEY="$SECRET_KEY" \
  "$PYTHON_BIN" -m pytest -q -m s3integration
