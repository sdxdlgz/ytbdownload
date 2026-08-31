from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.artifact_storage import S3ArtifactStorage
from app.config import Settings
from app.database import Database

pytestmark = pytest.mark.s3integration


def test_real_minio_upload_presigned_range_head_and_delete(tmp_path: Path) -> None:
    endpoint = os.environ.get("MINIO_ENDPOINT")
    if not endpoint:
        pytest.skip("MINIO_ENDPOINT is not configured")
    access_key = os.environ.get("MINIO_ACCESS_KEY", "signalminio")
    secret_key = os.environ.get("MINIO_SECRET_KEY", "signal-minio-password-123")
    bucket = f"signal-test-{uuid4().hex[:16]}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    client.create_bucket(Bucket=bucket)
    try:
        settings = Settings(
            _env_file=None,
            environment="test",
            data_dir=tmp_path / "minio-data",
            app_secret="minio-integration-secret",
            js_runtime="",
            min_free_disk_mb=32,
            s3_enabled=True,
            s3_bucket=bucket,
            s3_region="us-east-1",
            s3_endpoint_url=endpoint,
            s3_allow_insecure_endpoint=endpoint.startswith("http://"),
            s3_access_key_id=SecretStr(access_key),
            s3_secret_access_key=SecretStr(secret_key),
            s3_addressing_style="path",
            s3_keep_local=False,
            s3_failure_mode="required",
            s3_presign_ttl_seconds=600,
        )
        settings.ensure_directories()
        database = Database(settings.database_path)
        database.initialize()
        job, artifact, content = create_artifact(database, settings)
        storage = S3ArtifactStorage(settings, database)

        published = storage.publish_job_artifacts(job["id"], [artifact])
        assert published[0]["storage_backend"] == "s3"
        assert published[0]["local_available"] is False
        database.complete_job(job["id"], ttl_hours=12)

        get_url = storage.presign(published[0], method="GET", ttl_seconds=300)
        with urlopen(get_url, timeout=20) as response:
            assert response.read() == content
        request = Request(get_url, headers={"Range": "bytes=2-5"})
        with urlopen(request, timeout=20) as response:
            assert response.status == 206
            assert response.headers["Content-Range"] == f"bytes 2-5/{len(content)}"
            assert response.read() == content[2:6]

        head_url = storage.presign(published[0], method="HEAD", ttl_seconds=300)
        request = Request(head_url, method="HEAD")
        with urlopen(request, timeout=20) as response:
            assert response.status == 200
            assert int(response.headers["Content-Length"]) == len(content)
            assert response.read() == b""

        assert database.mark_job_expired(job["id"])
        result = storage.process_deletion_outbox(limit=10)
        assert result["deleted"] == 1
        with pytest.raises(ClientError) as caught:
            client.head_object(Bucket=bucket, Key=published[0]["object_key"])
        assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    finally:
        cleanup_bucket(client, bucket)


def create_artifact(database: Database, settings: Settings) -> tuple[dict, dict, bytes]:
    analysis = database.create_analysis(
        owner="owner",
        request_url="https://example.com/minio",
        url_hash="d" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    database.claim_next_operation()
    choice = {
        "id": "ee82d860-7bfc-4237-be3c-3b553f0348bc",
        "kind": "video",
        "policy": "best",
        "label": "Best",
    }
    database.complete_analysis(
        analysis["id"],
        {"id": "media", "extractor": "Generic", "title": "MinIO", "choices": [choice]},
    )
    job, _ = database.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="MinIO",
        platform="Generic",
        idempotency_key=None,
        ttl_hours=12,
    )
    database.claim_next_operation()
    directory = settings.artifacts_dir / job["id"]
    directory.mkdir()
    path = directory / "artifact-001.bin"
    content = b"0123456789-minio-presigned-range"
    path.write_bytes(content)
    artifact = database.add_artifact(
        job_id=job["id"],
        relpath=f"{job['id']}/{path.name}",
        filename="MinIO.bin",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        media_type="application/octet-stream",
        primary=True,
        ttl_hours=12,
    )
    return job, artifact, content


def cleanup_bucket(client, bucket: str) -> None:
    try:
        response = client.list_object_versions(Bucket=bucket)
        for item in [*(response.get("Versions") or []), *(response.get("DeleteMarkers") or [])]:
            client.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])
        response = client.list_objects_v2(Bucket=bucket)
        for item in response.get("Contents") or []:
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)
    except (ClientError, HTTPError):
        pass
