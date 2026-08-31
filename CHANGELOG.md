# Changelog

All notable changes to this project are documented here.

## [1.1.0] - 2026-08-31

### Added

- Optional transactional AWS S3/S3-compatible artifact publishing for R2, B2, Wasabi and MinIO, including multipart upload, HEAD verification, SSE/KMS options, local retention/fallback policy and provider metadata.
- Backward-compatible SQLite artifact migration with staged upload state, remote version metadata, local-copy awareness and a durable deletion outbox with leases and exponential retry.
- Short-lived HMAC artifact capabilities that require no session Cookie, support GET/HEAD/Range, immediately revoke on purge and redirect S3 artifacts with method-specific presigned URLs.
- LOCAL/S3 badges and accessible copy-direct-link controls in active transfers and history, including clipboard fallback and expiry feedback.
- Real pinned-MinIO integration test covering PUT, HEAD verification, presigned GET/HEAD/Range and deletion.

### Changed

- Version bumped to 1.1.0; health/config/job metadata report delivery capabilities without persisting or serializing bucket, key, endpoint or credentials. Authorized delivery still uses a short-lived presigned `Location` by design.
- Production reverse proxies suppress `/d/*` bearer URLs from access logs.

### Fixed

- Hide staged artifacts until a job is fully completed and mark stable same-origin links as downloads, preventing premature 404/canceled S3 browser downloads during final upload.
- Suppress Uvicorn access logs on every documented/runtime entry point and hide Pydantic validation inputs so direct-link bearer queries and S3 secrets are not echoed to logs.
- Avoid taking SQLite's writer lock during idle dispatcher polls, preventing cancellation and queue operations from starving under cross-process progress writes.


## [1.0.0] - 2026-08-31

### Added

- FastAPI media analysis API backed by yt-dlp with safe normalized metadata and server-generated format choices.
- Video merge presets and exact formats, MP3/M4A/Opus extraction, subtitles, metadata, thumbnails, and bounded playlist ZIP output.
- SQLite WAL task persistence, supervised worker process groups, progress, hard cancellation, restart reconciliation, artifact TTL and storage cleanup.
- Responsive SIGNAL browser interface with drag/paste input, format tabs, progress monitor, downloads, history, private-token dialog, keyboard navigation, and mobile layout.
- Optional HMAC-signed HttpOnly access sessions, Origin/Host checks, rate limits, URL/DNS safety checks, resource limits, and security headers.
- Docker image with ffmpeg, Deno, yt-dlp-ejs, non-root/read-only runtime; Compose and optional Caddy automatic HTTPS stack.
- Native Debian installer with systemd sandbox, Nginx X-Accel artifact delivery, separate artifact-reader group, Certbot, SSH-only updater, and optional nftables egress policy.
- Unit, API, security, real yt-dlp/ffmpeg, process cancellation, Playwright, container, Nginx and deployment validation workflows.
- Chinese README, detailed Debian deployment/security guide, security policy, MIT license, CI and Dependabot configuration.

### Fixed

- Browser test launchers now use the active CI Python when `.venv` is absent, while retaining local virtualenv auto-detection and a longer startup allowance.
