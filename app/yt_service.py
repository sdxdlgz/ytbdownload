from __future__ import annotations

import hashlib
import itertools
import math
import mimetypes
import os
import re
import shutil
import time
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yt_dlp
from yt_dlp.utils import DownloadError

from app.config import Settings
from app.database import Database
from app.errors import AppError, ResourceLimitExceeded, WorkerCancelled
from app.security import URLValidator

_SAFE_FORMAT_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MEDIA_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".m4v",
    ".mka",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
_IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_SIDECAR_EXTENSIONS = {".ass", ".json", ".lrc", ".srt", ".ssa", ".ttml", ".vtt"}


class QuietLogger:
    def __init__(self, input_url: str) -> None:
        self.input_url = input_url
        self.last_error = ""
        self.warnings: list[str] = []

    def debug(self, _message: str) -> None:
        return

    def info(self, _message: str) -> None:
        return

    def warning(self, message: str) -> None:
        if len(self.warnings) < 8:
            self.warnings.append(redact_upstream_message(message, self.input_url))

    def error(self, message: str) -> None:
        self.last_error = redact_upstream_message(message, self.input_url)


class ProgressReporter:
    def __init__(self, db: Database, job_id: str, settings: Settings) -> None:
        self.db = db
        self.job_id = job_id
        self.settings = settings
        self.last_write = 0.0
        self.last_progress = -1.0
        self.bytes_by_file: dict[str, int] = {}

    def progress_hook(self, event: dict[str, Any]) -> None:
        self._check_cancelled()
        status = event.get("status")
        info = event.get("info_dict") or {}
        filename = str(event.get("filename") or info.get("filepath") or "current")
        downloaded = safe_int(event.get("downloaded_bytes")) or 0
        self.bytes_by_file[filename] = max(downloaded, self.bytes_by_file.get(filename, 0))
        aggregate_downloaded = sum(self.bytes_by_file.values())
        if aggregate_downloaded > self.settings.max_filesize_bytes:
            raise ResourceLimitExceeded("下载数据超过服务器设置的文件大小上限。")

        total = safe_int(event.get("total_bytes")) or safe_int(event.get("total_bytes_estimate"))
        item_progress = 0.0
        if total and total > 0:
            item_progress = min(1.0, downloaded / total)
        else:
            fragment_index = safe_int(event.get("fragment_index"))
            fragment_count = safe_int(event.get("fragment_count"))
            if fragment_index and fragment_count:
                item_progress = min(1.0, fragment_index / fragment_count)

        playlist_index = safe_int(info.get("playlist_index"))
        playlist_count = safe_int(info.get("playlist_count")) or safe_int(info.get("n_entries"))
        if playlist_index and playlist_count:
            overall = ((playlist_index - 1) + item_progress) / max(1, playlist_count)
        else:
            overall = item_progress
        progress = min(94.0, max(0.0, overall * 94.0))

        now = time.monotonic()
        meaningful = abs(progress - self.last_progress) >= 0.5
        if status == "finished":
            self.db.set_job_postprocessing(self.job_id)
            self.last_write = now
            self.last_progress = max(progress, 94.0)
            return
        if status != "downloading":
            return
        if now - self.last_write < 0.75 and not meaningful:
            return
        self.db.update_job_progress(
            self.job_id,
            phase="downloading",
            progress=progress,
            downloaded_bytes=aggregate_downloaded,
            total_bytes=total,
            speed=safe_float(event.get("speed")),
            eta=safe_int(event.get("eta")),
            playlist_index=playlist_index,
            playlist_count=playlist_count,
        )
        self.last_write = now
        self.last_progress = progress

    def postprocessor_hook(self, event: dict[str, Any]) -> None:
        self._check_cancelled()
        if event.get("status") == "started":
            postprocessor = safe_text(event.get("postprocessor"), 80) or "ffmpeg"
            self.db.update_job_progress(
                self.job_id,
                phase=f"postprocessing:{postprocessor.lower()}",
                progress=96.0,
            )
        elif event.get("status") == "finished":
            self.db.update_job_progress(self.job_id, phase="finalizing", progress=98.0)

    def _check_cancelled(self) -> None:
        if self.db.is_cancel_requested(self.job_id):
            raise WorkerCancelled("任务已取消。")


