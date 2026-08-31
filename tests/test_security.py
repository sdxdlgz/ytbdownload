from __future__ import annotations

import socket
from pathlib import Path

import pytest
from starlette.requests import Request

from app.config import Settings
from app.errors import AppError
from app.security import (
    SessionManager,
    SlidingWindowRateLimiter,
    URLValidator,
    get_client_key,
)


def strict_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "access_token": "",
        "app_secret": "pytest-security-secret",
        "environment": "test",
        "data_dir": tmp_path / "strict-data",
        "allow_private_urls": False,
        "js_runtime": "",
        "min_free_disk_mb": 32,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("file:///etc/passwd", "UNSAFE_URL"),
        ("ftp://example.com/file", "UNSAFE_URL"),
        ("https://user:pass@example.com/video", "UNSAFE_URL"),
        ("http://localhost/video", "PRIVATE_ADDRESS"),
        ("http://service.local/video", "PRIVATE_ADDRESS"),
        ("https://example.com:8443/video", "UNSAFE_PORT"),
        ("https://example.com/\nvideo", "INVALID_URL"),
    ],
)
def test_url_validator_rejects_unsafe_syntax(tmp_path: Path, url: str, code: str) -> None:
    validator = URLValidator(strict_settings(tmp_path))
    with pytest.raises(AppError) as caught:
        validator.validate_syntax(url)
    assert caught.value.code == code


def test_url_validator_normalizes_idn_and_default_path(tmp_path: Path) -> None:
    validator = URLValidator(strict_settings(tmp_path))
    normalized, hostname, port = validator.validate_syntax("HTTPS://例子.测试")
    assert normalized == "https://xn--fsqu00a.xn--0zwm56d/"
    assert hostname == "xn--fsqu00a.xn--0zwm56d"
    assert port == 443


def test_url_validator_rejects_private_dns_result(tmp_path: Path, monkeypatch) -> None:
    validator = URLValidator(strict_settings(tmp_path))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(AppError) as caught:
        validator.validate_sync("https://public.example/video")
    assert caught.value.code == "PRIVATE_ADDRESS"


def test_url_validator_accepts_global_dns_result(tmp_path: Path, monkeypatch) -> None:
    validator = URLValidator(strict_settings(tmp_path))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    result = validator.validate_sync("https://PUBLIC.example/watch?v=1")
    assert result.hostname == "public.example"
    assert result.addresses == ("8.8.8.8",)
    assert len(result.digest) == 64


def test_session_cookie_signing_and_tamper_detection(tmp_path: Path) -> None:
    manager = SessionManager(
        strict_settings(tmp_path, access_token="correct horse battery staple", app_secret="secret")
    )
    cookie = manager.issue()
    assert manager.verify(cookie)
    assert not manager.verify(cookie[:-1] + ("A" if cookie[-1] != "A" else "B"))
    assert manager.validate_access_token("correct horse battery staple")
    assert not manager.validate_access_token("wrong")


def test_rate_limiter_returns_retry_after() -> None:
    limiter = SlidingWindowRateLimiter()
    limiter.check("client", "api", limit=2, window=60)
    limiter.check("client", "api", limit=2, window=60)
    with pytest.raises(AppError) as caught:
        limiter.check("client", "api", limit=2, window=60)
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.status_code == 429
    assert caught.value.details["retry_after"] >= 1


def test_trusted_proxy_uses_only_rightmost_forwarded_hop(tmp_path: Path) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"spoofed, 198.51.100.24")],
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    trusted = strict_settings(tmp_path, trusted_proxy=True)
    assert get_client_key(request, trusted) == "198.51.100.24"
    assert (
        get_client_key(request, trusted.model_copy(update={"trusted_proxy": False})) == "127.0.0.1"
    )


@pytest.mark.parametrize(
    ("token", "secret"),
    [
        ("CHANGE_ME_LONG_RANDOM_TOKEN", "safe-secret"),
        ("a-secure-production-token-123456", "CHANGE_ME_SECRET"),
        ("too-short", "safe-secret"),
    ],
)
def test_production_rejects_placeholder_or_weak_tokens(
    tmp_path: Path, token: str, secret: str
) -> None:
    with pytest.raises(ValueError):
        Settings(
            _env_file=None,
            environment="production",
            data_dir=tmp_path / "production",
            access_token=token,
            app_secret=secret,
        )
