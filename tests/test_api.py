from __future__ import annotations

from pathlib import Path

from conftest import sample_info
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import ValidatedURL
from app.yt_service import YtDlpService


def app_client(settings: Settings):
    app = create_app(settings)

    async def noop() -> None:
        return None

    app.state.dispatcher.start = noop
    app.state.dispatcher.stop = noop
    return app, TestClient(app)


def test_health_config_and_security_headers(settings: Settings) -> None:
    app, client_context = app_client(settings)
    with client_context as client:
        live = client.get("/api/v1/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "ok"
        assert live.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in live.headers["content-security-policy"]

        config = client.get("/api/v1/config")
        assert config.status_code == 200
        payload = config.json()
        assert payload["features"]["thumbnail"] == ["original", "jpg", "png"]
        assert payload["capabilities"]["extractor_count"] > 1000
        assert payload["capabilities"]["ffmpeg"] is True
        assert app.state.db.ping()


def test_analysis_job_cancel_and_idempotency_flow(settings: Settings) -> None:
    app, client_context = app_client(settings)

    async def fake_validate(url: str) -> ValidatedURL:
        return ValidatedURL(url, "example.com", ("8.8.8.8",))

    app.state.url_validator.validate = fake_validate
    with client_context as client:
        created = client.post(
            "/api/v1/analyses",
            json={"url": "https://example.com/watch?v=1", "playlist": False},
            headers={"Origin": "http://testserver"},
        )
        assert created.status_code == 202
        analysis_id = created.json()["id"]
        assert created.json()["status"] == "queued"

        service = YtDlpService(settings, app.state.db)
        result = service._normalize_single(sample_info())
        app.state.db.complete_analysis(analysis_id, result)
        analyzed = client.get(f"/api/v1/analyses/{analysis_id}")
        assert analyzed.status_code == 200
        assert analyzed.json()["result"]["platform"] == "YouTube"

        choice = next(item for item in result["choices"] if item["policy"] == "best")
        request_body = {
            "analysis_id": analysis_id,
            "choice_id": choice["id"],
            "subtitle_languages": ["zh-Hans"],
            "embed_metadata": True,
        }
        first = client.post(
            "/api/v1/jobs",
            json=request_body,
            headers={"Origin": "http://testserver", "Idempotency-Key": "flow-key-123"},
        )
        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        job_id = first.json()["id"]
        # Idempotent retries must replay even when the queue has become full.
        settings.max_queued_operations = 1

        replay = client.post(
            "/api/v1/jobs",
            json=request_body,
            headers={"Origin": "http://testserver", "Idempotency-Key": "flow-key-123"},
        )
        assert replay.status_code == 202
        assert replay.headers["idempotent-replay"] == "true"
        assert replay.json()["id"] == job_id

        cancelled = client.delete(f"/api/v1/jobs/{job_id}", headers={"Origin": "http://testserver"})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        listing = client.get("/api/v1/jobs")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["id"] == job_id


def test_artifact_download_supports_range_and_purge(settings: Settings) -> None:
    app, client_context = app_client(settings)
    db = app.state.db
    analysis = db.create_analysis(
        owner=app.state.session_manager.principal,
        request_url="https://example.com/video",
        url_hash="e" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    db.claim_next_operation()
    choice = {
        "id": "1db71dbb-fe72-4b84-ac04-c4827cf48361",
        "kind": "video",
        "policy": "best",
        "label": "Best",
    }
    db.complete_analysis(
        analysis["id"],
        {"kind": "single", "id": "media", "title": "Range", "choices": [choice]},
    )
    job, _ = db.create_job(
        owner=app.state.session_manager.principal,
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="Range",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    db.claim_next_operation()
    artifact_dir = settings.artifacts_dir / job["id"]
    artifact_dir.mkdir()
    artifact_path = artifact_dir / "artifact-001.mp4"
    artifact_path.write_bytes(b"0123456789")
    artifact = db.add_artifact(
        job_id=job["id"],
        relpath=f"{job['id']}/artifact-001.mp4",
        filename="范围测试.mp4",
        size=10,
        sha256="84d89877f0d4041efb6bf91a16f0248f2fd573e6af05d83824b381fc8f3e0294",
        media_type="video/mp4",
        primary=True,
        ttl_hours=12,
    )
    db.complete_job(job["id"], ttl_hours=12)

    with client_context as client:
        response = client.get(f"/api/v1/artifacts/{artifact['id']}", headers={"Range": "bytes=2-5"})
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert response.headers["x-artifact-sha256"] == artifact["sha256"]

        head = client.head(f"/api/v1/artifacts/{artifact['id']}")
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        assert head.content == b""

        purged = client.delete(f"/api/v1/jobs/{job['id']}", headers={"Origin": "http://testserver"})
        assert purged.status_code == 200
        assert purged.json()["status"] == "expired"
        assert not artifact_path.exists()
        assert client.get(f"/api/v1/artifacts/{artifact['id']}").status_code == 404


def test_optional_token_authentication(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "auth-data",
        access_token="a-very-long-private-token",
        app_secret="test-secret",
        cookie_secure=False,
        allow_private_urls=True,
        js_runtime="",
        min_free_disk_mb=32,
    )
    _app, client_context = app_client(settings)
    with client_context as client:
        assert client.get("/api/v1/config").status_code == 200
        assert client.get("/api/v1/jobs").status_code == 401
        wrong = client.post(
            "/api/v1/auth/session",
            json={"token": "wrong"},
            headers={"Origin": "http://testserver"},
        )
        assert wrong.status_code == 401
        login = client.post(
            "/api/v1/auth/session",
            json={"token": "a-very-long-private-token"},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        assert login.json()["authenticated"] is True
        assert "signal_session" in client.cookies
        assert client.get("/api/v1/jobs").status_code == 200

        cross_site = client.post(
            "/api/v1/analyses",
            json={"url": "https://example.com/video"},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_site.status_code == 403
        assert cross_site.json()["error"]["code"] == "INVALID_ORIGIN"