class YtDlpService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.url_validator = URLValidator(settings)

    def analyze(self, url: str, *, playlist: bool) -> dict[str, Any]:
        validated = self.url_validator.validate_sync(url)
        logger = QuietLogger(validated.url)
        options = self._common_options(logger)
        options.update(
            {
                "skip_download": True,
                "noplaylist": not playlist,
                "playlistend": self.settings.max_playlist_items + 1,
                "extract_flat": "in_playlist" if playlist else False,
                "ignore_no_formats_error": True,
            }
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(validated.url, download=False)
        except DownloadError as exc:
            raise map_download_error(exc, logger, validated.url) from exc
        if not info:
            raise AppError("NO_MEDIA", "没有从该链接中找到可下载媒体。", status_code=422)
        if info.get("_type") in {"playlist", "multi_video"}:
            if not playlist:
                raise AppError(
                    "PLAYLIST_CONFIRMATION_REQUIRED",
                    "该链接是播放列表；请启用“允许播放列表”后重新分析。",
                    status_code=409,
                )
            return self._normalize_playlist(info)
        return self._normalize_single(info)

    def download(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        job_id = str(context["id"])
        url = str(context["request_url"])
        validated = self.url_validator.validate_sync(url)
        choice = context["choice"]
        request = context["request"]
        analysis = context.get("analysis_result") or {}
        playlist = bool(context.get("analysis_playlist"))

        if context.get("cancel_requested") or self.db.is_cancel_requested(job_id):
            raise WorkerCancelled("任务已取消。")
        self._check_disk_space()

        work_dir = self.settings.work_dir / job_id
        artifact_dir = self.settings.artifacts_dir / job_id
        safe_rmtree(work_dir, self.settings.work_dir)
        safe_rmtree(artifact_dir, self.settings.artifacts_dir)
        work_dir.mkdir(parents=True, exist_ok=False)
        (work_dir / "tmp").mkdir()

        reporter = ProgressReporter(self.db, job_id, self.settings)
        logger = QuietLogger(validated.url)
        options = self._download_options(
            validated.url,
            choice=choice,
            request=request,
            analysis=analysis,
            playlist=playlist,
            work_dir=work_dir,
            reporter=reporter,
            logger=logger,
        )

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(validated.url, download=True)
        except WorkerCancelled:
            raise
        except ResourceLimitExceeded:
            raise
        except DownloadError as exc:
            if self.db.is_cancel_requested(job_id):
                raise WorkerCancelled("任务已取消。") from exc
            raise map_download_error(exc, logger, validated.url) from exc

        if self.db.is_cancel_requested(job_id):
            raise WorkerCancelled("任务已取消。")
        self.db.update_job_progress(job_id, phase="finalizing", progress=98.0)
        return self._finalize_artifacts(
            job_id,
            work_dir,
            artifact_dir,
            title=safe_text(analysis.get("title"), 180) or "download",
            choice=choice,
            playlist=playlist,
        )

    def _common_options(self, logger: QuietLogger) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": logger,
            "socket_timeout": self.settings.socket_timeout_seconds,
            "retries": self.settings.download_retries,
            "extractor_retries": self.settings.extractor_retries,
            "fragment_retries": self.settings.fragment_retries,
            "cachedir": False,
            "geo_bypass": False,
            "nocheckcertificate": False,
        }
        if self.settings.cookies_file:
            cookie_path = self.settings.cookies_file.resolve()
            if not cookie_path.is_file():
                raise AppError(
                    "COOKIES_FILE_MISSING", "服务器配置的 cookies 文件不存在。", status_code=503
                )
            options["cookiefile"] = str(cookie_path)
        if self.settings.proxy:
            options["proxy"] = self.settings.proxy
        if self.settings.impersonate:
            options["impersonate"] = self.settings.impersonate
        if self.settings.js_runtime:
            options["js_runtimes"] = {self.settings.js_runtime: {}}
            options["extractor_args"] = {"youtube-ejs": {"jitless": ["true"]}}
        return options

    def _normalize_single(self, info: dict[str, Any]) -> dict[str, Any]:
        media_id = safe_text(info.get("id"), 200) or "unknown"
        extractor = safe_text(info.get("extractor_key") or info.get("extractor"), 100) or "generic"
        duration = safe_int(info.get("duration"))
        live_status = safe_text(info.get("live_status"), 40)
        is_live = bool(info.get("is_live")) or live_status in {
            "is_live",
            "is_upcoming",
            "post_live",
        }
        restriction: dict[str, str] | None = None
        if is_live:
            restriction = {
                "code": "LIVE_NOT_SUPPORTED",
                "message": "为保护 VPS 资源，暂不下载直播或预约直播。",
            }
        elif duration and duration > self.settings.max_duration_seconds:
            restriction = {
                "code": "DURATION_LIMIT",
                "message": f"媒体时长超过服务器上限（{self.settings.max_duration_seconds // 60} 分钟）。",
            }

        thumbnail = best_thumbnail(info)
        formats = normalize_formats(source_formats(info), self.settings.max_formats)
        choices = build_choices(
            formats,
            has_thumbnail=bool(thumbnail),
            playlist=False,
            restricted=restriction is not None,
        )
        subtitles = normalize_subtitles(info)
        return {
            "kind": "single",
            "id": media_id,
            "extractor": extractor,
            "platform": platform_label(extractor),
            "title": safe_text(info.get("title"), 300) or "未命名媒体",
            "uploader": safe_text(
                info.get("uploader") or info.get("channel") or info.get("creator"), 200
            ),
            "duration": duration,
            "timestamp": safe_int(info.get("timestamp")),
            "upload_date": safe_text(info.get("upload_date"), 16),
            "description": safe_text(info.get("description"), 1200),
            "view_count": safe_int(info.get("view_count")),
            "like_count": safe_int(info.get("like_count")),
            "age_limit": safe_int(info.get("age_limit")),
            "thumbnail": thumbnail,
            "live_status": live_status,
            "restriction": restriction,
            "choices": choices,
            "formats": [public_format(item) for item in formats],
            "subtitles": subtitles,
            "webpage_domain": safe_domain(info.get("webpage_url")),
        }

    def _normalize_playlist(self, info: dict[str, Any]) -> dict[str, Any]:
        raw_entries = info.get("entries") or []
        entries = list(
            itertools.islice(
                (entry for entry in raw_entries if entry), self.settings.max_playlist_items + 1
            )
        )
        reported_count = safe_int(info.get("playlist_count")) or safe_int(info.get("n_entries"))
        if len(entries) > self.settings.max_playlist_items or (
            reported_count and reported_count > self.settings.max_playlist_items
        ):
            raise AppError(
                "PLAYLIST_LIMIT",
                f"播放列表超过服务器上限（{self.settings.max_playlist_items} 项），请使用更短的列表。",
                status_code=422,
                details={"limit": self.settings.max_playlist_items},
            )
        if any(entry.get("_type") in {"playlist", "multi_video"} for entry in entries):
            raise AppError("NESTED_PLAYLIST", "不支持嵌套播放列表。", status_code=422)

        normalized_entries: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for index, entry in enumerate(entries, 1):
            entry_id = safe_text(entry.get("id"), 200) or f"item-{index}"
            extractor = (
                safe_text(entry.get("extractor_key") or entry.get("ie_key"), 100) or "generic"
            )
            identity = (extractor, entry_id)
            if identity in seen:
                continue
            seen.add(identity)
            duration = safe_int(entry.get("duration"))
            if duration and duration > self.settings.max_duration_seconds:
                raise AppError(
                    "DURATION_LIMIT",
                    f"播放列表第 {index} 项超过时长上限。",
                    status_code=422,
                )
            normalized_entries.append(
                {
                    "index": index,
                    "id": entry_id,
                    "title": safe_text(entry.get("title"), 240) or f"项目 {index}",
                    "uploader": safe_text(entry.get("uploader") or entry.get("channel"), 160),
                    "duration": duration,
                    "thumbnail": best_thumbnail(entry),
                }
            )
        if not normalized_entries:
            raise AppError("EMPTY_PLAYLIST", "播放列表中没有可下载项目。", status_code=422)

        extractor = safe_text(info.get("extractor_key") or info.get("extractor"), 100) or "generic"
        thumbnail = best_thumbnail(info) or normalized_entries[0].get("thumbnail")
        return {
            "kind": "playlist",
            "id": safe_text(info.get("id"), 200) or "playlist",
            "extractor": extractor,
            "platform": platform_label(extractor),
            "title": safe_text(info.get("title"), 300) or "未命名播放列表",
            "uploader": safe_text(info.get("uploader") or info.get("channel"), 200),
            "description": safe_text(info.get("description"), 1200),
            "thumbnail": thumbnail,
            "entry_count": len(normalized_entries),
            "entries": normalized_entries,
            "choices": build_playlist_choices(has_thumbnail=bool(thumbnail)),
            "formats": [],
            "subtitles": [],
            "restriction": None,
            "webpage_domain": safe_domain(info.get("webpage_url")),
        }

    def _download_options(
        self,
        input_url: str,
        *,
        choice: dict[str, Any],
        request: dict[str, Any],
        analysis: dict[str, Any],
        playlist: bool,
        work_dir: Path,
        reporter: ProgressReporter,
        logger: QuietLogger,
    ) -> dict[str, Any]:
        options = self._common_options(logger)
        default_template = "item-%(playlist_index)03d.%(ext)s" if playlist else "media.%(ext)s"
        thumbnail_template = "item-%(playlist_index)03d.%(ext)s" if playlist else "cover.%(ext)s"
        options.update(
            {
                "paths": {"home": str(work_dir), "temp": str(work_dir / "tmp")},
                "outtmpl": {
                    "default": default_template,
                    "thumbnail": thumbnail_template,
                    "subtitle": default_template,
                },
                "windowsfilenames": True,
                "restrictfilenames": True,
                "trim_file_name": 180,
                "overwrites": False,
                "continuedl": True,
                "noplaylist": not playlist,
                "playlistend": self.settings.max_playlist_items,
                "max_downloads": (self.settings.max_playlist_items + 1 if playlist else 2),
                "max_filesize": self.settings.max_filesize_bytes,
                "concurrent_fragment_downloads": self.settings.concurrent_fragments,
                "progress_hooks": [reporter.progress_hook],
                "postprocessor_hooks": [reporter.postprocessor_hook],
                "match_filter": self._match_filter(analysis, choice, playlist, reporter.job_id),
            }
        )

        postprocessors: list[dict[str, Any]] = []
        kind = choice.get("kind")
        if kind == "video":
            options["format"] = video_selector(choice)
            options["merge_output_format"] = "mp4/mkv"
            if request.get("embed_metadata", True):
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
            subtitle_languages = request.get("subtitle_languages") or []
            if subtitle_languages:
                options["writesubtitles"] = True
                options["writeautomaticsub"] = bool(request.get("include_auto_subtitles"))
                options["subtitleslangs"] = subtitle_languages
                options["subtitlesformat"] = "vtt/best"
        elif kind == "audio":
            codec = choice.get("codec")
            if codec not in {"mp3", "m4a", "opus"}:
                raise AppError("INVALID_CHOICE", "无效音频输出格式。")
            options["format"] = "ba/b"
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec,
                    "preferredquality": "192" if codec == "mp3" else "0",
                }
            )
            if request.get("embed_metadata", True):
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        elif kind == "thumbnail":
            options["skip_download"] = True
            options["writethumbnail"] = True
            thumbnail_format = choice.get("format", "original")
            if thumbnail_format in {"jpg", "png"}:
                postprocessors.append(
                    {
                        "key": "FFmpegThumbnailsConvertor",
                        "format": thumbnail_format,
                        "when": "before_dl",
                    }
                )
        else:
            raise AppError("INVALID_CHOICE", "未知下载类型。")
        options["postprocessors"] = postprocessors
        return options

    def _match_filter(
        self,
        analysis: dict[str, Any],
        choice: dict[str, Any],
        playlist: bool,
        job_id: str,
    ) -> Callable[..., str | None]:
        expected_id = safe_text(analysis.get("id"), 200)
        expected_extractor = safe_text(analysis.get("extractor"), 100)

        def filter_media(info: dict[str, Any], *, incomplete: bool) -> str | None:
            del incomplete
            if self.db.is_cancel_requested(job_id):
                return "任务已取消"
            duration = safe_int(info.get("duration"))
            if duration and duration > self.settings.max_duration_seconds:
                return f"媒体时长超过服务器上限（{self.settings.max_duration_seconds} 秒）"
            live_status = info.get("live_status")
            if info.get("is_live") or live_status in {"is_live", "is_upcoming", "post_live"}:
                return "不支持直播或预约直播下载"
            if info.get("has_drm"):
                return "媒体受 DRM 保护"
            if not playlist:
                actual_id = safe_text(info.get("id"), 200)
                actual_extractor = safe_text(
                    info.get("extractor_key") or info.get("extractor"), 100
                )
                if expected_id and actual_id and expected_id != actual_id:
                    return "媒体身份已变化，请重新分析链接"
                if (
                    expected_extractor
                    and actual_extractor
                    and expected_extractor.lower() != actual_extractor.lower()
                ):
                    return "媒体来源已变化，请重新分析链接"
                if choice.get("policy") == "exact":
                    format_id = choice.get("format_id")
                    current_ids = {str(item.get("format_id")) for item in source_formats(info)}
                    if format_id not in current_ids:
                        return "所选格式已失效，请重新分析链接"
            return None

        return filter_media

    def _finalize_artifacts(
        self,
        job_id: str,
        work_dir: Path,
        artifact_dir: Path,
        *,
        title: str,
        choice: dict[str, Any],
        playlist: bool,
    ) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        root = work_dir.resolve()
        for path in work_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise AppError("UNSAFE_ARTIFACT", "下载器生成了不安全的文件路径。", status_code=500)
            if "tmp" in path.relative_to(work_dir).parts:
                continue
            if path.suffix.lower() in {".part", ".ytdl", ".temp"}:
                continue
            candidates.append(path)
        if not candidates:
            raise AppError("NO_ARTIFACT", "下载完成但没有找到输出文件。", status_code=502)
        candidates.sort(key=lambda item: item.name)
        raw_size = sum(path.stat().st_size for path in candidates)
        if raw_size > self.settings.max_filesize_bytes:
            raise ResourceLimitExceeded("最终文件超过服务器设置的大小上限。")

        artifact_dir.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, str]] = []
        for index, source in enumerate(candidates, 1):
            suffix = all_suffixes(source)
            destination = artifact_dir / f"artifact-{index:03d}{suffix}"
            shutil.move(str(source), destination)
            # A setgid artifact directory may use a dedicated Nginx-readable group.
            # rename(2) preserves the work-file GID, so set it explicitly after moving.
            if hasattr(os, "chown"):
                os.chown(destination, -1, artifact_dir.stat().st_gid)
            os.chmod(destination, 0o640)
            display_name = display_filename(title, suffix, index if len(candidates) > 1 else None)
            moved.append((destination, display_name))

        archive_path: Path | None = None
        archive_name: str | None = None
        if playlist and len(moved) > 1:
            archive_path = artifact_dir / "collection.zip"
            used: set[str] = set()
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                for path, proposed in moved:
                    unique = unique_name(proposed, used)
                    archive.write(path, arcname=unique)
            if hasattr(os, "chown"):
                os.chown(archive_path, -1, artifact_dir.stat().st_gid)
            os.chmod(archive_path, 0o640)
            archive_name = display_filename(title, ".zip", None)

        records: list[dict[str, Any]] = []
        primary_path = archive_path or choose_primary_file([item[0] for item in moved], choice)
        all_files: list[tuple[Path, str]] = list(moved)
        if archive_path and archive_name:
            all_files.insert(0, (archive_path, archive_name))
        for path, filename in all_files:
            relative = path.relative_to(self.settings.artifacts_dir).as_posix()
            record = self.db.add_artifact(
                job_id=job_id,
                relpath=relative,
                filename=filename,
                size=path.stat().st_size,
                sha256=sha256_file(path),
                media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                primary=path == primary_path,
                ttl_hours=self.settings.artifact_ttl_hours,
            )
            records.append(record)
        safe_rmtree(work_dir, self.settings.work_dir)
        return records

    def _check_disk_space(self) -> None:
        usage = shutil.disk_usage(self.settings.data_dir)
        if usage.free < self.settings.min_free_disk_bytes:
            raise AppError(
                "LOW_DISK_SPACE", "服务器磁盘可用空间不足，请稍后重试。", status_code=503
            )


