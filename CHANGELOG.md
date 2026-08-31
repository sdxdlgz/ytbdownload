# Changelog

All notable changes to this project are documented here.

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
