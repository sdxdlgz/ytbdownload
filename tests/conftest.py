from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.database import Database

# Keep tests hermetic even when a developer has followed README and created .env.
os.environ["YTDLP_WEB_ENVIRONMENT"] = "test"
os.environ["YTDLP_WEB_DATA_DIR"] = f"/tmp/ytbdownload-pytest-{os.getpid()}"
os.environ["YTDLP_WEB_ACCESS_TOKEN"] = ""
os.environ["YTDLP_WEB_APP_SECRET"] = "pytest-bootstrap-secret"
os.environ["YTDLP_WEB_COOKIE_SECURE"] = "false"
os.environ["YTDLP_WEB_JS_RUNTIME"] = ""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    configured = Settings(
        _env_file=None,
        access_token="",
        app_secret="pytest-fixture-secret",
        environment="test",
        data_dir=tmp_path / "data",
        allow_private_urls=True,
        js_runtime="",
        cookie_secure=False,
        min_free_disk_mb=32,
        max_filesize_mb=100,
        max_storage_mb=512,
        max_concurrent_operations=1,
        cleanup_interval_seconds=3600,
    )
    configured.ensure_directories()
    return configured


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.database_path)
    database.initialize()
    yield database


def sample_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": "video-123",
        "title": "测试 / Media",
        "extractor_key": "YouTube",
        "uploader": "Creator",
        "duration": 125,
        "thumbnail": "https://i.example.test/cover.jpg?token=secret",
        "webpage_url": "https://www.youtube.com/watch?v=video-123",
        "formats": [
            {
                "format_id": "137",
                "ext": "mp4",
                "height": 1080,
                "width": 1920,
                "fps": 30,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 5_000_000,
                "url": "https://cdn.example.test/private-video-url",
                "http_headers": {"Cookie": "secret"},
            },
            {
                "format_id": "22",
                "ext": "mp4",
                "height": 720,
                "width": 1280,
                "fps": 30,
                "vcodec": "avc1.4d401f",
                "acodec": "mp4a.40.2",
                "filesize": 3_000_000,
            },
            {
                "format_id": "140",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "filesize": 500_000,
            },
        ],
        "subtitles": {"zh-Hans": [{"name": "简体中文", "url": "https://secret"}]},
        "automatic_captions": {"en": [{"name": "English", "url": "https://secret"}]},
    }
    info.update(overrides)
    return info