def source_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return extractor formats, including yt-dlp's top-level direct-media fallback."""
    raw = info.get("formats")
    if isinstance(raw, list) and raw:
        return raw
    if not info.get("url"):
        return []
    ext = safe_text(info.get("ext"), 12) or "bin"
    video_ext = safe_text(info.get("video_ext"), 12)
    audio_ext = safe_text(info.get("audio_ext"), 12)
    has_video = video_ext not in {None, "none"} or ext.lower() in {
        "3gp",
        "avi",
        "m4v",
        "mkv",
        "mov",
        "mp4",
        "webm",
    }
    has_audio = (
        audio_ext not in {None, "none"}
        or has_video
        or ext.lower()
        in {
            "aac",
            "flac",
            "m4a",
            "mp3",
            "ogg",
            "opus",
            "wav",
        }
    )
    return [
        {
            "format_id": safe_text(info.get("format_id"), 80) or "direct",
            "ext": ext,
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "vcodec": info.get("vcodec") or ("unknown" if has_video else "none"),
            "acodec": info.get("acodec") or ("unknown" if has_audio else "none"),
            "filesize": info.get("filesize"),
            "filesize_approx": info.get("filesize_approx"),
            "tbr": info.get("tbr"),
            "abr": info.get("abr"),
            "vbr": info.get("vbr"),
            "format_note": info.get("format_note"),
            "protocol": info.get("protocol"),
            "has_drm": info.get("has_drm") or info.get("_has_drm"),
        }
    ]


