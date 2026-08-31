from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import Database


def test_existing_v1_database_migrates_artifacts_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            relpath TEXT NOT NULL,
            filename TEXT NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            media_type TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(job_id, relpath)
        );
        INSERT INTO artifacts VALUES (
            'legacy-artifact', 'legacy-job', 'legacy/file.mp4', 'Legacy.mp4',
            4, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'video/mp4', 1, '2026-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00'
        );
        """
    )
    connection.close()

    database = Database(path)
    database.initialize()
    database.initialize()

    with database.connect() as current:
        columns = {row["name"] for row in current.execute("PRAGMA table_info(artifacts)")}
        version = current.execute("PRAGMA user_version").fetchone()[0]
        row = current.execute("SELECT * FROM artifacts WHERE id = 'legacy-artifact'").fetchone()
    assert version == 2
    assert {
        "storage_backend",
        "storage_state",
        "object_bucket",
        "object_key",
        "object_etag",
        "object_version_id",
        "local_available",
        "uploaded_at",
    } <= columns
    assert row["storage_backend"] == "local"
    assert row["storage_state"] == "ready"
    assert row["local_available"] == 1


def test_remote_artifact_promotion_and_expiry_use_deletion_outbox(db: Database) -> None:
    analysis = db.create_analysis(
        owner="owner",
        request_url="https://example.com/video",
        url_hash="f" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    assert db.claim_next_operation() == ("analysis", analysis["id"])
    choice = {
        "id": "0d443f5e-5f8e-4bf8-a1bb-7127dff9fbb2",
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
    artifact = db.add_artifact(
        job_id=job["id"],
        relpath=f"{job['id']}/artifact-001.mp4",
        filename="Media.mp4",
        size=1024,
        sha256="b" * 64,
        media_type="video/mp4",
        primary=True,
        ttl_hours=12,
    )
    db.stage_artifact_upload(
        artifact["id"], job_id=job["id"], bucket="private-bucket", object_key="p/j/a.mp4"
    )
    db.record_artifact_upload_result(
        artifact["id"], etag="opaque-multipart-etag", version_id="version-1"
    )
    db.promote_job_artifacts_to_s3(job["id"], [artifact["id"]], keep_local=False)
    promoted = db.get_artifact(artifact["id"])
    assert promoted["storage_backend"] == "s3"
    assert promoted["storage_state"] == "ready"
    assert promoted["local_available"] is False
    assert promoted["object_etag"] == "opaque-multipart-etag"
    assert job["id"] not in db.oldest_completed_jobs_with_local_artifacts()

    db.complete_job(job["id"], ttl_hours=12)
    assert db.mark_job_expired(job["id"])
    assert db.get_artifact(artifact["id"]) is None
    claimed = db.claim_storage_deletions(limit=5)
    assert len(claimed) == 1
    assert claimed[0]["bucket"] == "private-bucket"
    assert claimed[0]["object_key"] == "p/j/a.mp4"
    assert claimed[0]["version_id"] == "version-1"
    db.acknowledge_storage_deletion(claimed[0]["id"])
    assert db.pending_storage_deletion_count() == 0
