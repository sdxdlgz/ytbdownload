# Security Policy

## Supported version

Security fixes are applied to the latest `main` branch. Keep yt-dlp and the container/system packages updated because upstream media sites and networking dependencies change frequently.

## Reporting a vulnerability

Please use GitHub's **Private vulnerability reporting / Security advisory** feature for this repository. Do not open a public issue containing exploit details, access tokens, cookies, signed media URLs, server IPs, or logs with account data.

Include:

- affected commit/version and deployment mode
- minimal reproduction steps
- expected impact
- whether a private/loopback/cloud-metadata request was observed
- sanitized logs (remove URLs, queries, cookies, tokens, signatures, and usernames)

## Deployment boundary

This application performs server-side network extraction and runs ffmpeg on untrusted media. Public operators must use HTTPS, a strong access token, conservative resource limits, and egress filtering. Initial URL validation alone cannot prevent every redirect/DNS-rebinding SSRF path. See [`docs/deployment.md`](docs/deployment.md#4-安全加固).
