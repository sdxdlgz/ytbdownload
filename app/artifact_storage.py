from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from app.config import Settings
from app.database import Database
from app.errors import AppError, WorkerCancelled

LOGGER = logging.getLogger("signal.storage")
_SAFE_SUFFIX = re.compile(r"^(?:\.[A-Za-z0-9_-]{1,12}){1,2}$")


@dataclass(frozen=True)
class UploadResult:
    bucket: str
    key: str
    etag: str | None
    version_id: str | None


class UploadCancellationCallback:
    """Abort boto transfers quickly without querying SQLite for every tiny callback."""

    def __init__(self, db: Database, job_id: str) -> None:
        self.db = db
        self.job_id = job_id
        self._last_check = 0.0
        self._lock = threading.Lock()

    def __call__(self, _bytes_transferred: int) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_check < 0.5:
                return
            self._last_check = now
        if self.db.is_cancel_requested(self.job_id):
            raise WorkerCancelled("任务已取消。")


class S3ArtifactStorage:
    """S3-compatible artifact publishing, delivery and deletion-outbox handling."""

    def __init__(self, settings: Settings, db: Database, *, client: Any | None = None) -> None:
        self.settings = settings
        self.db = db
        self._client = client
        self._client_lock = threading.Lock()
        self.transfer_config = TransferConfig(
            multipart_threshold=settings.s3_multipart_threshold_bytes,
            multipart_chunksize=settings.s3_multipart_chunksize_bytes,
            max_concurrency=settings.s3_max_concurrency,
            use_threads=settings.s3_max_concurrency > 1,
        )

    @property
    def enabled(self) -> bool:
        return self.settings.s3_enabled

    def publish_job_artifacts(
        self, job_id: str, artifacts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not self.enabled or not artifacts:
            return artifacts

        self.db.update_job_progress(job_id, phase="uploading:s3", progress=99.0)
        uploaded_ids: list[str] = []
        try:
            for artifact in artifacts:
                self._check_cancelled(job_id)
                path = self._local_artifact_path(artifact)
                object_key = self.object_key(job_id, artifact)
                staged = self.db.stage_artifact_upload(
                    artifact["id"],
                    job_id=job_id,
                    bucket=self.settings.s3_bucket,
                    object_key=object_key,
                )
                result = self._upload_one(path, staged, UploadCancellationCallback(self.db, job_id))
                self.db.record_artifact_upload_result(
                    artifact["id"], etag=result.etag, version_id=result.version_id
                )
                uploaded_ids.append(artifact["id"])

            self._check_cancelled(job_id)
            self.db.promote_job_artifacts_to_s3(
                job_id, uploaded_ids, keep_local=self.settings.s3_keep_local
            )
            if not self.settings.s3_keep_local:
                self._remove_local_copies(artifacts)
            return self.db.list_artifacts(job_id)
        except WorkerCancelled:
            raise
        except Exception as exc:
            LOGGER.warning(
                "S3 publish failed job=%s after=%s error_type=%s",
                job_id,
                len(uploaded_ids),
                type(exc).__name__,
            )
            if self.settings.s3_failure_mode == "fallback":
                self.db.fallback_job_artifacts_to_local(job_id)
                self.process_deletion_outbox(limit=max(50, len(artifacts)))
                self.db.update_job_progress(job_id, phase="finalizing", progress=99.0)
                return self.db.list_artifacts(job_id)
            raise AppError(
                "STORAGE_UPLOAD_FAILED",
                "文件已生成，但上传对象存储失败；服务器已撤销不完整产物。",
                status_code=502,
            ) from exc

    def object_key(self, job_id: str, artifact: dict[str, Any]) -> str:
        suffix = "".join(Path(str(artifact["relpath"])).suffixes[-2:]).lower()
        if not _SAFE_SUFFIX.fullmatch(suffix):
            suffix = ".bin"
        leaf = f"{artifact['id']}{suffix}"
        prefix = self.settings.s3_prefix
        return f"{prefix}/{job_id}/{leaf}" if prefix else f"{job_id}/{leaf}"

    def presign(
        self,
        artifact: dict[str, Any],
        *,
        method: Literal["GET", "HEAD"],
        ttl_seconds: int,
    ) -> str:
        if not self.enabled:
            raise AppError("STORAGE_UNAVAILABLE", "对象存储当前未配置。", status_code=503)
        if artifact.get("storage_backend") != "s3" or artifact.get("storage_state") != "ready":
            raise AppError("ARTIFACT_NOT_READY", "对象存储文件尚未就绪。", status_code=409)
        bucket = artifact.get("object_bucket")
        object_key = artifact.get("object_key")
        if not bucket or not object_key:
            raise AppError("ARTIFACT_NOT_READY", "对象存储位置不完整。", status_code=500)
        operation = "head_object" if method == "HEAD" else "get_object"
        params: dict[str, Any] = {"Bucket": bucket, "Key": object_key}
        if artifact.get("object_version_id"):
            params["VersionId"] = artifact["object_version_id"]
        ttl = max(1, min(int(ttl_seconds), self.settings.s3_presign_ttl_seconds, 604800))
        try:
            return str(
                self.client.generate_presigned_url(
                    operation,
                    Params=params,
                    ExpiresIn=ttl,
                    HttpMethod=method,
                )
            )
        except Exception as exc:
            LOGGER.warning("S3 presign failed artifact=%s", artifact.get("id"))
            raise AppError(
                "STORAGE_UNAVAILABLE", "暂时无法生成对象存储下载地址。", status_code=503
            ) from exc

    def process_deletion_outbox(self, *, limit: int = 50) -> dict[str, int]:
        if self.db.pending_storage_deletion_count() == 0:
            return {"deleted": 0, "failed": 0, "pending": 0}
        if self.settings.s3_delete_on_expiry and not self.enabled:
            return {
                "deleted": 0,
                "failed": 0,
                "pending": self.db.pending_storage_deletion_count(),
            }

        rows = self.db.claim_storage_deletions(limit=limit)
        deleted = 0
        failed = 0
        for row in rows:
            if not self.settings.s3_delete_on_expiry:
                self.db.acknowledge_storage_deletion(row["id"])
                deleted += 1
                continue
            try:
                params: dict[str, Any] = {
                    "Bucket": row["bucket"],
                    "Key": row["object_key"],
                }
                if row.get("version_id"):
                    params["VersionId"] = row["version_id"]
                self.client.delete_object(**params)
                self.db.acknowledge_storage_deletion(row["id"])
                deleted += 1
            except Exception as exc:
                failed += 1
                attempts = int(row.get("attempts") or 0) + 1
                delay = min(3600, 5 * (2 ** min(attempts, 9)))
                self.db.fail_storage_deletion(
                    row["id"],
                    message=type(exc).__name__,
                    retry_after_seconds=delay,
                )
                LOGGER.warning("S3 deletion failed id=%s attempt=%s", row["id"], attempts)
        return {
            "deleted": deleted,
            "failed": failed,
            "pending": self.db.pending_storage_deletion_count(),
        }

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "ok": True}
        if not self.settings.s3_healthcheck:
            return {
                "enabled": True,
                "ok": True,
                "checked": False,
                "keep_local": self.settings.s3_keep_local,
            }
        try:
            self.client.head_bucket(Bucket=self.settings.s3_bucket)
            return {
                "enabled": True,
                "ok": True,
                "checked": True,
                "keep_local": self.settings.s3_keep_local,
            }
        except Exception as exc:
            return {
                "enabled": True,
                "ok": False,
                "checked": True,
                "error": type(exc).__name__,
                "keep_local": self.settings.s3_keep_local,
            }

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        session_kwargs: dict[str, Any] = {}
        access_key = (
            self.settings.s3_access_key_id.get_secret_value()
            if self.settings.s3_access_key_id
            else ""
        )
        secret_key = (
            self.settings.s3_secret_access_key.get_secret_value()
            if self.settings.s3_secret_access_key
            else ""
        )
        session_token = (
            self.settings.s3_session_token.get_secret_value()
            if self.settings.s3_session_token
            else ""
        )
        if access_key:
            session_kwargs["aws_access_key_id"] = access_key
        if secret_key:
            session_kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            session_kwargs["aws_session_token"] = session_token
        if self.settings.s3_region:
            session_kwargs["region_name"] = self.settings.s3_region

        verify: bool | str = self.settings.s3_verify_tls
        if self.settings.s3_ca_bundle:
            verify = str(self.settings.s3_ca_bundle)
        session = boto3.Session(**session_kwargs)
        return session.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url or None,
            verify=verify,
            config=Config(
                signature_version="s3v4",
                connect_timeout=self.settings.s3_connect_timeout_seconds,
                read_timeout=self.settings.s3_read_timeout_seconds,
                max_pool_connections=self.settings.s3_max_concurrency + 2,
                retries={
                    "mode": "standard",
                    "max_attempts": self.settings.s3_retry_attempts,
                },
                s3={"addressing_style": self.settings.s3_addressing_style},
            ),
        )

    def _upload_one(
        self, path: Path, artifact: dict[str, Any], callback: UploadCancellationCallback
    ) -> UploadResult:
        bucket = str(artifact["object_bucket"])
        object_key = str(artifact["object_key"])
        extra_args: dict[str, Any] = {
            "ContentType": artifact["media_type"],
            "ContentDisposition": attachment_content_disposition(artifact["filename"]),
            "Metadata": {
                "sha256": artifact["sha256"],
                "artifact-id": artifact["id"],
            },
        }
        if self.settings.s3_storage_class:
            extra_args["StorageClass"] = self.settings.s3_storage_class
        if self.settings.s3_server_side_encryption:
            extra_args["ServerSideEncryption"] = self.settings.s3_server_side_encryption
        if self.settings.s3_server_side_encryption == "aws:kms" and self.settings.s3_kms_key_id:
            extra_args["SSEKMSKeyId"] = self.settings.s3_kms_key_id.get_secret_value()

        self.client.upload_file(
            str(path),
            bucket,
            object_key,
            ExtraArgs=extra_args,
            Callback=callback,
            Config=self.transfer_config,
        )
        head = self.client.head_object(Bucket=bucket, Key=object_key)
        if int(head.get("ContentLength", -1)) != int(artifact["size"]):
            raise RuntimeError("S3 object size verification failed")
        metadata = {
            str(key).lower(): str(value) for key, value in (head.get("Metadata") or {}).items()
        }
        if metadata.get("sha256") and metadata["sha256"] != artifact["sha256"]:
            raise RuntimeError("S3 object SHA-256 metadata verification failed")
        etag = str(head["ETag"]).strip('"') if head.get("ETag") else None
        version_id = str(head["VersionId"]) if head.get("VersionId") else None
        return UploadResult(bucket=bucket, key=object_key, etag=etag, version_id=version_id)

    def _local_artifact_path(self, artifact: dict[str, Any]) -> Path:
        relative = Path(str(artifact["relpath"]))
        path = (self.settings.artifacts_dir / relative).resolve()
        root = self.settings.artifacts_dir.resolve()
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise AppError("NO_ARTIFACT", "待上传文件不存在或路径不安全。", status_code=500)
        return path

    def _remove_local_copies(self, artifacts: list[dict[str, Any]]) -> None:
        directories: set[Path] = set()
        for artifact in artifacts:
            path = self._local_artifact_path(artifact)
            directories.add(path.parent)
            path.unlink()
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            with suppress(OSError):
                directory.rmdir()

    def _check_cancelled(self, job_id: str) -> None:
        if self.db.is_cancel_requested(job_id):
            raise WorkerCancelled("任务已取消。")


def attachment_content_disposition(filename: str) -> str:
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in '\\"' else "_" for char in filename
    )
    fallback = fallback[:150] or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