def normalize_formats(raw_formats: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_formats:
        format_id = safe_text(item.get("format_id"), 80)
        if not format_id or not _SAFE_FORMAT_ID.fullmatch(format_id) or format_id in seen:
            continue
        seen.add(format_id)
        if item.get("has_drm"):
            continue
        vcodec = safe_text(item.get("vcodec"), 80) or "none"
        acodec = safe_text(item.get("acodec"), 80) or "none"
        if vcodec == "none" and acodec == "none":
            continue
        protocol = safe_text(item.get("protocol"), 60) or ""
        if protocol in {"mhtml", "images"}:
            continue
        width = safe_int(item.get("width"))
        height = safe_int(item.get("height"))
        size = safe_int(item.get("filesize")) or safe_int(item.get("filesize_approx"))
        normalized.append(
            {
                "format_id": format_id,
                "ext": safe_text(item.get("ext"), 12) or "bin",
                "width": width,
                "height": height,
                "fps": safe_float(item.get("fps")),
                "vcodec": vcodec,
                "acodec": acodec,
                "has_video": vcodec != "none",
                "has_audio": acodec != "none",
                "filesize": size,
                "tbr": safe_float(item.get("tbr")),
                "abr": safe_float(item.get("abr")),
                "vbr": safe_float(item.get("vbr")),
                "quality": safe_float(item.get("quality")),
                "format_note": safe_text(item.get("format_note"), 100),
                "dynamic_range": safe_text(item.get("dynamic_range"), 30),
                "language": safe_text(item.get("language"), 32),
                "protocol": protocol,
            }
        )
    normalized.sort(
        key=lambda item: (
            int(item["has_video"]),
            item.get("height") or 0,
            int(item["has_audio"]),
            item.get("tbr") or 0,
        ),
        reverse=True,
    )
    return normalized[:limit]


def build_choices(
    formats: list[dict[str, Any]], *, has_thumbnail: bool, playlist: bool, restricted: bool
) -> list[dict[str, Any]]:
    if playlist:
        return build_playlist_choices(has_thumbnail=has_thumbnail)
    choices: list[dict[str, Any]] = []
    if not restricted:
        video_formats = [item for item in formats if item["has_video"]]
        audio_formats = [item for item in formats if item["has_audio"]]
        if video_formats:
            choices.append(
                make_choice(
                    "video",
                    "best",
                    "最佳画质",
                    "自动选择最佳视频与音频；优先 MP4，必要时使用 MKV。",
                    badge="AUTO",
                )
            )
            max_height = max((item.get("height") or 0 for item in video_formats), default=0)
            for height in (2160, 1440, 1080, 720, 480, 360):
                if max_height >= height:
                    choices.append(
                        make_choice(
                            "video",
                            "resolution",
                            f"最高 {height}p",
                            f"将画面限制在 {height}p 以内并自动合并音频。",
                            height=height,
                            badge=f"{height}P",
                        )
                    )
            for item in video_formats:
                label = format_label(item)
                choices.append(
                    make_choice(
                        "video",
                        "exact",
                        label,
                        format_description(item),
                        format_id=item["format_id"],
                        needs_audio=not item["has_audio"],
                        height=item.get("height"),
                        ext=item.get("ext"),
                        expected_size=item.get("filesize"),
                        technical=True,
                    )
                )
        if audio_formats:
            for codec, label, description in (
                ("mp3", "MP3 音频", "通用兼容，使用 ffmpeg 转换为高质量 MP3。"),
                ("m4a", "M4A 音频", "通常无需有损二次转码，适合 Apple 设备。"),
                ("opus", "Opus 音频", "高压缩效率，适合现代播放器。"),
            ):
                choices.append(
                    make_choice(
                        "audio", "audio", label, description, codec=codec, badge=codec.upper()
                    )
                )
    if has_thumbnail:
        for image_format, label in (
            ("original", "原始封面"),
            ("jpg", "JPG 封面"),
            ("png", "PNG 封面"),
        ):
            choices.append(
                make_choice(
                    "thumbnail",
                    "thumbnail",
                    label,
                    "单独保存最高质量封面图。",
                    format=image_format,
                    badge="COVER",
                )
            )
    return choices


def build_playlist_choices(*, has_thumbnail: bool) -> list[dict[str, Any]]:
    choices = [
        make_choice("video", "best", "整表 · 最佳画质", "下载全部项目并打包为 ZIP。", badge="ZIP"),
        make_choice(
            "video",
            "resolution",
            "整表 · 最高 720p",
            "限制清晰度以节省 VPS 流量和磁盘。",
            height=720,
            badge="720P",
        ),
        make_choice(
            "video",
            "resolution",
            "整表 · 最高 480p",
            "适合移动端或较小磁盘空间。",
            height=480,
            badge="480P",
        ),
        make_choice(
            "audio", "audio", "整表 · MP3", "提取全部项目音频并打包。", codec="mp3", badge="MP3"
        ),
        make_choice(
            "audio", "audio", "整表 · M4A", "提取全部项目音频并打包。", codec="m4a", badge="M4A"
        ),
    ]
    if has_thumbnail:
        choices.append(
            make_choice(
                "thumbnail",
                "thumbnail",
                "整表 · 全部封面",
                "下载每个项目的原始封面并打包。",
                format="original",
                badge="COVERS",
            )
        )
    return choices


def make_choice(
    kind: str, policy: str, label: str, description: str, **extra: Any
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "kind": kind,
        "policy": policy,
        "label": label,
        "description": description,
        **extra,
    }


def public_format(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "format_id",
            "ext",
            "width",
            "height",
            "fps",
            "vcodec",
            "acodec",
            "has_video",
            "has_audio",
            "filesize",
            "tbr",
            "format_note",
            "dynamic_range",
            "language",
        )
    }


