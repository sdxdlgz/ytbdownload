from __future__ import annotations

from app.database import Database


def create_completed_analysis(db: Database, owner: str = "owner") -> dict:
    analysis = db.create_analysis(
        owner=owner,
        request_url="https://example.com/video",
        url_hash="a" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    assert db.claim_next_operation() == ("analysis", analysis["id"])
    result = {
        "kind": "single",
        "id": "media-id",
        "extractor": "Generic",
        "platform": "Generic",
        "title": "Media",
        "choices": [
            {
                "id": "23ab6b1c-694d-432e-8806-895f847e41f4",
                "kind": "video",
                "policy": "best",
                "label": "Best",
            }
        ],
    }
    db.complete_analysis(analysis["id"], result)
    return db.get_analysis(analysis["id"], owner=owner)


def test_analysis_lifecycle_and_owner_scope(db: Database) -> None:
    analysis = create_completed_analysis(db)
    assert analysis["status"] == "completed"
    assert analysis["result"]["id"] == "media-id"
    assert db.get_analysis(analysis["id"], owner="another-owner") is None
    assert db.queued_operation_count() == 0


def test_idle_claim_does_not_compete_for_sqlite_writer_lock(db: Database) -> None:
    with db.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert db.claim_next_operation() is None
        writer.execute("ROLLBACK")


def test_job_idempotency_progress_and_completion(db: Database) -> None:
    analysis = create_completed_analysis(db)
    choice = analysis["result"]["choices"][0]
    job, created = db.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={"analysis_id": analysis["id"], "choice_id": choice["id"]},
        title="Media",
        platform="Generic",
        idempotency_key="idempotent-key",
        ttl_hours=12,
    )
    assert created
    replay, replay_created = db.create_job(
        owner="owner",
        analysis_id=analysis["id"],
        choice=choice,
        request={},
        title="ignored",
        platform="ignored",
        idempotency_key="idempotent-key",
        ttl_hours=12,
    )
    assert not replay_created
    assert replay["id"] == job["id"]

    assert db.claim_next_operation() == ("job", job["id"])
    db.update_job_progress(
        job["id"],
        phase="downloading",
        progress=42.5,
        downloaded_bytes=1024,
        total_bytes=2048,
        speed=512,
        eta=2,
    )
    db.set_job_postprocessing(job["id"])
    artifact = db.add_artifact(
        job_id=job["id"],
        relpath=f"{job['id']}/artifact-001.mp4",
        filename="Media.mp4",
        size=2048,
        sha256="b" * 64,
        media_type="video/mp4",
        primary=True,
        ttl_hours=12,
    )
    db.complete_job(job["id"], ttl_hours=12)
    completed = db.get_job(job["id"], owner="owner")
    assert completed["status"] == "completed"
    assert completed["progress"] == 100
    assert completed["artifacts"][0]["id"] == artifact["id"]
    assert completed["artifacts"][0]["primary"] is True


def test_queued_job_cancellation_is_immediate(db: Database) -> None:
    analysis = create_completed_analysis(db)
    choice = analysis["result"]["choices"][0]
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
    cancelled = db.request_cancel(job["id"], "owner")
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert db.claim_next_operation() is None


def test_analysis_expiry_waits_for_active_job(db: Database) -> None:
    analysis = create_completed_analysis(db)
    choice = analysis["result"]["choices"][0]
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
    with db.connect() as connection:
        connection.execute(
            "UPDATE analyses SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (analysis["id"],),
        )
    assert db.expire_analyses() == 0
    assert db.get_analysis(analysis["id"])["result"] is not None
    db.request_cancel(job["id"], "owner")
    assert db.expire_analyses() == 1
    assert db.get_analysis(analysis["id"])["status"] == "expired"


def test_reconcile_marks_interrupted_worker_failed(db: Database) -> None:
    analysis = db.create_analysis(
        owner="owner",
        request_url="https://example.com/video",
        url_hash="c" * 64,
        playlist=False,
        ttl_minutes=60,
    )
    assert db.claim_next_operation() == ("analysis", analysis["id"])
    interrupted = db.reconcile_interrupted()
    assert ("analysis", analysis["id"]) in interrupted
    assert db.get_analysis(analysis["id"])["error_code"] == "SERVER_RESTARTED"
