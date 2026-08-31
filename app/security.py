from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fastapi import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.config import Settings
from app.errors import AppError

SESSION_COOKIE = "signal_session"


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    addresses: tuple[str, ...]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()


class URLValidator:
    """Validate the user-controlled entry URL before yt-dlp sees it.

    This protects the first connection. Deployments exposed to untrusted users should
    additionally enforce an egress firewall or filtering proxy because extractors can
    follow redirects and discover more URLs.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate_syntax(self, raw_url: str) -> tuple[str, str, int]:
        if not isinstance(raw_url, str):
            raise AppError("INVALID_URL", "链接必须是字符串。")
        raw_url = raw_url.strip()
        if not raw_url or len(raw_url) > self.settings.max_url_length:
            raise AppError(
                "INVALID_URL",
                f"链接长度必须在 1 到 {self.settings.max_url_length} 个字符之间。",
            )
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
            raise AppError("INVALID_URL", "链接中包含无效控制字符。")

        try:
            parsed = urlsplit(raw_url)
            port = parsed.port
        except ValueError as exc:
            raise AppError("INVALID_URL", "链接格式无效。") from exc

        if parsed.scheme.lower() not in {"http", "https"}:
            raise AppError("UNSAFE_URL", "仅支持 http:// 或 https:// 链接。")
        if parsed.username is not None or parsed.password is not None:
            raise AppError("UNSAFE_URL", "链接不能包含用户名或密码。")
        if not parsed.hostname:
            raise AppError("INVALID_URL", "链接缺少有效域名。")

        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise AppError("INVALID_URL", "链接域名无效。") from exc
        if not hostname or len(hostname) > 253:
            raise AppError("INVALID_URL", "链接域名无效。")
        if (
            hostname == "localhost" or hostname.endswith((".localhost", ".local"))
        ) and not self.settings.allow_private_urls:
            raise AppError("PRIVATE_ADDRESS", "不允许访问本机或内网地址。")

        default_port = 443 if parsed.scheme.lower() == "https" else 80
        selected_port = port or default_port
        if (
            not self.settings.allow_private_urls
            and selected_port not in self.settings.allowed_ports
        ):
            raise AppError(
                "UNSAFE_PORT",
                f"出于安全考虑，仅允许端口：{', '.join(map(str, sorted(self.settings.allowed_ports)))}。",
            )

        normalized_netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            normalized_netloc = f"[{hostname}]"
        if port is not None and port != default_port:
            normalized_netloc = f"{normalized_netloc}:{port}"
        normalized = urlunsplit(
            SplitResult(
                parsed.scheme.lower(),
                normalized_netloc,
                parsed.path or "/",
                parsed.query,
                parsed.fragment,
            )
        )
        return normalized, hostname, selected_port

    def validate_sync(self, raw_url: str) -> ValidatedURL:
        normalized, hostname, port = self.validate_syntax(raw_url)
        try:
            records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise AppError("URL_DNS_ERROR", "无法解析该链接的域名。") from exc
        addresses = sorted({str(record[4][0]).split("%")[0] for record in records})
        if not addresses:
            raise AppError("URL_DNS_ERROR", "该域名没有可用的网络地址。")
        if not self.settings.allow_private_urls:
            for raw_address in addresses:
                try:
                    address = ipaddress.ip_address(raw_address)
                except ValueError as exc:
                    raise AppError("UNSAFE_URL", "域名解析到了无效地址。") from exc
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                    address = address.ipv4_mapped
                if not address.is_global:
                    raise AppError("PRIVATE_ADDRESS", "不允许访问本机、内网或保留网络地址。")
        return ValidatedURL(normalized, hostname, tuple(addresses))

    async def validate(self, raw_url: str) -> ValidatedURL:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.validate_sync, raw_url),
                timeout=self.settings.dns_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AppError(
                "URL_DNS_TIMEOUT", "域名解析超时，请稍后重试。", status_code=504
            ) from exc


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.key = settings.session_signing_key
        principal_source = settings.access_token or "public"
        self.principal = hashlib.sha256(f"principal:{principal_source}".encode()).hexdigest()[:24]

    def validate_access_token(self, candidate: str) -> bool:
        if not self.settings.auth_enabled:
            return True
        return hmac.compare_digest(candidate.encode(), self.settings.access_token.encode())

    def issue(self) -> str:
        payload = {
            "exp": int(time.time()) + self.settings.session_ttl_hours * 3600,
            "nonce": secrets.token_urlsafe(12),
            "v": 1,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = hmac.new(self.key, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str | None) -> bool:
        if not self.settings.auth_enabled:
            return True
        if not token or "." not in token:
            return False
        encoded_text, signature_text = token.split(".", 1)
        try:
            encoded = encoded_text.encode()
            signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
            canonical_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
            if not hmac.compare_digest(signature_text, canonical_signature):
                return False
            expected = hmac.new(self.key, encoded, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload_raw = base64.urlsafe_b64decode(encoded_text + "=" * (-len(encoded_text) % 4))
            payload = json.loads(payload_raw)
            return payload.get("v") == 1 and int(payload.get("exp", 0)) >= int(time.time())
        except (ValueError, TypeError, json.JSONDecodeError):
            return False

    def request_authenticated(self, request: Request) -> bool:
        if not self.settings.auth_enabled:
            return True
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return self.validate_access_token(authorization[7:].strip())
        return self.verify(request.cookies.get(SESSION_COOKIE))


async def require_principal(request: Request) -> str:
    manager: SessionManager = request.app.state.session_manager
    if not manager.request_authenticated(request):
        raise AppError("AUTH_REQUIRED", "请先输入访问令牌。", status_code=401)
    return manager.principal


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, bucket: str, *, limit: int, window: int) -> int:
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            events = self._events[(key, bucket)]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry = max(1, int(window - (now - events[0])) + 1)
                raise AppError(
                    "RATE_LIMITED",
                    "请求过于频繁，请稍后再试。",
                    status_code=429,
                    details={"retry_after": retry},
                )
            events.append(now)
            return max(0, limit - len(events))


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                additions = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"cross-origin-resource-policy": b"same-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                    b"content-security-policy": (
                        b"default-src 'self'; base-uri 'self'; form-action 'self'; "
                        b"frame-ancestors 'none'; object-src 'none'; script-src 'self'; "
                        b"style-src 'self'; img-src 'self' https: http: data: blob:; "
                        b"media-src 'self' blob:; connect-src 'self'; font-src 'self'"
                    ),
                }
                if self.hsts:
                    additions[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                existing = {key.lower() for key, _ in headers}
                headers.extend(
                    (key, value) for key, value in additions.items() if key not in existing
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def get_client_key(request: Request, settings: Settings) -> str:
    if settings.trusted_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Trust only the hop appended by the directly connected reverse proxy.
            return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else "unknown"


def validate_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise AppError("INVALID_ORIGIN", "无效请求来源。", status_code=403) from exc
    if parsed.netloc.lower() != request.url.netloc.lower():
        raise AppError("INVALID_ORIGIN", "拒绝跨站请求。", status_code=403)


def set_session_cookie(response: Response, manager: SessionManager) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        manager.issue(),
        max_age=manager.settings.session_ttl_hours * 3600,
        httponly=True,
        secure=manager.settings.cookie_secure,
        samesite="strict",
        path="/",
    )