def video_selector(choice: dict[str, Any]) -> str:
    policy = choice.get("policy")
    if policy == "best":
        return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    if policy == "resolution":
        height = safe_int(choice.get("height"))
        if height not in {360, 480, 720, 1080, 1440, 2160}:
            raise AppError("INVALID_CHOICE", "无效清晰度选项。")
        return (
            f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={height}][ext=mp4]/"
            f"bv*[height<={height}]+ba/b[height<={height}]/b"
        )
    if policy == "exact":
        format_id = str(choice.get("format_id", ""))
        if not _SAFE_FORMAT_ID.fullmatch(format_id):
            raise AppError("INVALID_CHOICE", "所选格式标识无效。")
        if choice.get("needs_audio"):
            return f"{format_id}+ba/{format_id}"
        return format_id
    raise AppError("INVALID_CHOICE", "无效视频选项。")


def normalize_subtitles(info: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source_key, kind in (("subtitles", "manual"), ("automatic_captions", "automatic")):
        source = info.get(source_key) or {}
        if not isinstance(source, dict):
            continue
        for code, variants in source.items():
            code_text = safe_text(code, 32)
            if not code_text or (code_text, kind) in seen:
                continue
            seen.add((code_text, kind))
            names = [
                safe_text(item.get("name"), 80) for item in variants or [] if isinstance(item, dict)
            ]
            result.append(
                {
                    "code": code_text,
                    "name": next((name for name in names if name), code_text),
                    "kind": kind,
                }
            )
            if len(result) >= 80:
                return result
    return result


def best_thumbnail(info: dict[str, Any]) -> dict[str, Any] | None:
    candidates = info.get("thumbnails") or []
    if info.get("thumbnail"):
        candidates = [*candidates, {"url": info.get("thumbnail")}]
    valid: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = safe_http_url(item.get("url"))
        if not url:
            continue
        valid.append(
            {
                "url": url,
                "width": safe_int(item.get("width")),
                "height": safe_int(item.get("height")),
                "id": safe_text(item.get("id"), 40),
            }
        )
    if not valid:
        return None
    return max(valid, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0))


