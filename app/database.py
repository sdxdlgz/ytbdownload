from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models import TERMINAL_STATUSES


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


def iso_after(*, seconds: int = 0, minutes: int = 0, hours: int = 0) -> str:
    return (utcnow() + timedelta(seconds=seconds, minutes=minutes, hours=hours)).isoformat()


class Database:
    """Small SQLite repository with atomic operation leasing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    request_url TEXT NOT NULL,
                    url_hash TEXT NOT NULL,
                    playlist INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    worker_pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_analyses_owner_created
                    ON analyses(owner, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_analyses_status_created
                    ON analyses(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_analyses_expiry
                    ON analyses(expires_at);

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    analysis_id TEXT NOT NULL REFERENCES analyses(id),
                    choice_id TEXT NOT NULL,
                    choice_json TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER,
                    total_bytes INTEGER,
                    speed REAL,
                    eta INTEGER,
                    playlist_index INTEGER,
                    playlist_count INTEGER,
                    title TEXT,
                    platform TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_pid INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_owner_created
                    ON jobs(owner, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_expiry
                    ON jobs(expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
                    ON jobs(owner, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    relpath TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    storage_backend TEXT NOT NULL DEFAULT 'local',
                    storage_state TEXT NOT NULL DEFAULT 'ready',
                    object_bucket TEXT,
                    object_key TEXT,
                    object_etag TEXT,
                    object_version_id TEXT,
                    local_available INTEGER NOT NULL DEFAULT 1,
                    uploaded_at TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(job_id, relpath)
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_job
                    ON artifacts(job_id, is_primary DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_artifacts_expiry
                    ON artifacts(expires_at);
                CREATE TABLE IF NOT EXISTS storage_deletions (
                    id TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    version_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    not_before TEXT NOT NULL,
                    lease_until TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(backend, bucket, object_key, version_id)
                );

                CREATE INDEX IF NOT EXISTS idx_storage_deletions_due
                    ON storage_deletions(not_before, lease_until);
                """
            )
            self._migrate_artifact_storage(connection)

    @staticmethod
    def _migrate_artifact_storage(connection: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(artifacts)")}
        additions = {
            "storage_backend": "TEXT NOT NULL DEFAULT 'local'",
            "storage_state": "TEXT NOT NULL DEFAULT 'ready'",
            "object_bucket": "TEXT",
            "object_key": "TEXT",
            "object_etag": "TEXT",
            "object_version_id": "TEXT",
            "local_available": "INTEGER NOT NULL DEFAULT 1",
            "uploaded_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                # Names/declarations are fixed above and never contain request data.
                connection.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {declaration}")  # nosec B608
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_remote_object
            ON artifacts(object_bucket, object_key)
            WHERE object_key IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_local_jobs
            ON artifacts(job_id, local_available)
            """
        )
        connection.execute("PRAGMA user_version = 2")

    def ping(self) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def queued_operation_count(self) -> int:
        with self.connect() as connection:
            analysis_count = connection.execute(
                "SELECT COUNT(*) FROM analyses WHERE status = 'queued'"
            ).fetchone()[0]
            job_count = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
            ).fetchone()[0]
            return int(analysis_count + job_count)

    def create_analysis(
        self,
        *,
        owner: str,
        request_url: str,
        url_hash: str,
        playlist: bool,
        ttl_minutes: int,
    ) -> dict[str, Any]:
        analysis_id = str(uuid4())
        now = iso_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO analyses (
                    id, owner, request_url, url_hash, playlist, status,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    analysis_id,
                    owner,
                    request_url,
                    url_hash,
                    int(playlist),
                    now,
                    now,
                    iso_after(minutes=ttl_minutes),
                ),
            )
        return self.get_analysis(analysis_id, owner=owner)  # type: ignore[return-value]

    def get_analysis(self, analysis_id: str, *, owner: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM analyses WHERE id = ?"
        params: list[Any] = [analysis_id]
        if owner is not None:
            query += " AND owner = ?"
            params.append(owner)
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._analysis_row(row) if row else None

    def complete_analysis(self, analysis_id: str, result: dict[str, Any]) -> None:
        now = iso_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE analyses
                SET status = 'completed', result_json = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?, completed_at = ?,
                    worker_pid = NULL
                WHERE id = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    analysis_id,
                ),
            )

    def fail_analysis(self, analysis_id: str, code: str, message: str) -> None:
        now = iso_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE analyses
                SET status = 'failed', error_code = ?, error_message = ?,
                    updated_at = ?, completed_at = ?, worker_pid = NULL
                WHERE id = ? AND status NOT IN ('completed', 'expired')
                """,
                (code, message, now, now, analysis_id),
            )

    def create_job(
        self,
        *,
        owner: str,
        analysis_id: str,
        choice: dict[str, Any],
        request: dict[str, Any],
        title: str | None,
        platform: str | None,
        idempotency_key: str | None,
        ttl_hours: int,
    ) -> tuple[dict[str, Any], bool]:
        if idempotency_key:
            existing = self.get_job_by_idempotency(owner, idempotency_key)
            if existing:
                return existing, False

        job_id = str(uuid4())
        now = iso_now()
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        id, owner, analysis_id, choice_id, choice_json, request_json,
                        idempotency_key, status, phase, title, platform,
                        created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        owner,
                        analysis_id,
                        choice["id"],
                        json.dumps(choice, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                        idempotency_key,
                        title,
                        platform,
                        now,
                        now,
                        iso_after(hours=ttl_hours),
                    ),
                )
        except sqlite3.IntegrityError:
            if idempotency_key:
                existing = self.get_job_by_idempotency(owner, idempotency_key)
                if existing:
                    return existing, False
            raise
        return self.get_job(job_id, owner=owner), True  # type: ignore[return-value]

    def get_job(self, job_id: str, *, owner: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM jobs WHERE id = ?"
        params: list[Any] = [job_id]
        if owner is not None:
            query += " AND owner = ?"
            params.append(owner)
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        if not row:
            return None
        job = self._job_row(row)
        job["artifacts"] = self.list_artifacts(job_id)
        return job

    def get_job_by_idempotency(self, owner: str, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM jobs WHERE owner = ? AND idempotency_key = ?",
                (owner, key),
            ).fetchone()
        return self.get_job(row["id"], owner=owner) if row else None

    def list_jobs(self, owner: str, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE owner = ?
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (owner, limit, offset),
            ).fetchall()
            total = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner = ?", (owner,)
            ).fetchone()[0]
        jobs = []
        for row in rows:
            job = self._job_row(row)
            job["artifacts"] = self.list_artifacts(job["id"])
            jobs.append(job)
        return jobs, int(total)

    def load_job_context(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT j.*, a.request_url, a.playlist AS analysis_playlist,
                       a.result_json AS analysis_result_json, a.status AS analysis_status
                FROM jobs j
                JOIN analyses a ON a.id = j.analysis_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["choice"] = json.loads(result.pop("choice_json"))
        result["request"] = json.loads(result.pop("request_json"))
        raw_analysis = result.pop("analysis_result_json")
        result["analysis_result"] = json.loads(raw_analysis) if raw_analysis else None
        result["analysis_playlist"] = bool(result["analysis_playlist"])
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def claim_next_operation(self) -> tuple[str, str] | None:
        """Atomically lease the oldest queued analysis or job."""

        def oldest_queued(connection: sqlite3.Connection) -> tuple[str, sqlite3.Row] | None:
            analysis = connection.execute(
                "SELECT id, created_at FROM analyses WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            job = connection.execute(
                "SELECT id, created_at FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if analysis and (not job or analysis["created_at"] <= job["created_at"]):
                return "analysis", analysis
            if job:
                return "job", job
            return None

        with self.connect() as connection:
            # Avoid taking SQLite's only writer lock on every idle dispatcher poll.
            # A second lookup inside BEGIN IMMEDIATE keeps the lease atomic if a row exists.
            if oldest_queued(connection) is None:
                return None
            connection.execute("BEGIN IMMEDIATE")
            candidate = oldest_queued(connection)
            if candidate is None:
                connection.execute("COMMIT")
                return None

            kind, row = candidate
            now = iso_now()
            if kind == "analysis":
                query = """
                    UPDATE analyses
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        updated_at = ?
                    WHERE id = ? AND status = 'queued'
                """
            else:
                query = """
                    UPDATE jobs
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        updated_at = ?, phase = 'extracting'
                    WHERE id = ? AND status = 'queued'
                """
            cursor = connection.execute(query, (now, now, row["id"]))
            connection.execute("COMMIT")
            if cursor.rowcount != 1:
                return None
            return kind, str(row["id"])

    def set_worker_pid(self, kind: str, operation_id: str, pid: int) -> None:
        query = (
            "UPDATE analyses SET worker_pid = ?, updated_at = ? WHERE id = ?"
            if kind == "analysis"
            else "UPDATE jobs SET worker_pid = ?, updated_at = ? WHERE id = ?"
        )
        with self.connect() as connection:
            connection.execute(query, (pid, iso_now(), operation_id))

    def operation_state(self, kind: str, operation_id: str) -> dict[str, Any] | None:
        query = (
            "SELECT status, started_at FROM analyses WHERE id = ?"
            if kind == "analysis"
            else "SELECT status, started_at, cancel_requested FROM jobs WHERE id = ?"
        )
        with self.connect() as connection:
            row = connection.execute(query, (operation_id,)).fetchone()
        return dict(row) if row else None

    def update_job_progress(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        progress: float | None = None,
        downloaded_bytes: int | None = None,
        total_bytes: int | None = None,
        speed: float | None = None,
        eta: int | None = None,
        playlist_index: int | None = None,
        playlist_count: int | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET
                    updated_at = ?,
                    phase = COALESCE(?, phase),
                    progress = COALESCE(?, progress),
                    downloaded_bytes = COALESCE(?, downloaded_bytes),
                    total_bytes = COALESCE(?, total_bytes),
                    speed = COALESCE(?, speed),
                    eta = COALESCE(?, eta),
                    playlist_index = COALESCE(?, playlist_index),
                    playlist_count = COALESCE(?, playlist_count)
                WHERE id = ?
                  AND status IN ('running', 'postprocessing', 'cancelling')
                """,
                (
                    iso_now(),
                    phase,
                    progress,
                    downloaded_bytes,
                    total_bytes,
                    speed,
                    eta,
                    playlist_index,
                    playlist_count,
                    job_id,
                ),
            )

    def set_job_postprocessing(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'postprocessing', phase = 'postprocessing',
                    progress = CASE WHEN progress < 95 THEN 95 ELSE progress END,
                    updated_at = ?
                WHERE id = ? AND status IN ('running', 'cancelling')
                """,
                (iso_now(), job_id),
            )

    def complete_job(self, job_id: str, *, ttl_hours: int) -> None:
        now = iso_now()
        expiry = iso_after(hours=ttl_hours)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ready_count = connection.execute(
                """
                SELECT COUNT(*) FROM artifacts
                WHERE job_id = ? AND storage_state = 'ready'
                """,
                (job_id,),
            ).fetchone()[0]
            if ready_count < 1:
                connection.execute("ROLLBACK")
                raise RuntimeError("job cannot complete without a ready artifact")
            connection.execute(
                "UPDATE artifacts SET expires_at = ? WHERE job_id = ?", (expiry, job_id)
            )
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'completed', phase = 'ready', progress = 100,
                    error_code = NULL, error_message = NULL, worker_pid = NULL,
                    updated_at = ?, completed_at = ?, expires_at = ?
                WHERE id = ? AND status IN ('running', 'postprocessing')
                """,
                (now, now, expiry, job_id),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise RuntimeError("job completion transition failed")
            connection.execute("COMMIT")

    def fail_job(self, job_id: str, code: str, message: str, *, cancelled: bool = False) -> None:
        now = iso_now()
        status = "cancelled" if cancelled else "failed"
        phase = "cancelled" if cancelled else "failed"
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, phase = ?, error_code = ?, error_message = ?,
                    worker_pid = NULL, updated_at = ?, completed_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'expired')
                """,
                (status, phase, code, message, now, now, job_id),
            )

    def request_cancel(self, job_id: str, owner: str) -> dict[str, Any] | None:
        now = iso_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ? AND owner = ?", (job_id, owner)
            ).fetchone()
            if not row:
                connection.execute("COMMIT")
                return None
            status = row["status"]
            if status == "queued":
                connection.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', phase = 'cancelled',
                        cancel_requested = 1, error_code = 'CANCELLED',
                        error_message = '任务已取消。', updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (now, now, job_id),
                )
            elif status in {"running", "postprocessing", "cancelling"}:
                connection.execute(
                    """
                    UPDATE jobs SET status = 'cancelling', phase = 'cancelling',
                        cancel_requested = 1, updated_at = ? WHERE id = ?
                    """,
                    (now, job_id),
                )
            connection.execute("COMMIT")
        return self.get_job(job_id, owner=owner)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def add_artifact(
        self,
        *,
        job_id: str,
        relpath: str,
        filename: str,
        size: int,
        sha256: str,
        media_type: str,
        primary: bool,
        ttl_hours: int,
    ) -> dict[str, Any]:
        artifact_id = str(uuid4())
        now = iso_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts (
                    id, job_id, relpath, filename, size, sha256, media_type,
                    is_primary, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    job_id,
                    relpath,
                    filename,
                    size,
                    sha256,
                    media_type,
                    int(primary),
                    now,
                    iso_after(hours=ttl_hours),
                ),
            )
        return self.get_artifact(artifact_id)  # type: ignore[return-value]

    def get_artifact(self, artifact_id: str, *, owner: str | None = None) -> dict[str, Any] | None:
        query = """
            SELECT a.*, j.owner, j.status AS job_status
            FROM artifacts a JOIN jobs j ON j.id = a.job_id
            WHERE a.id = ?
        """
        params: list[Any] = [artifact_id]
        if owner is not None:
            query += " AND j.owner = ?"
            params.append(owner)
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._artifact_row(row) if row else None

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM artifacts WHERE job_id = ?
                ORDER BY is_primary DESC, created_at, filename
                """,
                (job_id,),
            ).fetchall()
        return [self._artifact_row(row) for row in rows]

    def stage_artifact_upload(
        self, artifact_id: str, *, job_id: str, bucket: str, object_key: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE artifacts
                SET storage_state = 'uploading', object_bucket = ?, object_key = ?,
                    object_etag = NULL, object_version_id = NULL, uploaded_at = NULL
                WHERE id = ? AND job_id = ? AND storage_backend = 'local'
                """,
                (bucket, object_key, artifact_id, job_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("artifact could not be staged for upload")
        return self.get_artifact(artifact_id)  # type: ignore[return-value]

    def record_artifact_upload_result(
        self, artifact_id: str, *, etag: str | None, version_id: str | None
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE artifacts
                SET object_etag = ?, object_version_id = ?
                WHERE id = ? AND storage_state = 'uploading' AND object_key IS NOT NULL
                """,
                (etag, version_id, artifact_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("staged artifact disappeared before upload verification")

    def promote_job_artifacts_to_s3(
        self, job_id: str, artifact_ids: list[str], *, keep_local: bool
    ) -> None:
        expected = set(artifact_ids)
        now = iso_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM artifacts
                WHERE job_id = ? AND storage_state = 'uploading'
                  AND object_bucket IS NOT NULL AND object_key IS NOT NULL
                """,
                (job_id,),
            ).fetchall()
            actual = {str(row["id"]) for row in rows}
            if actual != expected or not expected:
                connection.execute("ROLLBACK")
                raise RuntimeError("not every job artifact completed S3 upload")
            updated = 0
            for artifact_id in artifact_ids:
                cursor = connection.execute(
                    """
                    UPDATE artifacts
                    SET storage_backend = 's3', storage_state = 'ready',
                        local_available = ?, uploaded_at = ?
                    WHERE id = ? AND job_id = ? AND storage_state = 'uploading'
                    """,
                    (int(keep_local), now, artifact_id, job_id),
                )
                updated += cursor.rowcount
            if updated != len(artifact_ids):
                connection.execute("ROLLBACK")
                raise RuntimeError("S3 artifact promotion was incomplete")
            connection.execute("COMMIT")

    def fallback_job_artifacts_to_local(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ? AND object_key IS NOT NULL",
                (job_id,),
            ).fetchall()
            self._enqueue_remote_deletions(connection, rows)
            connection.execute(
                """
                UPDATE artifacts
                SET storage_backend = 'local', storage_state = 'ready',
                    object_bucket = NULL, object_key = NULL, object_etag = NULL,
                    object_version_id = NULL, local_available = 1, uploaded_at = NULL
                WHERE job_id = ?
                """,
                (job_id,),
            )
            connection.execute("COMMIT")

    def revoke_job_artifacts(self, job_id: str) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ?", (job_id,)
            ).fetchall()
            queued = self._enqueue_remote_deletions(connection, rows)
            connection.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            connection.execute("COMMIT")
        return queued

    @staticmethod
    def _enqueue_remote_deletions(connection: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
        queued = 0
        now = iso_now()
        for row in rows:
            bucket = row["object_bucket"]
            object_key = row["object_key"]
            if not bucket or not object_key:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO storage_deletions (
                    id, backend, bucket, object_key, version_id, job_id,
                    not_before, created_at
                ) VALUES (?, 's3', ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    bucket,
                    object_key,
                    (row["object_version_id"] or ""),
                    row["job_id"],
                    now,
                    now,
                ),
            )
            queued += cursor.rowcount
        return queued

    def claim_storage_deletions(
        self, *, limit: int = 50, lease_seconds: int = 60
    ) -> list[dict[str, Any]]:
        now = iso_now()
        lease_until = iso_after(seconds=lease_seconds)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM storage_deletions
                WHERE not_before <= ? AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY created_at LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE storage_deletions
                    SET lease_until = ?, attempts = attempts + 1
                    WHERE id = ?
                    """,
                    (lease_until, row["id"]),
                )
            connection.execute("COMMIT")
        return [dict(row) for row in rows]

    def acknowledge_storage_deletion(self, deletion_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM storage_deletions WHERE id = ?", (deletion_id,))

    def fail_storage_deletion(
        self, deletion_id: str, *, message: str, retry_after_seconds: int
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE storage_deletions
                SET last_error = ?, not_before = ?, lease_until = NULL
                WHERE id = ?
                """,
                (message[:500], iso_after(seconds=retry_after_seconds), deletion_id),
            )

    def pending_storage_deletion_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM storage_deletions").fetchone()[0])

    def clear_artifacts(self, job_id: str) -> None:
        self.revoke_job_artifacts(job_id)

    def reconcile_interrupted(self) -> list[tuple[str, str]]:
        """Mark operations left running by a previous API process as failed."""
        now = iso_now()
        interrupted: list[tuple[str, str]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            analyses = connection.execute(
                "SELECT id FROM analyses WHERE status = 'running'"
            ).fetchall()
            jobs = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('running', 'postprocessing', 'cancelling')"
            ).fetchall()
            for row in analyses:
                interrupted.append(("analysis", row["id"]))
            for row in jobs:
                interrupted.append(("job", row["id"]))
            connection.execute(
                """
                UPDATE analyses SET status = 'failed', error_code = 'SERVER_RESTARTED',
                    error_message = '服务重启，分析任务已中止。', worker_pid = NULL,
                    updated_at = ?, completed_at = ? WHERE status = 'running'
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE jobs SET status = 'failed', phase = 'failed',
                    error_code = 'SERVER_RESTARTED', error_message = '服务重启，下载任务已中止。',
                    worker_pid = NULL, updated_at = ?, completed_at = ?
                WHERE status IN ('running', 'postprocessing', 'cancelling')
                """,
                (now, now),
            )
            connection.execute("COMMIT")
        return interrupted

    def expired_jobs(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE expires_at < ? AND status IN ('completed', 'failed', 'cancelled')
                ORDER BY expires_at
                """,
                (iso_now(),),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def oldest_completed_jobs_with_local_artifacts(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.id FROM jobs AS j
                WHERE j.status = 'completed'
                  AND EXISTS (
                    SELECT 1 FROM artifacts AS a
                    WHERE a.job_id = j.id AND a.local_available = 1
                  )
                ORDER BY j.completed_at ASC
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def mark_job_expired(self, job_id: str) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] in {"running", "postprocessing", "cancelling"}:
                connection.execute("ROLLBACK")
                return False
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ?", (job_id,)
            ).fetchall()
            self._enqueue_remote_deletions(connection, artifacts)
            connection.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            connection.execute(
                """
                UPDATE jobs SET status = 'expired', phase = 'expired',
                    worker_pid = NULL, updated_at = ?
                WHERE id = ?
                """,
                (iso_now(), job_id),
            )
            connection.execute("COMMIT")
        return True

    def expire_analyses(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE analyses SET status = 'expired', result_json = NULL, updated_at = ?
                WHERE expires_at < ? AND status IN ('completed', 'failed')
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs
                    WHERE jobs.analysis_id = analyses.id
                      AND jobs.status IN ('queued', 'running', 'postprocessing', 'cancelling')
                  )
                """,
                (iso_now(), iso_now()),
            )
            return cursor.rowcount

    @staticmethod
    def _analysis_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        raw_result = item.pop("result_json")
        item["result"] = json.loads(raw_result) if raw_result else None
        item["playlist"] = bool(item["playlist"])
        return item

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["choice"] = json.loads(item.pop("choice_json"))
        item["request"] = json.loads(item.pop("request_json"))
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    @staticmethod
    def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["primary"] = bool(item.pop("is_primary"))
        item["local_available"] = bool(item.get("local_available", 1))
        return item


def status_is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
