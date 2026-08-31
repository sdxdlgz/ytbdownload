from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class SlowMediaHandler(BaseHTTPRequestHandler):
    media_size = 20 * 1024 * 1024

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/index.html":
            port = self.server.server_address[1]
            body = (
                "<!doctype html><html><head><title>Cancellation Fixture</title>"
                '<meta property="og:title" content="Cancellation Fixture">'
                f'<meta property="og:video" content="http://127.0.0.1:{port}/slow.mp4">'
                '<meta property="og:video:type" content="video/mp4">'
                "</head><body>slow fixture</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/slow.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(self.media_size))
            self.end_headers()
            chunk = b"0" * (64 * 1024)
            try:
                for _ in range(self.media_size // len(chunk)):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_error(404)


@pytest.mark.integration
def test_running_download_is_cancelled_and_worker_files_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowMediaHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data_dir = tmp_path / "cancel-data"
    env = {
        "YTDLP_WEB_ENVIRONMENT": "test",
        "YTDLP_WEB_DATA_DIR": str(data_dir),
        "YTDLP_WEB_ACCESS_TOKEN": "",
        "YTDLP_WEB_APP_SECRET": "cancel-test-secret",
        "YTDLP_WEB_ALLOW_PRIVATE_URLS": "true",
        "YTDLP_WEB_ALLOWED_HOSTS": "testserver,127.0.0.1,localhost",
        "YTDLP_WEB_JS_RUNTIME": "",
        "YTDLP_WEB_MIN_FREE_DISK_MB": "32",
        "YTDLP_WEB_MAX_FILESIZE_MB": "100",
        "YTDLP_WEB_WORKER_POLL_SECONDS": "0.1",
        "YTDLP_WEB_CANCEL_GRACE_SECONDS": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings(
        _env_file=None,
        environment="test",
        data_dir=data_dir,
        access_token="",
        app_secret="cancel-test-secret",
        allow_private_urls=True,
        allowed_hosts="testserver,127.0.0.1,localhost",
        js_runtime="",
        min_free_disk_mb=32,
        max_filesize_mb=100,
        max_concurrent_operations=1,
        rate_limit_requests=10_000,
        worker_poll_seconds=0.1,
        cancel_grace_seconds=1,
        analysis_timeout_seconds=30,
        download_timeout_seconds=60,
        cleanup_interval_seconds=3600,
    )
    app = create_app(settings)
    media_url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    try:
        with TestClient(app) as client:
            analysis = client.post("/api/v1/analyses", json={"url": media_url}).json()
            analysis = poll_resource(client, f"/api/v1/analyses/{analysis['id']}", "completed", 60)
            choice = next(
                item
                for item in analysis["result"]["choices"]
                if item["kind"] == "video" and item["policy"] == "best"
            )
            job_response = client.post(
                "/api/v1/jobs",
                json={"analysis_id": analysis["id"], "choice_id": choice["id"]},
            )
            assert job_response.status_code == 202, job_response.text
            job = poll_until_downloading(client, job_response.json()["id"], 30)
            assert job["status"] in {"running", "postprocessing"}

            cancelled = client.delete(f"/api/v1/jobs/{job['id']}")
            assert cancelled.status_code == 200
            terminal = poll_resource(client, f"/api/v1/jobs/{job['id']}", "cancelled", 20)
            assert terminal["error"]["code"] == "CANCELLED"
            assert not (settings.work_dir / job["id"]).exists()
            assert not (settings.artifacts_dir / job["id"]).exists()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def poll_resource(client: TestClient, path: str, status: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] == status:
            return latest
        if latest["status"] in {"failed", "cancelled", "expired"} and latest["status"] != status:
            pytest.fail(f"unexpected terminal state: {latest}")
        time.sleep(0.15)
    pytest.fail(f"timed out waiting for {status}: {latest}")


def poll_until_downloading(client: TestClient, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] in {"running", "postprocessing"} and (
            latest["phase"] == "downloading" or (latest.get("downloaded_bytes") or 0) > 0
        ):
            return latest
        if latest["status"] in {"failed", "cancelled", "completed", "expired"}:
            pytest.fail(f"job ended before cancellation: {latest}")
        time.sleep(0.1)
    pytest.fail(f"timed out waiting for download: {latest}")