def format_label(item: dict[str, Any]) -> str:
    height = item.get("height")
    resolution = f"{height}p" if height else ("音频" if not item["has_video"] else "未知分辨率")
    audio = " + 音频" if item["has_audio"] else " · 需合并音频"
    fps = f" · {int(item['fps'])}fps" if item.get("fps") else ""
    return f"{resolution}{fps}{audio} · {str(item.get('ext', '')).upper()}"


def format_description(item: dict[str, Any]) -> str:
    parts = [str(item.get("format_id"))]
    if item.get("vcodec") and item["vcodec"] != "none":
        parts.append(str(item["vcodec"]).split(".")[0])
    if item.get("acodec") and item["acodec"] != "none":
        parts.append(str(item["acodec"]).split(".")[0])
    if item.get("filesize"):
        parts.append(human_bytes(item["filesize"]))
    elif item.get("tbr"):
        parts.append(f"≈ {item['tbr']:.0f} kb/s")
    if item.get("format_note"):
        parts.append(str(item["format_note"]))
    return " · ".join(parts)


def platform_label(extractor: str) -> str:
    lowered = extractor.lower()
    labels = {
        "youtube": "YouTube",
        "youtubeplaylist": "YouTube",
        "bilibili": "Bilibili",
        "bilibilispacevideo": "Bilibili",
        "twitter": "X / Twitter",
        "tiktok": "TikTok",
        "instagram": "Instagram",
        "vimeo": "Vimeo",
        "soundcloud": "SoundCloud",
        "twitch": "Twitch",
        "facebook": "Facebook",
        "reddit": "Reddit",
    }
    for key, label in labels.items():
        if key in lowered:
            return label
    return extractor


