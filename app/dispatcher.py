from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from app.config import Settings
from app.database import Database, status_is_terminal
from app.yt_service import safe_rmtree


@dataclass
class RunningOperation:
    kind: str
    operation_id: str
    process: asyncio.subprocess.Process
    log_handle: IO[bytes]
    started_monotonic: float
    terminate_sent_at: float | None = None
    timed_out: bool = False


class Dispatcher:
    """Lease queued SQLite operations and supervise isolated worker process groups."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.running: dict[tuple[str, str], RunningOperation] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_cleanup = 0.0
        self.project_root = Path(__file__).resolve().parents[1]

    async def start(self) -> None:
        interrupted = self.db.reconcile_interrupted()
        for kind, operation_id in interrupted:
            if kind == "job":
                self._remove_job_directories(operation_id)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="yt-dlp-dispatcher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._task,
                    timeout=self.settings.cancel_grace_seconds + 5,
                )
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        self._task = None

    async def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._monitor_running()
                await self._fill_available_slots()
                now = time.monotonic()
                if now - self._last_cleanup >= self.settings.cleanup_interval_seconds:
                    await asyncio.to_thread(self.cleanup)
                    self._last_cleanup = now
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.settings.worker_poll_seconds
                    )
        finally:
            await self._terminate_all()

    async def _fill_available_slots(self) -> None:
        while len(self.running) < self.settings.max_concurrent_operations:
            claimed = await asyncio.to_thread(self.db.claim_next_operation)
            if not claimed:
                return
            kind, operation_id = claimed
            try:
                await self._spawn(kind, operation_id)
            except Exception:
                if kind == "analysis":
                    self.db.fail_analysis(
                        operation_id, "WORKER_START_FAILED", "无法启动分析进程，请检查服务器配置。"
                    )
                else:
                    self.db.fail_job(
                        operation_id, "WORKER_START_FAILED", "无法启动下载进程，请检查服务器配置。"
                    )

    async def _spawn(self, kind: str, operation_id: str) -> None:
        log_path = self.settings.logs_dir / f"{kind}-{operation_id}.log"
        log_handle = log_path.open("ab", buffering=0)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.worker",
                kind,
                operation_id,
                cwd=self.project_root,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=os.name != "nt",
            )
        except Exception:
            log_handle.close()
            raise
        operation = RunningOperation(
            kind=kind,
            operation_id=operation_id,
            process=process,
            log_handle=log_handle,
            started_monotonic=time.monotonic(),
        )
        self.running[(kind, operation_id)] = operation
        await asyncio.to_thread(self.db.set_worker_pid, kind, operation_id, process.pid)

    async def _monitor_running(self) -> None:
        now = time.monotonic()
        for key, operation in list(self.running.items()):
            state = await asyncio.to_thread(
                self.db.operation_state, operation.kind, operation.operation_id
            )
            if operation.process.returncode is not None:
                await self._finish_operation(key, operation, state)
                continue

            should_cancel = bool(
                operation.kind == "job" and state and state.get("cancel_requested")
            )
            timeout = (
                self.settings.analysis_timeout_seconds
                if operation.kind == "analysis"
                else self.settings.download_timeout_seconds
            )
            timed_out = now - operation.started_monotonic > timeout
            if (should_cancel or timed_out) and operation.terminate_sent_at is None:
                operation.timed_out = timed_out
                self._signal_process_group(operation.process, signal.SIGTERM)
                operation.terminate_sent_at = now
            elif (
                operation.terminate_sent_at is not None
                and now - operation.terminate_sent_at > self.settings.cancel_grace_seconds
            ):
                self._signal_process_group(operation.process, signal.SIGKILL)

    async def _finish_operation(
        self,
        key: tuple[str, str],
        operation: RunningOperation,
        state: dict[str, object] | None,
    ) -> None:
        await operation.process.wait()
        operation.log_handle.close()
        self.running.pop(key, None)
        status = str(state.get("status")) if state else "missing"
        if status_is_terminal(status):
            return
        if operation.kind == "analysis":
            code = "TIME_LIMIT" if operation.timed_out else "WORKER_EXITED"
            message = "分析超时，已由服务器终止。" if operation.timed_out else "分析进程异常退出。"
            await asyncio.to_thread(self.db.fail_analysis, operation.operation_id, code, message)
        else:
            cancelled = bool(state and state.get("cancel_requested"))
            if cancelled:
                await asyncio.to_thread(
                    self.db.fail_job,
                    operation.operation_id,
                    "CANCELLED",
                    "任务已取消。",
                    cancelled=True,
                )
            else:
                code = "TIME_LIMIT" if operation.timed_out else "WORKER_EXITED"
                message = (
                    "下载超时，已由服务器终止。" if operation.timed_out else "下载进程异常退出。"
                )
                await asyncio.to_thread(
                    self.db.fail_job,
                    operation.operation_id,
                    code,
                    message,
                )
            await asyncio.to_thread(self._remove_job_directories, operation.operation_id)
            await asyncio.to_thread(self.db.clear_artifacts, operation.operation_id)

    async def _terminate_all(self) -> None:
        if not self.running:
            return
        for operation in self.running.values():
            self._signal_process_group(operation.process, signal.SIGTERM)
        deadline = time.monotonic() + self.settings.cancel_grace_seconds
        while self.running and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            await self._monitor_running()
        for operation in self.running.values():
            self._signal_process_group(operation.process, signal.SIGKILL)
        for operation in list(self.running.values()):
            with contextlib.suppress(Exception):
                await operation.process.wait()
            operation.log_handle.close()
            if operation.kind == "analysis":
                self.db.fail_analysis(
                    operation.operation_id, "SERVER_SHUTDOWN", "服务关闭，分析任务已中止。"
                )
            else:
                self.db.fail_job(
                    operation.operation_id, "SERVER_SHUTDOWN", "服务关闭，下载任务已中止。"
                )
                self._remove_job_directories(operation.operation_id)
                self.db.clear_artifacts(operation.operation_id)
        self.running.clear()

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return

    def cleanup(self) -> None:
        for job_id in self.db.expired_jobs():
            self.db.mark_job_expired(job_id)
            self._remove_job_directories(job_id)

        total_size = directory_size(self.settings.artifacts_dir)
        if total_size > self.settings.max_storage_bytes:
            for job_id in self.db.oldest_completed_jobs():
                path = self.settings.artifacts_dir / job_id
                job_size = directory_size(path)
                self.db.mark_job_expired(job_id)
                self._remove_job_directories(job_id)
                total_size = max(0, total_size - job_size)
                if total_size <= int(self.settings.max_storage_bytes * 0.9):
                    break

        self.db.expire_analyses()
        cutoff = time.time() - max(86400, self.settings.artifact_ttl_hours * 7200)
        for log_file in self.settings.logs_dir.glob("*.log"):
            with contextlib.suppress(OSError):
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()

    def purge_job(self, job_id: str) -> None:
        self.db.mark_job_expired(job_id)
        self._remove_job_directories(job_id)

    def _remove_job_directories(self, job_id: str) -> None:
        for path, root in (
            (self.settings.work_dir / job_id, self.settings.work_dir),
            (self.settings.artifacts_dir / job_id, self.settings.artifacts_dir),
        ):
            with contextlib.suppress(OSError, RuntimeError):
                safe_rmtree(path, root)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total
