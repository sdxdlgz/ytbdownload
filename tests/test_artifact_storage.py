from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.artifact_storage import S3ArtifactStorage
from app.config import Settings
from app.database import Database
from app.errors import AppError


class FakeS3Client:
    def __init__(self, *, fail_upload_number: int | None = None) -> None:
        self.fail_upload_number = fail_upload_number
        self.upload_count = 0
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.presigns: list[dict[str, Any]] = []
        self.deletions: list[dict[str, Any]] = []

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, Any],
        Callback,
        Config,
    ) -> None:
        del Config
        self.upload_count += 1
        if self.fail_upload_number == self.upload_count:
            raise OSError("injected upload failure")
        data = Path(filename).read_bytes()
        Callback(len(data))
        self.objects[(bucket, key)] = {
            "data": data,
            "extra": ExtraArgs,
            "etag": f'"multipart-{self.upload_count}-2"',
            "version": f"version-{self.upload_count}",
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        item = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(item["data"]),
            "Metadata": item["extra"]["Metadata"],
            "ETag": item["etag"],
            "VersionId": item["version"],
        }

    def generate_presigned_url(self, operation: str, **kwargs) -> str:
        call = {"operation": operation, **kwargs}
        self.presigns.append(call)
        return f"https://objects.example/{operation}/{kwargs['Params']['Key']}"

    def delete_object(self, **kwargs) -> None:
        self.deletions.append(kwargs)
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)

    def head_bucket(self, *, Bucket: str) -> None:
        assert Bucket


def s3_settings(settings: Settings, **updates: Any) -> Settings:
    return settings.model_copy(
        update={
            "s3_enabled": True,
            "s3_bucket": "private-media",
            "s3_region": "auto",
            "s3_prefix": "signal/test",
            "s3_keep_local": False,
            "s3_failure_mode": "required",
            "s3_delete_on_expiry": True,
            "s3_presign_ttl_seconds": 600,
            **updates,
        }
    )


def create_running_job_with_artifacts(
    db: Database, settings: Settings, contents: list[bytes]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Path]]:
    analysis = db.create_analysis(
        owner="owner",
        request_url="https://example.com/media",
        url_hash="a" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    assert db.claim_next_operation() == ("analysis", analysis["id"])
    choice = {
        "id": "2a85b811-5f1c-47d4-a1f7-c61a63934982",
        "kind": "video",
        "policy": "best",
        "label": "Best",
    }
    db.complete_analysis(
        analysis["id"],
        {"id": "media", "extractor": "Generic", "title": "Media", "choices": [choice]},
    )
    job, _ = db.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="Media",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    assert db.claim_next_operation() == ("job", job["id"])
    directory = settings.artifacts_dir / job["id"]
    directory.mkdir()
    artifacts = []
    paths = []
    for index, content in enumerate(contents, 1):
        path = directory / f"artifact-{index:03d}.mp4"
        path.write_bytes(content)
        paths.append(path)
        artifacts.append(
            db.add_artifact(
                job_id=job["id"],
                relpath=f"{job['id']}/{path.name}",
                filename=f"Media {index}.mp4",
                size=len(content),
                sha256=(f"{index:x}" * 64)[:64],
                media_type="video/mp4",
                primary=index == 1,
                ttl_hours=12,
            )
        )
    return job, artifacts, paths


def test_publish_promote_presign_and_delete_remote_artifacts(
    settings: Settings, db: Database
) -> None:
    configured = s3_settings(settings)
    job, artifacts, paths = create_running_job_with_artifacts(
        db, configured, [b"first-video", b"second-video"]
    )
    client = FakeS3Client()
    storage = S3ArtifactStorage(configured, db, client=client)

    published = storage.publish_job_artifacts(job["id"], artifacts)
    assert len(published) == 2
    assert all(item["storage_backend"] == "s3" for item in published)
    assert all(item["storage_state"] == "ready" for item in published)
    assert all(item["local_available"] is False for item in published)
    assert all(not path.exists() for path in paths)
    assert len(client.objects) == 2
    assert all("Media" not in key for _bucket, key in client.objects)
    first_uploaded = next(iter(client.objects.values()))
    assert first_uploaded["extra"]["Metadata"]["sha256"] == artifacts[0]["sha256"]
    assert first_uploaded["extra"]["ContentDisposition"].startswith("attachment;")

    get_url = storage.presign(published[0], method="GET", ttl_seconds=9999)
    head_url = storage.presign(published[0], method="HEAD", ttl_seconds=300)
    assert "/get_object/" in get_url
    assert "/head_object/" in head_url
    assert client.presigns[0]["ExpiresIn"] == configured.s3_presign_ttl_seconds
    assert client.presigns[0]["HttpMethod"] == "GET"
    assert client.presigns[1]["HttpMethod"] == "HEAD"

    db.complete_job(job["id"], ttl_hours=12)
    assert db.mark_job_expired(job["id"])
    result = storage.process_deletion_outbox(limit=10)
    assert result == {"deleted": 2, "failed": 0, "pending": 0}
    assert len(client.deletions) == 2
    assert all("VersionId" in call for call in client.deletions)
    assert client.objects == {}


def test_upload_failure_falls_back_to_complete_local_set(settings: Settings, db: Database) -> None:
    configured = s3_settings(settings, s3_keep_local=True, s3_failure_mode="fallback")
    job, artifacts, paths = create_running_job_with_artifacts(db, configured, [b"first", b"second"])
    client = FakeS3Client(fail_upload_number=2)
    storage = S3ArtifactStorage(configured, db, client=client)

    published = storage.publish_job_artifacts(job["id"], artifacts)
    assert all(item["storage_backend"] == "local" for item in published)
    assert all(item["storage_state"] == "ready" for item in published)
    assert all(item["object_key"] is None for item in published)
    assert all(path.exists() for path in paths)
    assert client.objects == {}
    assert db.pending_storage_deletion_count() == 0


def test_required_upload_failure_leaves_staging_for_worker_revocation(
    settings: Settings, db: Database
) -> None:
    configured = s3_settings(settings, s3_failure_mode="required", s3_keep_local=True)
    job, artifacts, _paths = create_running_job_with_artifacts(
        db, configured, [b"first", b"second"]
    )
    client = FakeS3Client(fail_upload_number=2)
    storage = S3ArtifactStorage(configured, db, client=client)

    with pytest.raises(AppError) as caught:
        storage.publish_job_artifacts(job["id"], artifacts)
    assert caught.value.code == "STORAGE_UPLOAD_FAILED"
    assert any(item["storage_state"] == "uploading" for item in db.list_artifacts(job["id"]))

    assert db.revoke_job_artifacts(job["id"]) == 2
    storage.process_deletion_outbox(limit=10)
    assert db.list_artifacts(job["id"]) == []
    assert client.objects == {}