def map_download_error(exc: Exception, logger: QuietLogger, input_url: str) -> AppError:
    message = logger.last_error or redact_upstream_message(str(exc), input_url)
    lowered = message.lower()
    if "unsupported url" in lowered or "no suitable extractor" in lowered:
        return AppError("UNSUPPORTED_URL", "yt-dlp 暂不支持该链接。", status_code=422)
    if (
        "private video" in lowered
        or "login" in lowered
        or "sign in" in lowered
        or "cookies" in lowered
    ):
        return AppError(
            "AUTHENTICATION_REQUIRED", "该媒体需要登录或有效 cookies。", status_code=422
        )
    if "drm" in lowered:
        return AppError("DRM_PROTECTED", "该媒体受 DRM 保护，无法下载。", status_code=422)
    if "requested format is not available" in lowered or "格式已失效" in message:
        return AppError("FORMAT_STALE", "所选格式已失效，请重新分析链接。", status_code=409)
    if "media duration" in lowered or "时长超过" in message:
        return AppError("DURATION_LIMIT", "媒体超过服务器允许的时长。", status_code=422)
    if "file is larger" in lowered or "文件大小" in message:
        return AppError("SIZE_LIMIT", "媒体超过服务器允许的文件大小。", status_code=422)
    if "not available" in lowered or "removed" in lowered:
        return AppError("MEDIA_UNAVAILABLE", "该媒体当前不可用或已被删除。", status_code=422)
    return AppError(
        "EXTRACTOR_ERROR",
        "媒体站点拒绝请求或解析失败；请确认链接可公开访问，并尝试更新 yt-dlp/cookies。",
        status_code=502,
    )


