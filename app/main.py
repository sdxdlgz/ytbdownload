from __future__ import annotations

import logging
import platform
import shutil
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID, uuid4

import yt_dlp
from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.database import Database
from app.dispatcher import Dispatcher, directory_size
from app.errors import AppError
from app.models import (
    AnalysisCreateRequest,
    AnalysisPublic,
    HealthPublic,
    JobCreateRequest,
    JobPublic,
    LoginRequest,
    PaginatedJobs,
    SessionPublic,
)
from app.security import (
    SESSION_COOKIE,
    SecurityHeadersMiddleware,
    SessionManager,
    SlidingWindowRateLimiter,
    URLValidator,
    get_client_key,
    require_principal,
    set_session_cookie,
    validate_same_origin,
)

APP_VERSION = "1.0.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
LOGGER = logging.getLogger("signal.web")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    dispatcher = Dispatcher(settings, database)
    session_manager = SessionManager(settings)
    limiter = SlidingWindowRateLimiter()
    url_validator = URLValidator(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await dispatcher.start()
        try:
            yield
        finally:
            await dispatcher.stop()

    app = FastAPI(
        title=settings.app_name,
        version=APP_VERSION,
        description="Self-hosted multi-platform media downloader powered by yt-dlp.",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.dispatcher = dispatcher
    app.state.session_manager = session_manager
    app.state.rate_limiter = limiter
    app.state.url_validator = url_validator

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        try:
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                validate_same_origin(request)
            if request.url.path.startswith("/api/"):
                key = get_client_key(request, settings)
                limiter.check(
                    key,
                    "api",
                    limit=settings.rate_limit_requests,
                    window=settings.rate_limit_window_seconds,
                )
            response = await call_next(request)
        except AppError as exc:
            response = app_error_response(request, exc)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return app_error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = []
        for error in exc.errors()[:12]:
            details.append(
                {
                    "field": ".".join(str(part) for part in error.get("loc", [])[1:]),
                    "message": error.get("msg", "invalid value"),
                    "type": error.get("type", "validation_error"),
                }
            )
        return app_error_response(
            request,
            AppError(
                "VALIDATION_ERROR",
                "请求参数无效。",
                status_code=422,
                details={"fields": details},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
            return app_error_response(
                request,
                AppError(code, "请求的资源不存在。", status_code=exc.status_code),
            )
        return HTMLResponse("Not found", status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.error(
            "Unhandled request error id=%s path=%s",
            getattr(request.state, "request_id", "unknown"),
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return app_error_response(
            request,
            AppError("INTERNAL_ERROR", "服务器发生意外错误。", status_code=500),
        )

    @app.get("/api/v1/health/live", response_model=HealthPublic, tags=["health"])
    async def health_live() -> dict[str, Any]:
        return {"status": "ok", "version": APP_VERSION, "checks": {"process": "ok"}}

    @app.get("/api/v1/health/ready", response_model=HealthPublic, tags=["health"])
    async def health_ready(response: Response) -> dict[str, Any]:
        checks = readiness_checks(settings, database)
        essential_ok = all(checks[key]["ok"] for key in ("database", "storage", "ffmpeg"))
        if not essential_ok:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ok" if essential_ok and checks["javascript_runtime"]["ok"] else "degraded",
            "version": APP_VERSION,
            "checks": checks,
        }

    @app.get("/api/v1/config", tags=["system"])
    async def public_config() -> dict[str, Any]:
        capabilities = runtime_capabilities(settings)
        return {
            "name": settings.app_name,
            "version": APP_VERSION,
            "auth_required": settings.auth_enabled,
            "features": {
                "video": True,
                "audio": ["mp3", "m4a", "opus"],
                "thumbnail": ["original", "jpg", "png"],
                "playlists": True,
                "subtitles": True,
                "cancellation": True,
                "range_downloads": True,
            },
            "limits": {
                "max_filesize_mb": settings.max_filesize_mb,
                "max_duration_seconds": settings.max_duration_seconds,
                "max_playlist_items": settings.max_playlist_items,
                "artifact_ttl_hours": settings.artifact_ttl_hours,
                "max_concurrent_operations": settings.max_concurrent_operations,
            },
            "capabilities": capabilities,
            "featured_platforms": [
                "YouTube",
                "Bilibili",
                "TikTok",
                "Instagram",
                "X / Twitter",
                "Vimeo",
                "SoundCloud",
                "Twitch",
                "Facebook",
                "Reddit",
                "以及 yt-dlp 支持的其他站点",
            ],
        }

    @app.get("/api/v1/session", response_model=SessionPublic, tags=["auth"])
    async def session_status(request: Request) -> dict[str, bool]:
        return {
            "auth_required": settings.auth_enabled,
            "authenticated": session_manager.request_authenticated(request),
        }

    @app.post("/api/v1/auth/session", response_model=SessionPublic, tags=["auth"])
    async def login(request: Request, body: LoginRequest, response: Response) -> dict[str, bool]:
        key = get_client_key(request, settings)
        limiter.check(key, "login", limit=10, window=300)
        if not session_manager.validate_access_token(body.token):
            raise AppError("INVALID_TOKEN", "访问令牌不正确。", status_code=401)
        if settings.auth_enabled:
            set_session_cookie(response, session_manager)
        return {"auth_required": settings.auth_enabled, "authenticated": True}

    @app.delete("/api/v1/auth/session", response_model=SessionPublic, tags=["auth"])
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return {"auth_required": settings.auth_enabled, "authenticated": not settings.auth_enabled}

    @app.post(
        "/api/v1/analyses",
        response_model=AnalysisPublic,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["analysis"],
    )
    async def create_analysis(
        request: Request,
        body: AnalysisCreateRequest,
        principal: Annotated[str, Depends(require_principal)],
    ) -> dict[str, Any]:
        key = f"{principal}:{get_client_key(request, settings)}"
        limiter.check(
            key,
            "analysis",
            limit=settings.analysis_rate_limit,
            window=settings.rate_limit_window_seconds,
        )
        ensure_queue_capacity(database, settings)
        validated = await url_validator.validate(body.url)
        record = await asyncio_to_thread(
            database.create_analysis,
            owner=principal,
            request_url=validated.url,
            url_hash=validated.digest,
            playlist=body.playlist,
            ttl_minutes=settings.analysis_ttl_minutes,
        )
        return analysis_public(record, request.state.request_id)

    @app.get("/api/v1/analyses/{analysis_id}", response_model=AnalysisPublic, tags=["analysis"])
    async def get_analysis(
        request: Request,
        analysis_id: UUID,
        principal: Annotated[str, Depends(require_principal)],
    ) -> dict[str, Any]:
        record = await asyncio_to_thread(database.get_analysis, str(analysis_id), owner=principal)
        if not record:
            raise AppError("NOT_FOUND", "分析任务不存在。", status_code=404)
        return analysis_public(record, request.state.request_id)

    @app.post(
        "/api/v1/jobs",
        response_model=JobPublic,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["downloads"],
    )
    async def create_job(
        request: Request,
        response: Response,
        body: JobCreateRequest,
        principal: Annotated[str, Depends(require_principal)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        key = f"{principal}:{get_client_key(request, settings)}"
        limiter.check(
            key,
            "jobs",
            limit=settings.job_rate_limit,
            window=settings.rate_limit_window_seconds,
        )
        if idempotency_key:
            idempotency_key = idempotency_key.strip()
            if not (8 <= len(idempotency_key) <= 128) or any(
                ord(char) < 33 for char in idempotency_key
            ):
                raise AppError(
                    "INVALID_IDEMPOTENCY_KEY",
                    "Idempotency-Key 必须为 8-128 个可见字符。",
                )
        if idempotency_key:
            replay = await asyncio_to_thread(
                database.get_job_by_idempotency, principal, idempotency_key
            )
            if replay:
                response.headers["Idempotent-Replay"] = "true"
                response.headers["Location"] = f"/api/v1/jobs/{replay['id']}"
                return job_public(replay, request.state.request_id)
        ensure_queue_capacity(database, settings)
        analysis = await asyncio_to_thread(
            database.get_analysis, str(body.analysis_id), owner=principal
        )
        if not analysis:
            raise AppError("NOT_FOUND", "分析任务不存在。", status_code=404)
        if analysis["status"] != "completed" or not analysis.get("result"):
            if analysis["status"] in {"queued", "running"}:
                raise AppError("ANALYSIS_PENDING", "媒体分析尚未完成。", status_code=409)
            raise AppError(
                "ANALYSIS_UNAVAILABLE", "媒体分析已失败或过期，请重新分析。", status_code=409
            )
        result = analysis["result"]
        choice = next(
            (item for item in result.get("choices", []) if item.get("id") == str(body.choice_id)),
            None,
        )
        if not choice:
            raise AppError(
                "INVALID_CHOICE", "下载选项不存在或已过期，请重新分析。", status_code=409
            )
        if (
            choice.get("expected_size")
            and int(choice["expected_size"]) > settings.max_filesize_bytes
        ):
            raise AppError("SIZE_LIMIT", "所选格式预计超过服务器文件大小上限。", status_code=422)
        validate_subtitle_request(body, result, choice)
        record, created = await asyncio_to_thread(
            database.create_job,
            owner=principal,
            analysis_id=str(body.analysis_id),
            choice=choice,
            request=body.model_dump(mode="json"),
            title=result.get("title"),
            platform=result.get("platform"),
            idempotency_key=idempotency_key,
            ttl_hours=settings.artifact_ttl_hours,
        )
        if not created:
            response.headers["Idempotent-Replay"] = "true"
        response.headers["Location"] = f"/api/v1/jobs/{record['id']}"
        return job_public(record, request.state.request_id)

    @app.get("/api/v1/jobs", response_model=PaginatedJobs, tags=["downloads"])
    async def list_jobs(
        request: Request,
        principal: Annotated[str, Depends(require_principal)],
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> dict[str, Any]:
        items, total = await asyncio_to_thread(
            database.list_jobs, principal, limit=limit, offset=offset
        )
        return {
            "items": [job_public(item, request.state.request_id) for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/jobs/{job_id}", response_model=JobPublic, tags=["downloads"])
    async def get_job(
        request: Request,
        job_id: UUID,
        principal: Annotated[str, Depends(require_principal)],
    ) -> dict[str, Any]:
        record = await asyncio_to_thread(database.get_job, str(job_id), owner=principal)
        if not record:
            raise AppError("NOT_FOUND", "下载任务不存在。", status_code=404)
        return job_public(record, request.state.request_id)

    @app.delete("/api/v1/jobs/{job_id}", response_model=JobPublic, tags=["downloads"])
    async def cancel_or_purge_job(
        request: Request,
        job_id: UUID,
        principal: Annotated[str, Depends(require_principal)],
    ) -> dict[str, Any]:
        record = await asyncio_to_thread(database.get_job, str(job_id), owner=principal)
        if not record:
            raise AppError("NOT_FOUND", "下载任务不存在。", status_code=404)
        if record["status"] in {"queued", "running", "postprocessing", "cancelling"}:
            updated = await asyncio_to_thread(database.request_cancel, str(job_id), principal)
        elif record["status"] != "expired":
            await asyncio_to_thread(dispatcher.purge_job, str(job_id))
            updated = await asyncio_to_thread(database.get_job, str(job_id), owner=principal)
        else:
            updated = record
        return job_public(updated, request.state.request_id)

    @app.api_route("/api/v1/artifacts/{artifact_id}", methods=["GET", "HEAD"], tags=["downloads"])
    async def download_artifact(
        artifact_id: UUID,
        principal: Annotated[str, Depends(require_principal)],
    ) -> Response:
        artifact = await asyncio_to_thread(database.get_artifact, str(artifact_id), owner=principal)
        if not artifact or artifact.get("job_status") != "completed":
            raise AppError("NOT_FOUND", "下载文件不存在或已过期。", status_code=404)
        if datetime.fromisoformat(artifact["expires_at"]) <= datetime.now(UTC):
            raise AppError("ARTIFACT_EXPIRED", "下载文件已过期。", status_code=410)
        relative = Path(artifact["relpath"])
        file_path = (settings.artifacts_dir / relative).resolve()
        artifact_root = settings.artifacts_dir.resolve()
        if (
            not file_path.is_relative_to(artifact_root)
            or file_path.is_symlink()
            or not file_path.is_file()
        ):
            raise AppError("NOT_FOUND", "下载文件不存在或已过期。", status_code=404)
        headers = {
            "Cache-Control": "private, no-store",
            "X-Artifact-SHA256": artifact["sha256"],
            "Accept-Ranges": "bytes",
        }
        if settings.x_accel_redirect:
            internal_uri = f"{settings.x_accel_prefix.rstrip('/')}/{quote(relative.as_posix())}"
            headers["X-Accel-Redirect"] = internal_uri
            headers["Content-Disposition"] = content_disposition(artifact["filename"])
            headers["Content-Type"] = artifact["media_type"]
            return Response(headers=headers)
        return FileResponse(
            file_path,
            media_type=artifact["media_type"],
            filename=artifact["filename"],
            headers=headers,
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        if INDEX_FILE.is_file():
            return FileResponse(INDEX_FILE, media_type="text/html")
        return HTMLResponse(
            "<h1>Signal / yt-dlp Web</h1><p>Frontend assets are missing.</p>", status_code=503
        )

    return app


def app_error_response(request: Request, exc: AppError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    headers: dict[str, str] = {}
    if exc.status_code == 429 and exc.details.get("retry_after"):
        headers["Retry-After"] = str(exc.details["retry_after"])
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


def analysis_public(record: dict[str, Any], request_id: str) -> dict[str, Any]:
    error = None
    if record.get("error_code"):
        error = {
            "code": record["error_code"],
            "message": record.get("error_message") or "分析失败。",
            "request_id": request_id,
            "details": {},
        }
    return {
        "id": record["id"],
        "status": record["status"],
        "playlist": bool(record["playlist"]),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "expires_at": record["expires_at"],
        "result": record.get("result"),
        "error": error,
    }


def job_public(record: dict[str, Any], request_id: str) -> dict[str, Any]:
    error = None
    if record.get("error_code"):
        error = {
            "code": record["error_code"],
            "message": record.get("error_message") or "下载失败。",
            "request_id": request_id,
            "details": {},
        }
    artifacts = []
    for artifact in record.get("artifacts") or []:
        artifacts.append(
            {
                "id": artifact["id"],
                "filename": artifact["filename"],
                "size": artifact["size"],
                "media_type": artifact["media_type"],
                "sha256": artifact["sha256"],
                "primary": bool(artifact["primary"]),
                "created_at": artifact["created_at"],
                "expires_at": artifact["expires_at"],
                "download_url": f"/api/v1/artifacts/{artifact['id']}",
            }
        )
    return {
        "id": record["id"],
        "analysis_id": record["analysis_id"],
        "status": record["status"],
        "phase": record.get("phase") or record["status"],
        "progress": min(100.0, max(0.0, float(record.get("progress") or 0))),
        "downloaded_bytes": record.get("downloaded_bytes"),
        "total_bytes": record.get("total_bytes"),
        "speed": record.get("speed"),
        "eta": record.get("eta"),
        "playlist_index": record.get("playlist_index"),
        "playlist_count": record.get("playlist_count"),
        "title": record.get("title"),
        "platform": record.get("platform"),
        "choice": record.get("choice"),
        "artifacts": artifacts,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "started_at": record.get("started_at"),
        "completed_at": record.get("completed_at"),
        "expires_at": record["expires_at"],
        "cancel_requested": bool(record.get("cancel_requested")),
        "error": error,
    }


def validate_subtitle_request(
    body: JobCreateRequest, analysis: dict[str, Any], choice: dict[str, Any]
) -> None:
    if not body.subtitle_languages and not body.include_auto_subtitles:
        return
    if choice.get("kind") != "video" or analysis.get("kind") != "single":
        raise AppError("INVALID_SUBTITLE_OPTIONS", "字幕选项仅适用于单个视频下载。")
    available = {
        item.get("code")
        for item in analysis.get("subtitles") or []
        if item.get("kind") == "manual" or body.include_auto_subtitles
    }
    invalid = [code for code in body.subtitle_languages if code not in available]
    if invalid:
        raise AppError(
            "INVALID_SUBTITLE_OPTIONS",
            "所选字幕已不可用，请重新分析。",
            status_code=409,
            details={"languages": invalid},
        )


def ensure_queue_capacity(database: Database, settings: Settings) -> None:
    if database.queued_operation_count() >= settings.max_queued_operations:
        raise AppError(
            "QUEUE_FULL",
            "服务器任务队列已满，请稍后重试。",
            status_code=503,
            details={"max_queue": settings.max_queued_operations},
        )


def runtime_capabilities(settings: Settings) -> dict[str, Any]:
    return _runtime_capabilities(settings.js_runtime)


@lru_cache(maxsize=8)
def _runtime_capabilities(js_runtime: str) -> dict[str, Any]:
    with suppress(Exception):
        from yt_dlp.extractor import gen_extractor_classes

        extractor_count = len(gen_extractor_classes())
    if "extractor_count" not in locals():
        extractor_count = None
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "extractor_count": extractor_count,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "javascript_runtime": {
            "name": js_runtime or None,
            "available": bool(js_runtime and shutil.which(js_runtime)),
        },
        "python": platform.python_version(),
    }


def readiness_checks(settings: Settings, database: Database) -> dict[str, Any]:
    try:
        db_ok = database.ping()
    except Exception:
        db_ok = False
    storage_ok = False
    probe = settings.data_dir / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        storage_ok = shutil.disk_usage(settings.data_dir).free >= settings.min_free_disk_bytes
    except OSError:
        pass
    runtime = settings.js_runtime
    return {
        "database": {"ok": db_ok},
        "storage": {
            "ok": storage_ok,
            "used_bytes": directory_size(settings.artifacts_dir),
            "limit_bytes": settings.max_storage_bytes,
        },
        "ffmpeg": {"ok": shutil.which("ffmpeg") is not None},
        "javascript_runtime": {
            "ok": bool(runtime and shutil.which(runtime)),
            "name": runtime or None,
            "recommended": True,
        },
        "yt_dlp": {"ok": True, "version": yt_dlp.version.__version__},
    }


def content_disposition(filename: str) -> str:
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in '\\"' else "_" for char in filename
    )
    fallback = fallback[:150] or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


async def asyncio_to_thread(function, /, *args, **kwargs):
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)


def run() -> None:
    settings = get_settings()
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        proxy_headers=settings.trusted_proxy,
        forwarded_allow_ips="127.0.0.1" if settings.trusted_proxy else "",
    )


app = create_app()


if __name__ == "__main__":
    run()
