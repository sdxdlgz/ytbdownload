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

S3 endpoint/bucket/credentials are operator-only configuration. A malicious endpoint receives signed credential-bearing requests; production endpoints should use HTTPS, and insecure MinIO must be explicitly enabled only on a trusted network.

Artifact direct links are reusable bearer capabilities. Keep TTL short, never place them in public issues/chat logs, suppress `/d/*` access logging, and rotate `YTDLP_WEB_DIRECT_LINK_SECRET`/`APP_SECRET` if they may be exposed. Rotating the secret immediately invalidates all issued app direct links; already-issued provider presigned URLs remain valid until their shorter expiry.

S3 delivery uses a short-lived `307 Location`; the authorized client necessarily sees the presigned endpoint, bucket/path and signature. These transient delivery details are never included in job/config JSON, but remain bearer secrets until expiry.

For S3, keep buckets private, grant only the configured prefix, enable lifecycle cleanup for expired/noncurrent/incomplete multipart objects, and monitor the durable deletion outbox reported by readiness health.
