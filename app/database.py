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
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    UNIQUE(job_id, relpath)
                );

                CREATE INDEX IF NOT EXISTS idx_artifacts_job
                    ON artifacts(job_id, is_primary DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_artifacts_expiry
                    ON artifacts(expires_at);
                """
            )

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
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            analysis = connection.execute(
                "SELECT id, created_at FROM analyses WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            job = connection.execute(
                "SELECT id, created_at FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            candidate: tuple[str, sqlite3.Row] | None = None
            if analysis and job:
                candidate = (
                    "analysis",
                    analysis if analysis["created_at"] <= job["created_at"] else job,
                )
                if candidate[1] is job:
                    candidate = ("job", job)
            elif analysis:
                candidate = ("analysis", analysis)
            elif job:
                candidate = ("job", job)
            if not candidate:
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
            connection.execute(
                "UPDATE artifacts SET expires_at = ? WHERE job_id = ?", (expiry, job_id)
            )
            connection.execute(
                """
                UPDATE jobs SET status = 'completed', phase = 'ready', progress = 100,
                    error_code = NULL, error_message = NULL, worker_pid = NULL,
                    updated_at = ?, completed_at = ?, expires_at = ?
                WHERE id = ? AND status IN ('running', 'postprocessing')
                """,
                (now, now, expiry, job_id),
            )
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

    def clear_artifacts(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))

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

    def oldest_completed_jobs(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs WHERE status = 'completed'
                ORDER BY completed_at ASC
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def mark_job_expired(self, job_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))
            connection.execute(
                """
                UPDATE jobs SET status = 'expired', phase = 'expired',
                    worker_pid = NULL, updated_at = ?
                WHERE id = ? AND status NOT IN ('running', 'postprocessing', 'cancelling')
                """,
                (iso_now(), job_id),
            )
            connection.execute("COMMIT")

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
        return item


def status_is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
