from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class PresignOnlyS3Client:
    def __init__(self) -> None:
        self.presigns: list[dict[str, Any]] = []
        self.deletions: list[dict[str, Any]] = []

    def generate_presigned_url(self, operation: str, **kwargs) -> str:
        self.presigns.append({"operation": operation, **kwargs})
        return f"https://objects.example/{operation}/{kwargs['Params']['Key']}"

    def delete_object(self, **kwargs) -> None:
        self.deletions.append(kwargs)


def client_for(settings: Settings) -> tuple[Any, TestClient]:
    app = create_app(settings)

    async def noop() -> None:
        return None

    app.state.dispatcher.start = noop
    app.state.dispatcher.stop = noop
    return app, TestClient(app)


def create_completed_artifact(app, *, remote: bool) -> tuple[dict, dict, Path]:
    db = app.state.db
    owner = app.state.session_manager.principal
    analysis = db.create_analysis(
        owner=owner,
        request_url="https://example.com/media",
        url_hash="c" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    db.claim_next_operation()
    choice = {
        "id": "f1185c40-aa66-4b66-864f-d18ac3c480ee",
        "kind": "video",
        "policy": "best",
        "label": "Best",
    }
    db.complete_analysis(
        analysis["id"],
        {"id": "media", "extractor": "Generic", "title": "Delivery", "choices": [choice]},
    )
    job, _ = db.create_job(
        owner=owner,
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="Delivery",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    db.claim_next_operation()
    directory = app.state.settings.artifacts_dir / job["id"]
    directory.mkdir()
    path = directory / "artifact-001.mp4"
    path.write_bytes(b"0123456789")
    artifact = db.add_artifact(
        job_id=job["id"],
        relpath=f"{job['id']}/{path.name}",
        filename="Delivery.mp4",
        size=10,
        sha256="84d89877f0d4041efb6bf91a16f0248f2fd573e6af05d83824b381fc8f3e0294",
        media_type="video/mp4",
        primary=True,
        ttl_hours=12,
    )
    if remote:
        db.stage_artifact_upload(
            artifact["id"],
            job_id=job["id"],
            bucket="private-media",
            object_key=f"prefix/{job['id']}/{artifact['id']}.mp4",
        )
        db.record_artifact_upload_result(
            artifact["id"], etag="multipart-etag", version_id="version-1"
        )
        db.promote_job_artifacts_to_s3(job["id"], [artifact["id"]], keep_local=False)
    db.complete_job(job["id"], ttl_hours=12)
    return db.get_job(job["id"], owner=owner), db.get_artifact(artifact["id"]), path


def login(client: TestClient, token: str) -> None:
    response = client.post("/api/v1/auth/session", json={"token": token})
    assert response.status_code == 200, response.text


def test_local_direct_link_needs_no_cookie_supports_range_and_revokes(
    tmp_path: Path,
) -> None:
    token = "delivery-test-access-token-123456"
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "local-direct",
        access_token=token,
        app_secret="delivery-test-secret-that-is-long-enough",
        cookie_secure=False,
        js_runtime="",
        min_free_disk_mb=32,
        direct_links_enabled=True,
        direct_link_ttl_minutes=10,
        direct_link_max_ttl_minutes=60,
    )
    app, client_context = client_for(settings)
    job, artifact, _path = create_completed_artifact(app, remote=False)

    with client_context as client:
        login(client, token)
        issued = client.post(
            f"/api/v1/artifacts/{artifact['id']}/direct-links",
            json={"ttl_minutes": 5},
        )
        assert issued.status_code == 201, issued.text
        payload = issued.json()
        assert payload["storage_backend"] == "local"
        direct_url = payload["url"]

        client.cookies.clear()
        assert client.get(f"/api/v1/artifacts/{artifact['id']}").status_code == 401
        ranged = client.get(direct_url, headers={"Range": "bytes=2-5"})
        assert ranged.status_code == 206
        assert ranged.content == b"2345"
        head = client.head(direct_url)
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        assert head.content == b""

        parts = urlsplit(direct_url)
        query = parse_qs(parts.query)
        signature = query["signature"][0]
        tampered = direct_url.replace(
            signature, signature[:-1] + ("A" if signature[-1] != "A" else "B")
        )
        assert client.get(tampered).status_code == 404

        login(client, token)
        purged = client.delete(f"/api/v1/jobs/{job['id']}")
        assert purged.status_code == 200
        client.cookies.clear()
        assert client.get(direct_url).status_code == 404


def test_s3_authenticated_and_direct_get_head_use_method_specific_307(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path / "remote-direct",
        access_token="",
        app_secret="remote-delivery-test-secret",
        js_runtime="",
        min_free_disk_mb=32,
        s3_enabled=True,
        s3_bucket="private-media",
        s3_keep_local=False,
        s3_failure_mode="required",
        s3_presign_ttl_seconds=600,
        direct_links_enabled=True,
    )
    app, client_context = client_for(settings)
    fake = PresignOnlyS3Client()
    app.state.artifact_storage._client = fake
    job, artifact, _path = create_completed_artifact(app, remote=True)

    with client_context as client:
        listing = client.get(f"/api/v1/jobs/{job['id']}")
        assert listing.status_code == 200
        public_artifact = listing.json()["artifacts"][0]
        assert public_artifact["storage_backend"] == "s3"
        assert public_artifact["local_available"] is False
        assert "object_key" not in public_artifact

        get_response = client.get(f"/api/v1/artifacts/{artifact['id']}", follow_redirects=False)
        head_response = client.head(f"/api/v1/artifacts/{artifact['id']}", follow_redirects=False)
        assert get_response.status_code == 307
        assert "/get_object/" in get_response.headers["location"]
        assert head_response.status_code == 307
        assert "/head_object/" in head_response.headers["location"]
        assert fake.presigns[0]["HttpMethod"] == "GET"
        assert fake.presigns[1]["HttpMethod"] == "HEAD"

        issued = client.post(f"/api/v1/artifacts/{artifact['id']}/direct-links", json={})
        direct = client.get(issued.json()["url"], follow_redirects=False)
        assert direct.status_code == 307
        direct_head = client.head(issued.json()["url"], follow_redirects=False)
        assert direct_head.status_code == 307
        assert "/head_object/" in direct_head.headers["location"]
        assert "/get_object/" in direct.headers["location"]

        purged = client.delete(f"/api/v1/jobs/{job['id']}")
        assert purged.status_code == 200
        assert fake.deletions == [
            {
                "Bucket": "private-media",
                "Key": artifact["object_key"],
                "VersionId": "version-1",
            }
        ]
