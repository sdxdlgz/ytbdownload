#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="/etc/ytbdownload-install.conf"

INSTALL_DIR="/opt/ytbdownload"
SERVICE_USER="ytbdownload"
ARTIFACT_GROUP="ytbdownload-artifacts"
DOMAIN="_"
EMAIL=""
DENO_VERSION="2.9.6"
SKIP_CERTBOT=0
SKIP_DENO=0
DOMAIN_EXPLICIT=0
ENV_CREATED=0

if [[ -r "$CONFIG_FILE" ]]; then
  # The file is root-owned and written by this script with shell-escaped values.
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi
# Skip flags are one-shot command options; never keep them sticky across updates.
SKIP_CERTBOT=0
SKIP_DENO=0

usage() {
  cat <<'EOF'
Install Signal / yt-dlp Web on Debian/Ubuntu.

Run from an SSH-cloned checkout:
  sudo ./scripts/install-debian.sh --domain dl.example.com --email admin@example.com

Options:
  --domain NAME          Public DNS name. Omit for HTTP-by-IP setup.
  --email ADDRESS        Let's Encrypt account email (requires --domain).
  --install-dir PATH     Runtime directory (default: /opt/ytbdownload).
  --service-user NAME    Dedicated system user (default: ytbdownload).
  --artifact-group NAME  Group shared only for completed artifacts (default: ytbdownload-artifacts).
  --deno-version VER     Deno version without leading v (default: 2.9.6).
  --skip-certbot         Do not request or modify a TLS certificate.
  --skip-deno            Do not install Deno (reduces YouTube compatibility).
  -h, --help             Show this help.

The script is idempotent. It preserves tokens, limits and data; runtime path/proxy settings stay installer-managed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:?missing domain}"; DOMAIN_EXPLICIT=1; shift 2 ;;
    --email) EMAIL="${2:?missing email}"; shift 2 ;;
    --install-dir) INSTALL_DIR="${2:?missing install path}"; shift 2 ;;
    --service-user) SERVICE_USER="${2:?missing service user}"; shift 2 ;;
    --artifact-group) ARTIFACT_GROUP="${2:?missing artifact group}"; shift 2 ;;
    --deno-version) DENO_VERSION="${2:?missing Deno version}"; shift 2 ;;
    --skip-certbot) SKIP_CERTBOT=1; shift ;;
    --skip-deno) SKIP_DENO=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer as root (use sudo)." >&2
  exit 1
