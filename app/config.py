from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from ``YTDLP_WEB_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="YTDLP_WEB_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Signal / yt-dlp Web"
    environment: str = "production"
    # Public container default; native systemd explicitly binds 127.0.0.1.
    host: str = "0.0.0.0"  # nosec B104
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "info"
    data_dir: Path = Path("data")

    access_token: str = ""
    app_secret: str = ""
    cookie_secure: bool = False
    session_ttl_hours: int = Field(default=168, ge=1, le=8760)
    trusted_proxy: bool = False
    allowed_hosts: str = "*"

    allow_private_urls: bool = False
    allowed_url_ports: str = "80,443"
    max_url_length: int = Field(default=2048, ge=256, le=16384)
    dns_timeout_seconds: float = Field(default=8.0, ge=0.5, le=60.0)

    max_concurrent_operations: int = Field(default=2, ge=1, le=16)
    max_queued_operations: int = Field(default=20, ge=1, le=1000)
    analysis_timeout_seconds: int = Field(default=120, ge=15, le=1800)
    download_timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    cancel_grace_seconds: int = Field(default=8, ge=1, le=120)
    worker_poll_seconds: float = Field(default=0.5, ge=0.1, le=10.0)

    max_filesize_mb: int = Field(default=2048, ge=10, le=102400)
    max_duration_seconds: int = Field(default=14400, ge=60, le=604800)
    max_playlist_items: int = Field(default=20, ge=1, le=500)
    max_formats: int = Field(default=120, ge=10, le=500)
    min_free_disk_mb: int = Field(default=512, ge=32, le=102400)
    max_storage_mb: int = Field(default=10240, ge=256, le=1048576)
    artifact_ttl_hours: int = Field(default=12, ge=1, le=8760)
    analysis_ttl_minutes: int = Field(default=60, ge=5, le=10080)
    cleanup_interval_seconds: int = Field(default=900, ge=30, le=86400)

    rate_limit_requests: int = Field(default=120, ge=10, le=100000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    analysis_rate_limit: int = Field(default=12, ge=1, le=10000)
    job_rate_limit: int = Field(default=20, ge=1, le=10000)

    socket_timeout_seconds: int = Field(default=20, ge=5, le=300)
    extractor_retries: int = Field(default=2, ge=0, le=20)
    download_retries: int = Field(default=5, ge=0, le=50)
    fragment_retries: int = Field(default=5, ge=0, le=50)
    concurrent_fragments: int = Field(default=4, ge=1, le=16)
    cookies_file: Path | None = None
    proxy: str = ""
    js_runtime: str = "deno"
    impersonate: str = ""

    x_accel_redirect: bool = False
    x_accel_prefix: str = "/_protected_downloads"

    direct_links_enabled: bool = True
    direct_link_ttl_minutes: int = Field(default=120, ge=1, le=10080)
    direct_link_max_ttl_minutes: int = Field(default=1440, ge=1, le=10080)
    direct_link_secret: SecretStr | None = None

    s3_enabled: bool = False
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""
    s3_allow_insecure_endpoint: bool = False
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_session_token: SecretStr | None = None
    s3_prefix: str = "signal-artifacts"
    s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    s3_keep_local: bool = True
    s3_failure_mode: Literal["fallback", "required"] = "fallback"
    s3_delete_on_expiry: bool = True
    s3_presign_ttl_seconds: int = Field(default=7200, ge=60, le=604800)
    s3_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    s3_read_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    s3_retry_attempts: int = Field(default=4, ge=1, le=20)
    s3_multipart_threshold_mb: int = Field(default=64, ge=5, le=5120)
    s3_multipart_chunksize_mb: int = Field(default=16, ge=5, le=5120)
    s3_max_concurrency: int = Field(default=2, ge=1, le=16)
    s3_server_side_encryption: Literal["", "AES256", "aws:kms"] = ""
    s3_kms_key_id: SecretStr | None = None
    s3_storage_class: str = ""
    s3_verify_tls: bool = True
    s3_ca_bundle: Path | None = None
    s3_healthcheck: bool = False

    @field_validator("allowed_url_ports")
    @classmethod
    def validate_allowed_url_ports(cls, value: str) -> str:
        ports: list[str] = []
        for raw in value.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                port = int(raw)
            except ValueError as exc:
                raise ValueError("allowed_url_ports must contain integers") from exc
            if not 1 <= port <= 65535:
                raise ValueError("allowed URL ports must be between 1 and 65535")
            normalized = str(port)
            if normalized not in ports:
                ports.append(normalized)
        if not ports:
            raise ValueError("at least one allowed URL port is required")
        return ",".join(ports)

    @field_validator("s3_prefix")
    @classmethod
    def normalize_s3_prefix(cls, value: str) -> str:
        value = value.strip().strip("/")
        if len(value) > 180 or ".." in value or "\\" in value:
            raise ValueError("s3_prefix must be a safe relative object prefix")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("s3_prefix contains control characters")
        return value

    @field_validator("s3_storage_class")
    @classmethod
    def normalize_s3_storage_class(cls, value: str) -> str:
        value = value.strip().upper()
        if value and not all(char.isalnum() or char in {"_", "-"} for char in value):
            raise ValueError("s3_storage_class contains invalid characters")
        return value

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"development", "test", "production"}:
            raise ValueError("environment must be development, test, or production")
        return value

    @field_validator("js_runtime")
    @classmethod
    def validate_js_runtime(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"", "deno", "node", "bun", "quickjs"}:
            raise ValueError("js_runtime must be deno, node, bun, quickjs, or empty")
        return value

    @model_validator(mode="after")
    def validate_cross_field_configuration(self) -> Settings:
        if self.direct_link_ttl_minutes > self.direct_link_max_ttl_minutes:
            raise ValueError("direct_link_ttl_minutes cannot exceed direct_link_max_ttl_minutes")

        if self.s3_enabled:
            if not self.s3_bucket.strip():
                raise ValueError("s3_bucket is required when S3 storage is enabled")
            if any(ord(char) < 32 or char.isspace() for char in self.s3_bucket):
                raise ValueError("s3_bucket contains invalid characters")
            has_key = bool(self.s3_access_key_id and self.s3_access_key_id.get_secret_value())
            has_secret = bool(
                self.s3_secret_access_key and self.s3_secret_access_key.get_secret_value()
            )
            if has_key != has_secret:
                raise ValueError("S3 access key and secret key must be provided together")
            has_session_token = bool(
                self.s3_session_token and self.s3_session_token.get_secret_value()
            )
            if has_session_token and not (has_key and has_secret):
                raise ValueError("S3 session token requires an access key and secret key")
            if self.s3_endpoint_url:
                parsed = urlsplit(self.s3_endpoint_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError("s3_endpoint_url must be an absolute HTTP(S) URL")
                if parsed.username or parsed.password:
                    raise ValueError("s3_endpoint_url cannot contain credentials")
                if parsed.scheme == "http" and not self.s3_allow_insecure_endpoint:
                    raise ValueError("HTTP S3 endpoints require s3_allow_insecure_endpoint=true")
            kms_key = self.s3_kms_key_id.get_secret_value() if self.s3_kms_key_id else ""
            if self.s3_server_side_encryption == "aws:kms" and not kms_key:
                raise ValueError("s3_kms_key_id is required for aws:kms encryption")
            if self.s3_ca_bundle and not self.s3_ca_bundle.is_file():
                raise ValueError("configured s3_ca_bundle does not exist")
        elif self.s3_failure_mode == "required" or not self.s3_keep_local:
            raise ValueError("required/remote-only artifact storage requires s3_enabled=true")

        if self.environment != "production":
            return self
        placeholders = ("CHANGE_ME", "REPLACE_ME", "CHANGEME")
        if self.access_token.upper().startswith(placeholders):
            raise ValueError("replace the placeholder YTDLP_WEB_ACCESS_TOKEN before production")
        if self.app_secret.upper().startswith(placeholders):
            raise ValueError("replace the placeholder YTDLP_WEB_APP_SECRET before production")
        if self.access_token and len(self.access_token) < 24:
            raise ValueError("production access_token must contain at least 24 characters")
        if self.direct_links_enabled:
            direct_override = (
                self.direct_link_secret.get_secret_value() if self.direct_link_secret else ""
            )
            direct_secret = direct_override or self.app_secret
            if direct_secret.upper().startswith(placeholders) or len(direct_secret) < 32:
                raise ValueError(
                    "production direct links require a non-placeholder 32+ character "
                    "direct_link_secret or app_secret"
                )
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def max_filesize_bytes(self) -> int:
        return self.max_filesize_mb * 1024 * 1024

    @property
    def min_free_disk_bytes(self) -> int:
        return self.min_free_disk_mb * 1024 * 1024

    @property
    def max_storage_bytes(self) -> int:
        return self.max_storage_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        return bool(self.access_token)

    @property
    def session_signing_key(self) -> bytes:
        source = self.app_secret or self.access_token or os.environ.get("HOSTNAME", "yt-dlp-web")
        return hashlib.sha256(f"signal-session-v1:{source}".encode()).digest()

    @property
    def direct_link_signing_key(self) -> bytes:
        override = self.direct_link_secret.get_secret_value() if self.direct_link_secret else ""
        source = (
            override
            or self.app_secret
            or self.access_token
            or os.environ.get("HOSTNAME", "yt-dlp-web")
        )
        return hashlib.sha256(f"signal-artifact-direct-v1:{source}".encode()).digest()

    @property
    def s3_multipart_threshold_bytes(self) -> int:
        return self.s3_multipart_threshold_mb * 1024 * 1024

    @property
    def s3_multipart_chunksize_bytes(self) -> int:
        return self.s3_multipart_chunksize_mb * 1024 * 1024

    @property
    def allowed_hosts_list(self) -> list[str]:
        values = [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]
        return values or ["*"]

    @property
    def allowed_ports(self) -> set[int]:
        ports: set[int] = set()
        for raw in self.allowed_url_ports.split(","):
            raw = raw.strip()
            if raw:
                ports.add(int(raw))
        return ports

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.work_dir, self.artifacts_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
