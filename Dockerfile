# syntax=docker/dockerfile:1.7

ARG DENO_VERSION=2.9.6

FROM debian:bookworm-slim AS deno
ARG DENO_VERSION
ARG TARGETARCH
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH:-amd64}" in \
      amd64) deno_arch="x86_64" ;; \
      arm64) deno_arch="aarch64" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${deno_arch}-unknown-linux-gnu.zip"; \
    curl --http1.1 --fail --location --retry 5 --retry-all-errors --connect-timeout 20 --max-time 600 "$base" --output /tmp/deno.zip; \
    curl --http1.1 --fail --location --retry 5 --retry-all-errors --connect-timeout 20 --max-time 120 "$base.sha256sum" --output /tmp/deno.zip.sha256sum; \
    cd /tmp; \
    echo "$(awk '{print $1}' deno.zip.sha256sum)  deno.zip" | sha256sum --check -; \
    unzip deno.zip; \
    install -m 0755 deno /usr/local/bin/deno; \
    /usr/local/bin/deno --version

FROM python:3.12-slim-bookworm AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip setuptools wheel
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN /opt/venv/bin/pip install .

FROM python:3.12-slim-bookworm AS runtime
ARG APP_VERSION=1.0.0
LABEL org.opencontainers.image.title="Signal yt-dlp Web" \
      org.opencontainers.image.description="Secure self-hosted web interface for yt-dlp" \
      org.opencontainers.image.source="https://github.com/sdxdlgz/ytbdownload" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:/usr/local/bin:${PATH}" \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    YTDLP_WEB_ENVIRONMENT=production \
    YTDLP_WEB_HOST=0.0.0.0 \
    YTDLP_WEB_PORT=8000 \
    YTDLP_WEB_DATA_DIR=/data \
    YTDLP_WEB_JS_RUNTIME=deno

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg tini; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 10001 signal; \
    useradd --uid 10001 --gid signal --no-create-home --home-dir /tmp --shell /usr/sbin/nologin signal; \
    install -d -o signal -g signal -m 0750 /data /tmp/.cache

COPY --from=builder /opt/venv /opt/venv
COPY --from=deno /usr/local/bin/deno /usr/local/bin/deno

WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=8s --start-period=25s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=5).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