fi
if [[ "$INSTALL_DIR" != /* || "$INSTALL_DIR" == "/" ]]; then
  echo "--install-dir must be a safe absolute path other than /." >&2
  exit 2
fi
if [[ ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]]; then
  echo "Invalid service user name." >&2
  exit 2
fi
if [[ ! "$ARTIFACT_GROUP" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]]; then
  echo "Invalid artifact group name." >&2
  exit 2
fi
if [[ "$DOMAIN" != "_" && ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Invalid domain name." >&2
  exit 2
fi
if [[ ! -f "$SOURCE_DIR/pyproject.toml" || ! -d "$SOURCE_DIR/app" ]]; then
  echo "Run this script from the project checkout." >&2
  exit 1
fi

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}" in
    debian|ubuntu) ;;
    *) echo "Warning: this installer is tested on Debian/Ubuntu, detected ${ID:-unknown}." >&2 ;;
  esac
fi

export DEBIAN_FRONTEND=noninteractive
packages=(ca-certificates curl ffmpeg git nginx python3 python3-pip python3-venv rsync unzip)
if [[ "$DOMAIN" != "_" && -n "$EMAIL" && $SKIP_CERTBOT -eq 0 ]]; then
  packages+=(certbot python3-certbot-nginx)
fi
apt-get update -qq
apt-get install -y --no-install-recommends "${packages[@]}"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$INSTALL_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi
if ! getent group "$ARTIFACT_GROUP" >/dev/null; then
  groupadd --system "$ARTIFACT_GROUP"
fi
usermod -a -G "$ARTIFACT_GROUP" "$SERVICE_USER"
if id www-data >/dev/null 2>&1; then
  usermod -a -G "$ARTIFACT_GROUP" www-data
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR"
rsync -a --delete --chown=root:root \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='.deno/' \
  --include='.env.example' \
  --exclude='.env*' \
  --exclude='secrets/' \
  --exclude='cookies.txt' \
  --exclude='*.cookies.txt' \
  --exclude='data/' \
  --exclude='artifacts/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='task_plan.md' \
  --exclude='findings.md' \
  --exclude='progress.md' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"
find "$INSTALL_DIR" -path "$INSTALL_DIR/.venv" -prune -o -path "$INSTALL_DIR/.deno" -prune \
  -o -path "$INSTALL_DIR/data" -prune -o -type d -exec chmod 0755 {} +
find "$INSTALL_DIR" -path "$INSTALL_DIR/.venv" -prune -o -path "$INSTALL_DIR/.deno" -prune \
  -o -path "$INSTALL_DIR/data" -prune -o -type f -exec chmod 0644 {} +
find "$INSTALL_DIR/scripts" -type f -name '*.sh' -exec chmod 0755 {} +

# Nginx gets traverse permission on data and read permission only on completed artifacts.
install -d -o "$SERVICE_USER" -g "$ARTIFACT_GROUP" -m 2710 "$INSTALL_DIR/data"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$INSTALL_DIR/data/work"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$INSTALL_DIR/data/logs"
install -d -o "$SERVICE_USER" -g "$ARTIFACT_GROUP" -m 2750 "$INSTALL_DIR/data/artifacts"
find "$INSTALL_DIR/data/artifacts" -type d -exec chown "$SERVICE_USER:$ARTIFACT_GROUP" {} + -exec chmod 2750 {} +
find "$INSTALL_DIR/data/artifacts" -type f -exec chown "$SERVICE_USER:$ARTIFACT_GROUP" {} + -exec chmod 0640 {} +

GENERATED_TOKEN=""
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  ENV_CREATED=1
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  GENERATED_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  GENERATED_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
else
  GENERATED_SECRET=""
fi

if [[ "$DOMAIN" == "_" ]]; then
  ALLOWED_HOSTS="*"
else
  ALLOWED_HOSTS="$DOMAIN,localhost,127.0.0.1"
fi

COOKIE_SECURE_VALUE="false"
if [[ "$DOMAIN" != "_" && -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]]; then
  COOKIE_SECURE_VALUE="true"
fi

ENV_FILE="$INSTALL_DIR/.env" \
INSTALL_DIR_VALUE="$INSTALL_DIR" \
ALLOWED_HOSTS_VALUE="$ALLOWED_HOSTS" \
COOKIE_SECURE_VALUE="$COOKIE_SECURE_VALUE" \
ENV_CREATED_VALUE="$ENV_CREATED" \
DOMAIN_EXPLICIT_VALUE="$DOMAIN_EXPLICIT" \
GENERATED_TOKEN_VALUE="$GENERATED_TOKEN" \
GENERATED_SECRET_VALUE="$GENERATED_SECRET" \
python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
updates = {
    "YTDLP_WEB_ENVIRONMENT": "production",
    "YTDLP_WEB_HOST": "127.0.0.1",
    "YTDLP_WEB_PORT": "8000",
    "YTDLP_WEB_DATA_DIR": f'{os.environ["INSTALL_DIR_VALUE"]}/data',
    "YTDLP_WEB_TRUSTED_PROXY": "true",
    "YTDLP_WEB_X_ACCEL_REDIRECT": "true",
    "YTDLP_WEB_X_ACCEL_PREFIX": "/_protected_downloads",
}
if os.environ["ENV_CREATED_VALUE"] == "1":
    updates.update({
        "YTDLP_WEB_COOKIE_SECURE": os.environ["COOKIE_SECURE_VALUE"],
        "YTDLP_WEB_ALLOWED_HOSTS": os.environ["ALLOWED_HOSTS_VALUE"],
        "YTDLP_WEB_ALLOW_PRIVATE_URLS": "false",
        "YTDLP_WEB_JS_RUNTIME": "deno",
    })
elif os.environ["DOMAIN_EXPLICIT_VALUE"] == "1":
    updates["YTDLP_WEB_COOKIE_SECURE"] = os.environ["COOKIE_SECURE_VALUE"]
    updates["YTDLP_WEB_ALLOWED_HOSTS"] = os.environ["ALLOWED_HOSTS_VALUE"]
if os.environ.get("GENERATED_TOKEN_VALUE"):
    updates["YTDLP_WEB_ACCESS_TOKEN"] = os.environ["GENERATED_TOKEN_VALUE"]
if os.environ.get("GENERATED_SECRET_VALUE"):
    updates["YTDLP_WEB_APP_SECRET"] = os.environ["GENERATED_SECRET_VALUE"]

lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
output = []
for line in lines:
    key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")
path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
chown root:"$SERVICE_USER" "$INSTALL_DIR/.env"
chmod 0640 "$INSTALL_DIR/.env"

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade "$INSTALL_DIR"
chown -R root:root "$INSTALL_DIR/.venv"

if [[ $SKIP_DENO -eq 0 ]]; then
  current_deno=""
  if [[ -x "$INSTALL_DIR/.deno/bin/deno" ]]; then
    current_deno="$("$INSTALL_DIR/.deno/bin/deno" --version | awk 'NR==1 {print $2}')"
  fi
  if [[ "$current_deno" != "$DENO_VERSION" ]]; then
    case "$(dpkg --print-architecture)" in
      amd64) deno_arch="x86_64" ;;
      arm64) deno_arch="aarch64" ;;
      *) echo "Unsupported Deno architecture: $(dpkg --print-architecture)" >&2; exit 1 ;;
    esac
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT
    deno_url="https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}-unknown-linux-gnu.zip"
    curl --http1.1 --fail --location --retry 5 --retry-all-errors --connect-timeout 20 --max-time 600 "$deno_url" --output "$tmp_dir/deno.zip"
    curl --http1.1 --fail --location --retry 5 --retry-all-errors --connect-timeout 20 --max-time 120 "$deno_url.sha256sum" --output "$tmp_dir/deno.zip.sha256sum"
    (cd "$tmp_dir" && echo "$(awk '{print $1}' deno.zip.sha256sum)  deno.zip" | sha256sum --check - && unzip -q deno.zip)
    install -d -o root -g root -m 0755 "$INSTALL_DIR/.deno/bin"
    install -o root -g root -m 0755 "$tmp_dir/deno" "$INSTALL_DIR/.deno/bin/deno"
    rm -rf "$tmp_dir"
    trap - EXIT
  fi
  "$INSTALL_DIR/.deno/bin/deno" --version | head -n 1
fi

sed -e "s|@@INSTALL_DIR@@|$INSTALL_DIR|g" \
    -e "s|@@SERVICE_USER@@|$SERVICE_USER|g" \
    -e "s|@@ARTIFACT_GROUP@@|$ARTIFACT_GROUP|g" \
    "$INSTALL_DIR/deploy/systemd/ytbdownload.service" \
    > /etc/systemd/system/ytbdownload.service
chmod 0644 /etc/systemd/system/ytbdownload.service

sed -e "s|@@INSTALL_DIR@@|$INSTALL_DIR|g" \
    -e "s|@@DOMAIN@@|$DOMAIN|g" \
    "$INSTALL_DIR/deploy/nginx/ytbdownload.conf" \
    > /etc/nginx/sites-available/ytbdownload
ln -sfn /etc/nginx/sites-available/ytbdownload /etc/nginx/sites-enabled/ytbdownload
rm -f /etc/nginx/sites-enabled/default

systemd-analyze verify /etc/systemd/system/ytbdownload.service
nginx -t
systemctl daemon-reload
systemctl enable ytbdownload.service >/dev/null
systemctl restart ytbdownload.service
systemctl restart nginx

healthy=0
for _attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8000/api/v1/health/ready >/dev/null; then
    healthy=1
    break
  fi
  sleep 1
done
if [[ $healthy -ne 1 ]]; then
  systemctl --no-pager --full status ytbdownload.service || true
  journalctl -u ytbdownload.service -n 80 --no-pager || true
  echo "Application health check failed." >&2
  exit 1
fi

TLS_ENABLED=0
if [[ "$DOMAIN" != "_" && -n "$EMAIL" && $SKIP_CERTBOT -eq 0 ]]; then
  certbot --nginx --non-interactive --agree-tos --redirect --keep-until-expiring \
    --email "$EMAIL" --domain "$DOMAIN"
  TLS_ENABLED=1
  sed -i 's/^YTDLP_WEB_COOKIE_SECURE=.*/YTDLP_WEB_COOKIE_SECURE=true/' "$INSTALL_DIR/.env"
  systemctl restart ytbdownload.service
fi

{
  printf 'INSTALL_DIR=%q\n' "$INSTALL_DIR"
  printf 'SERVICE_USER=%q\n' "$SERVICE_USER"
  printf 'ARTIFACT_GROUP=%q\n' "$ARTIFACT_GROUP"
  printf 'DOMAIN=%q\n' "$DOMAIN"
  printf 'EMAIL=%q\n' "$EMAIL"
  printf 'DENO_VERSION=%q\n' "$DENO_VERSION"
} > "$CONFIG_FILE"
chmod 0600 "$CONFIG_FILE"

if [[ -n "$GENERATED_TOKEN" ]]; then
  token_file="/root/ytbdownload-access-token.txt"
  printf '%s\n' "$GENERATED_TOKEN" > "$token_file"
  chmod 0600 "$token_file"
  echo "Generated access token saved to: $token_file"
fi

if [[ $TLS_ENABLED -eq 1 ]]; then
  public_url="https://$DOMAIN"
elif [[ "$DOMAIN" != "_" ]]; then
  public_url="http://$DOMAIN"
else
  public_url="http://SERVER_IP"
fi

cat <<EOF

Signal / yt-dlp Web installation complete.
  URL:        $public_url
  Runtime:    $INSTALL_DIR
  Config:     $INSTALL_DIR/.env
  Service:    systemctl status ytbdownload
  Logs:       journalctl -u ytbdownload -f
  Health:     curl http://127.0.0.1:8000/api/v1/health/ready
EOF

if [[ "$DOMAIN" != "_" && $TLS_ENABLED -eq 0 ]]; then
  echo "TLS was not configured. Run Certbot, then set YTDLP_WEB_COOKIE_SECURE=true."
fi
