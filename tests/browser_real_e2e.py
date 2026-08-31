from __future__ import annotations

import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright

BASE_URL = "http://127.0.0.1:8766"
ARTIFACT_DIR = Path("artifacts/browser-real")


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


def create_fixture(root: Path) -> tuple[ThreadingHTTPServer, Thread, str]:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=44100",
            "-t",
            "1.1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(root / "sample.mp4"),
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x182014:s=320x180",
            "-frames:v",
            "1",
            str(root / "cover.jpg"),
        ],
        check=True,
        timeout=30,
    )
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
                page = browser.new_page(viewport={"width": 1365, "height": 900})
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
                    timeout=60_000
                )
                expect(
                    page.locator("#choice-list .choice-copy strong", has_text="最佳画质")
                ).to_be_visible()
                page.get_by_role("button", name="开始传输").click()

                video_link = page.locator("#artifact-list .artifact-item", has_text=".mp4")
                expect(video_link).to_be_visible(timeout=90_000)
                with page.expect_download(timeout=30_000) as download_info:
                    video_link.click()
                video_path = ARTIFACT_DIR / "browser-downloaded.mp4"
                download_info.value.save_as(video_path)
                assert probe(video_path, "v:0") == "video"

                page.get_by_role("tab", name="封面").click()
                expect(
                    page.locator("#choice-list .choice-copy strong", has_text="原始封面")
                ).to_be_visible()
                page.get_by_role("button", name="开始传输").click()
                image_link = page.locator("#artifact-list .artifact-item", has_text=".jpg")
                expect(image_link).to_be_visible(timeout=90_000)
                with page.expect_download(timeout=30_000) as download_info:
                    image_link.click()
                image_path = ARTIFACT_DIR / "browser-downloaded.jpg"
                download_info.value.save_as(image_path)
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
