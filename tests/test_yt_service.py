from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest
from conftest import sample_info

from app.config import Settings
from app.database import Database
from app.errors import AppError
from app.security import ValidatedURL
from app.yt_service import (
    YtDlpService,
    build_choices,
    normalize_formats,
    redact_upstream_message,
    video_selector,
)


class FakeYDL:
    payload: ClassVar[dict] = {}

    def __init__(self, options: dict) -> None:
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url: str, *, download: bool):
        assert download is False
        return self.payload


def test_analysis_returns_allowlisted_metadata_only(
    settings: Settings, db: Database, monkeypatch
) -> None:
    service = YtDlpService(settings, db)
    service.url_validator.validate_sync = lambda url: ValidatedURL(url, "example.com", ("8.8.8.8",))
    FakeYDL.payload = sample_info(
        url="https://cdn.example.test/top-secret",
        http_headers={"Authorization": "Bearer secret"},
    )
    monkeypatch.setattr("app.yt_service.yt_dlp.YoutubeDL", FakeYDL)

    result = service.analyze("https://example.com/video", playlist=False)
    encoded = json.dumps(result, ensure_ascii=False)
    assert result["title"] == "测试 / Media"
    assert result["platform"] == "YouTube"
    assert result["thumbnail"]["url"].startswith("https://")
    assert "top-secret" not in encoded
    assert "private-video-url" not in encoded
    assert "Authorization" not in encoded
    assert any(choice["kind"] == "video" for choice in result["choices"])
    assert any(choice.get("codec") == "mp3" for choice in result["choices"])
    assert {item["code"] for item in result["subtitles"]} == {"zh-Hans", "en"}


def test_analysis_restricts_live_media_but_keeps_thumbnail(
    settings: Settings, db: Database
) -> None:
    service = YtDlpService(settings, db)
    result = service._normalize_single(sample_info(is_live=True, live_status="is_live"))
    assert result["restriction"]["code"] == "LIVE_NOT_SUPPORTED"
    assert {choice["kind"] for choice in result["choices"]} == {"thumbnail"}


def test_playlist_limit_is_explicit(settings: Settings, db: Database) -> None:
    limited = settings.model_copy(update={"max_playlist_items": 2})
    service = YtDlpService(limited, db)
    payload = {
        "_type": "playlist",
        "id": "list",
        "title": "Too long",
        "extractor_key": "YouTubePlaylist",
        "entries": [
            {"id": "1", "title": "one"},
            {"id": "2", "title": "two"},
            {"id": "3", "title": "three"},
        ],
    }
    with pytest.raises(AppError) as caught:
        service._normalize_playlist(payload)
    assert caught.value.code == "PLAYLIST_LIMIT"
    assert caught.value.details["limit"] == 2


def test_format_policy_filters_drm_and_never_accepts_expression() -> None:
    formats = normalize_formats(
        [
            {"format_id": "good", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080},
            {"format_id": "drm", "ext": "mp4", "vcodec": "avc1", "acodec": "aac", "has_drm": True},
            {"format_id": "bad+selector", "ext": "mp4", "vcodec": "avc1", "acodec": "aac"},
            {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
        ],
        120,
    )
    assert {item["format_id"] for item in formats} == {"good", "audio"}
    choices = build_choices(formats, has_thumbnail=False, playlist=False, restricted=False)
    exact = next(choice for choice in choices if choice.get("format_id") == "good")
    assert video_selector(exact) == "good+ba/good"
    with pytest.raises(AppError):
        video_selector({"policy": "exact", "format_id": "best;rm -rf /", "needs_audio": False})


def test_redaction_removes_urls_and_credentials() -> None:
    message = (
        "failed https://cdn.example/video?token=abc&signature=xyz "
        "for https://input.example/watch?v=private"
    )
    redacted = redact_upstream_message(message, "https://input.example/watch?v=private")
    assert "abc" not in redacted
    assert "private" not in redacted
    assert "https://" not in redacted


def test_artifact_finalization_uses_isolated_fixed_paths(settings: Settings, db: Database) -> None:
    service = YtDlpService(settings, db)
    analysis = db.create_analysis(
        owner="owner",
        request_url="https://example.com/video",
        url_hash="d" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    db.claim_next_operation()
    choice = {
        "id": "7dcd5d0d-8867-4544-985b-8833f0ac1634",
        "kind": "video",
        "policy": "best",
        "label": "Best",
    }
    db.complete_analysis(
        analysis["id"],
        {"id": "x", "extractor": "Generic", "title": "../../危险标题", "choices": [choice]},
    )
    job, _ = db.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="../../危险标题",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    db.claim_next_operation()
    work = settings.work_dir / job["id"]
    work.mkdir()
    (work / "media.mp4").write_bytes(b"video-data")
    (work / "media.zh.vtt").write_text("WEBVTT", encoding="utf-8")
    artifact_dir = settings.artifacts_dir / job["id"]

    records = service._finalize_artifacts(
        job["id"],
        work,
        artifact_dir,
        title="../../危险标题",
        choice=choice,
        playlist=False,
    )
    assert len(records) == 2
    assert all(Path(record["relpath"]).parts[0] == job["id"] for record in records)
    assert all(".." not in record["filename"] for record in records)
    primary = next(record for record in records if record["primary"])
    assert primary["media_type"] == "video/mp4"
    assert not work.exists()


def test_playlist_artifacts_include_primary_zip(settings: Settings, db: Database) -> None:
    service = YtDlpService(settings, db)
    analysis = db.create_analysis(
        owner="owner",
        request_url="https://example.com/list",
        url_hash="e" * 64,
        playlist=True,
        ttl_minutes=60,
    )
    db.claim_next_operation()
    choice = {
        "id": "e5e10b20-6856-4669-8919-1f85bb4da344",
        "kind": "video",
        "policy": "best",
        "label": "Playlist",
    }
    db.complete_analysis(
        analysis["id"],
        {"id": "list", "extractor": "Generic", "title": "Collection", "choices": [choice]},
    )
    job, _ = db.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="Collection",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    db.claim_next_operation()
    work = settings.work_dir / job["id"]
    work.mkdir()
    (work / "item-001.mp4").write_bytes(b"first")
    (work / "item-002.mp4").write_bytes(b"second")
    records = service._finalize_artifacts(
        job["id"],
        work,
        settings.artifacts_dir / job["id"],
        title="Collection",
        choice=choice,
        playlist=True,
    )
    primary = next(record for record in records if record["primary"])
    archive_path = settings.artifacts_dir / primary["relpath"]
    assert archive_path.suffix == ".zip"
    assert len(records) == 3
    with zipfile.ZipFile(archive_path) as archive:
        assert len(archive.namelist()) == 2