def redact_upstream_message(message: str, input_url: str) -> str:
    message = str(message).replace(input_url, "[输入链接]")
    message = re.sub(r"https?://[^\s'\"]+", "[上游链接]", message)
    message = re.sub(r"(?i)(cookie|token|signature|sig|key)=([^&\s]+)", r"\1=[REDACTED]", message)
    message = re.sub(r"[\r\n\t]+", " ", message).strip()
    return message[:500]


def safe_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or None


def safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def safe_http_url(value: Any) -> str | None:
    text = safe_text(value, 4096)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def safe_domain(value: Any) -> str | None:
    text = safe_http_url(value)
    if not text:
        return None
    return urlsplit(text).hostname


def display_filename(title: str, suffix: str, index: int | None) -> str:
    base = safe_text(title, 120) or "download"
    base = re.sub(r"[\\/:*?\"<>|]", "_", base).strip(" ._") or "download"
    if index is not None:
        base = f"{index:03d} - {base}"
    return f"{base}{suffix}"


def unique_name(name: str, used: set[str]) -> str:
    candidate = name
    stem, suffix = os.path.splitext(name)
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({counter}){suffix}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def all_suffixes(path: Path) -> str:
    suffixes = "".join(path.suffixes[-2:])
    if not suffixes or len(suffixes) > 20 or any(char in suffixes for char in "/\\"):
        return path.suffix[:12] or ".bin"
    return suffixes.lower()


def choose_primary_file(paths: list[Path], choice: dict[str, Any]) -> Path:
    kind = choice.get("kind")
    desired = _IMAGE_EXTENSIONS if kind == "thumbnail" else _MEDIA_EXTENSIONS
    for path in sorted(paths, key=lambda item: item.stat().st_size, reverse=True):
        if path.suffix.lower() in desired:
            return path
    return max(paths, key=lambda item: item.stat().st_size)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    root = allowed_root.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"refusing to remove unsafe path: {resolved}")
    shutil.rmtree(resolved, ignore_errors=False)
