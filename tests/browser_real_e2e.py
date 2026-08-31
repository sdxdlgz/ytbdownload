from __future__ import annotations

import base64
import os
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright

BASE_URL = "http://127.0.0.1:8766"
ARTIFACT_DIR = Path("artifacts/browser-real")
EXPECTED_BACKEND = os.environ.get("BROWSER_E2E_EXPECT_BACKEND", "").strip().lower()


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


def create_fixture(root: Path) -> tuple[ThreadingHTTPServer, Thread, str]:
    fixture_dir = Path(__file__).resolve().parent / "fixtures"
    for encoded_name, output_name in (
        ("sample.mp4.b64", "sample.mp4"),
        ("cover.jpg.b64", "cover.jpg"),
    ):
        encoded = (fixture_dir / encoded_name).read_text(encoding="ascii")
        (root / output_name).write_bytes(base64.b64decode(encoded, validate=True))
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SilentHandler, directory=str(root)))
    port = server.server_address[1]
    (root / "index.html").write_text(
        "<!doctype html><html><head><title>Real Browser Pipeline</title>"
        '<meta property="og:title" content="Real Browser Pipeline">'
        f'<meta property="og:video" content="http://127.0.0.1:{port}/sample.mp4">'
        '<meta property="og:video:type" content="video/mp4">'
        f'<meta property="og:image" content="http://127.0.0.1:{port}/cover.jpg">'
        "</head><body>local browser fixture</body></html>",
        encoding="utf-8",
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}/index.html"


def run() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with TemporaryDirectory(prefix="signal-browser-real-") as temporary:
        fixture_root = Path(temporary)
        server, thread, media_url = create_fixture(fixture_root)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1365, "height": 900}, accept_downloads=True
                )
                page = context.new_page()
                context.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE_URL)
                page.on(
                    "console",
                    lambda message: (
                        errors.append(f"console:{message.text}")
                        if message.type == "error"
                        else None
                    ),
                )
                page.on("pageerror", lambda error: errors.append(f"page:{error}"))
                page.goto(BASE_URL, wait_until="networkidle")
                page.get_by_label("媒体链接").fill(media_url)
                page.get_by_role("button", name="解析链接").click()

                expect(page.get_by_text("Real Browser Pipeline", exact=True)).to_be_visible(
                    timeout=120_000
                )
                expect(
                    page.locator("#choice-list .choice-copy strong", has_text="最佳画质")
                ).to_be_visible()
                page.get_by_role("button", name="开始传输").click()
                expect(page.get_by_text("文件已经落地。", exact=True)).to_be_visible(
                    timeout=180_000
                )

                video_item = page.locator("#artifact-list .artifact-item", has_text=".mp4")
                expect(video_item).to_be_visible(timeout=180_000)
                video_download = video_item.locator(".artifact-download")
                if EXPECTED_BACKEND:
                    expect(video_item).to_have_attribute("data-backend", EXPECTED_BACKEND)
                assert (video_download.get_attribute("download") or "").lower().endswith(".mp4")
                with page.expect_download(timeout=30_000) as download_info:
                    video_download.click()
                video_result = download_info.value
                failure = video_result.failure()
                assert failure is None, {
                    "failure": failure,
                    "suggested_filename": video_result.suggested_filename,
                }
                video_path = ARTIFACT_DIR / "browser-downloaded.mp4"
                video_result.save_as(video_path)
                assert probe(video_path, "v:0") == "video"

                video_item.locator(".artifact-link-button").click()
                expect(page.get_by_text("临时直链已复制", exact=True)).to_be_visible()
                direct_url = page.evaluate("navigator.clipboard.readText()")
                direct_client = playwright.request.new_context()
                direct_response = direct_client.get(direct_url, headers={"Range": "bytes=0-3"})
                assert direct_response.status == 206
                assert len(direct_response.body()) == 4
                direct_client.dispose()

                page.get_by_role("tab", name="封面").click()
                expect(
                    page.locator("#choice-list .choice-copy strong", has_text="原始封面")
                ).to_be_visible()
                page.get_by_role("button", name="开始传输").click()
                image_item = page.locator("#artifact-list .artifact-item", has_text=".jpg")
                expect(image_item).to_be_visible(timeout=180_000)
                image_download = image_item.locator(".artifact-download")
                if EXPECTED_BACKEND:
                    expect(image_item).to_have_attribute("data-backend", EXPECTED_BACKEND)
                assert (image_download.get_attribute("download") or "").lower().endswith(".jpg")
                with page.expect_download(timeout=30_000) as download_info:
                    image_download.click()
                image_result = download_info.value
                failure = image_result.failure()
                assert failure is None, {
                    "failure": failure,
                    "suggested_filename": image_result.suggested_filename,
                }
                image_path = ARTIFACT_DIR / "browser-downloaded.jpg"
                image_result.save_as(image_path)
                assert image_path.read_bytes().startswith(b"\xff\xd8")

                page.screenshot(path=str(ARTIFACT_DIR / "real-completed.png"), full_page=True)
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert not errors, errors
    print(f"Real browser/API/worker yt-dlp flow PASS: {ARTIFACT_DIR}")


def probe(path: Path, selector: str) -> str:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    run()
