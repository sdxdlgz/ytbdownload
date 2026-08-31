#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -d .git ]]; then
  echo "This updater must run from the SSH-cloned Git checkout." >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Refusing to update a checkout with uncommitted changes." >&2
  git status --short >&2
  exit 1
fi

remote_url="$(git remote get-url origin)"
if [[ "$remote_url" != git@github.com:* && "$remote_url" != ssh://* ]]; then
  echo "origin is not an SSH remote: $remote_url" >&2
  echo "Set it with: git remote set-url origin git@github.com:sdxdlgz/ytbdownload.git" >&2
  exit 1
fi

git fetch --prune origin
git pull --ff-only origin main

if [[ $EUID -eq 0 ]]; then
  "$ROOT_DIR/scripts/install-debian.sh" "$@"
else
  sudo "$ROOT_DIR/scripts/install-debian.sh" "$@"
fi
