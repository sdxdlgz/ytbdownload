from __future__ import annotations

import argparse
import signal
import traceback

from app.config import get_settings
from app.database import Database
from app.errors import AppError, ResourceLimitExceeded, WorkerCancelled
from app.yt_service import YtDlpService, safe_rmtree


def _raise_cancelled(_signum: int, _frame: object) -> None:
    raise WorkerCancelled("任务已取消。")


def run_analysis(operation_id: str, service: YtDlpService, db: Database) -> int:
    record = db.get_analysis(operation_id)
    if not record or record["status"] != "running":
        return 2
    try:
        result = service.analyze(record["request_url"], playlist=record["playlist"])
        db.complete_analysis(operation_id, result)
        return 0
    except AppError as exc:
        db.fail_analysis(operation_id, exc.code, exc.message)
        return 1
    except WorkerCancelled:
        db.fail_analysis(operation_id, "CANCELLED", "分析任务已取消。")
        return 1
    except Exception:
        traceback.print_exc()
        db.fail_analysis(operation_id, "INTERNAL_ERROR", "分析任务异常退出，请稍后重试。")
        return 1


def run_job(operation_id: str, service: YtDlpService, db: Database) -> int:
    settings = service.settings
    context = db.load_job_context(operation_id)
    if not context or context["status"] not in {"running", "cancelling"}:
        return 2
    try:
        service.download(context)
        if db.is_cancel_requested(operation_id):
            raise WorkerCancelled("任务已取消。")
        db.complete_job(operation_id, ttl_hours=settings.artifact_ttl_hours)
        return 0
    except WorkerCancelled:
        _cleanup_job(operation_id, service, db)
        db.fail_job(operation_id, "CANCELLED", "任务已取消。", cancelled=True)
        return 1
    except ResourceLimitExceeded as exc:
        _cleanup_job(operation_id, service, db)
        db.fail_job(operation_id, "SIZE_LIMIT", str(exc))
        return 1
    except AppError as exc:
        _cleanup_job(operation_id, service, db)
        db.fail_job(operation_id, exc.code, exc.message)
        return 1
    except Exception:
        traceback.print_exc()
        _cleanup_job(operation_id, service, db)
        db.fail_job(operation_id, "INTERNAL_ERROR", "下载任务异常退出，请稍后重试。")
        return 1


def _cleanup_job(job_id: str, service: YtDlpService, db: Database) -> None:
    settings = service.settings
    try:
        db.revoke_job_artifacts(job_id)
    except Exception:
        traceback.print_exc()
    try:
        service.artifact_storage.process_deletion_outbox(limit=100)
    except Exception:
        traceback.print_exc()
    for path, root in (
        (settings.work_dir / job_id, settings.work_dir),
        (settings.artifacts_dir / job_id, settings.artifacts_dir),
    ):
        try:
            safe_rmtree(path, root)
        except (OSError, RuntimeError):
            traceback.print_exc()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal yt-dlp web worker")
    parser.add_argument("kind", choices=("analysis", "job"))
    parser.add_argument("operation_id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    signal.signal(signal.SIGTERM, _raise_cancelled)
    signal.signal(signal.SIGINT, _raise_cancelled)
    settings = get_settings()
    database = Database(settings.database_path)
    database.initialize()
    service = YtDlpService(settings, database)
    if args.kind == "analysis":
        code = run_analysis(args.operation_id, service, database)
    else:
        code = run_job(args.operation_id, service, database)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
