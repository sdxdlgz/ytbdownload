from __future__ import annotations

import base64
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest

from app.config import Settings
from app.database import Database
from app.worker import run_analysis, run_job
from app.yt_service import YtDlpService


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


@pytest.mark.integration
def test_real_ytdlp_ffmpeg_video_audio_and_thumbnail_pipeline() -> None:
    with TemporaryDirectory(prefix="ytbdownload-integration-") as temporary:
        root = Path(temporary)
        media_root = root / "site"
        media_root.mkdir()
        fixture_dir = Path(__file__).resolve().parent / "fixtures"
        for encoded_name, output_name in (
            ("sample.mp4.b64", "sample.mp4"),
            ("cover.jpg.b64", "cover.jpg"),
        ):
            encoded = (fixture_dir / encoded_name).read_text(encoding="ascii")
            (media_root / output_name).write_bytes(base64.b64decode(encoded, validate=True))

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(SilentHandler, directory=str(media_root))
        )
        port = server.server_address[1]
        page_url = f"http://127.0.0.1:{port}/index.html"
        (media_root / "index.html").write_text(
            """<!doctype html><html><head><title>Integration Clip</title>"""
            f'<meta property="og:title" content="Integration Clip">'
            f'<meta property="og:video" content="http://127.0.0.1:{port}/sample.mp4">'
            '<meta property="og:video:type" content="video/mp4">'
            f'<meta property="og:image" content="http://127.0.0.1:{port}/cover.jpg">'
            "</head><body>local fixture</body></html>",
            encoding="utf-8",
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = Settings(
                _env_file=None,
                access_token="",
                app_secret="pytest-integration-secret",
                environment="test",
                data_dir=root / "app-data",
                allow_private_urls=True,
                js_runtime="",
                min_free_disk_mb=32,
                max_filesize_mb=100,
                max_storage_mb=512,
            )
            settings.ensure_directories()
            database = Database(settings.database_path)
            database.initialize()
            service = YtDlpService(settings, database)

            analysis = database.create_analysis(
                owner="integration",
                request_url=page_url,
                url_hash="f" * 64,
                playlist=False,
                ttl_minutes=60,
            )
            assert database.claim_next_operation() == ("analysis", analysis["id"])
            assert run_analysis(analysis["id"], service, database) == 0
            analysis = database.get_analysis(analysis["id"], owner="integration")
            assert analysis["status"] == "completed"
            assert analysis["result"]["thumbnail"]["url"].endswith("/cover.jpg")
            assert {choice["kind"] for choice in analysis["result"]["choices"]} == {
                "video",
                "audio",
                "thumbnail",
            }

            selections = {
                "video": lambda choice: choice["kind"] == "video" and choice["policy"] == "best",
                "audio": lambda choice: choice["kind"] == "audio" and choice.get("codec") == "mp3",
                "thumbnail": lambda choice: (
                    choice["kind"] == "thumbnail" and choice.get("format") == "png"
                ),
            }
            completed = {}
            for kind, predicate in selections.items():
                choice = next(item for item in analysis["result"]["choices"] if predicate(item))
                job, _ = database.create_job(
                    owner="integration",
                    analysis_id=analysis["id"],
                    choice=choice,
                    request={
                        "subtitle_languages": [],
                        "include_auto_subtitles": False,
                        "embed_metadata": True,
                    },
                    title=analysis["result"]["title"],
                    platform=analysis["result"]["platform"],
                    idempotency_key=None,
                    ttl_hours=12,
                )
                assert database.claim_next_operation() == ("job", job["id"])
                assert run_job(job["id"], service, database) == 0
                completed[kind] = database.get_job(job["id"], owner="integration")
                assert completed[kind]["status"] == "completed"
                assert completed[kind]["artifacts"]
                assert completed[kind]["artifacts"][0]["sha256"]

            video_artifact = primary_path(settings, completed["video"])
            audio_artifact = primary_path(settings, completed["audio"])
            image_artifact = primary_path(settings, completed["thumbnail"])
            assert video_artifact.suffix == ".mp4"
            assert audio_artifact.suffix == ".mp3"
            assert image_artifact.suffix == ".png"
            assert probe_codec_type(video_artifact) == "video"
            assert probe_codec_type(audio_artifact) == "audio"
            assert image_artifact.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def primary_path(settings: Settings, job: dict) -> Path:
    artifact = next(item for item in job["artifacts"] if item["primary"])
    return settings.artifacts_dir / artifact["relpath"]


def probe_codec_type(path: Path) -> str:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0" if path.suffix == ".mp4" else "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
        "stderr": result.stderr,
    }
    return result.stdout.strip()
