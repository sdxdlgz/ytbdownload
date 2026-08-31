from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Page, Route, expect, sync_playwright

BASE_URL = "http://127.0.0.1:8765"
ARTIFACT_DIR = Path("artifacts/browser")
ANALYSIS_ID = "11111111-1111-4111-8111-111111111111"
CHOICE_VIDEO = "22222222-2222-4222-8222-222222222222"
CHOICE_AUDIO = "33333333-3333-4333-8333-333333333333"
CHOICE_COVER = "44444444-4444-4444-8444-444444444444"
JOB_ID = "55555555-5555-4555-8555-555555555555"
ARTIFACT_ID = "66666666-6666-4666-8666-666666666666"


def iso(offset_hours: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(hours=offset_hours)).isoformat()


def analysis_record() -> dict:
    return {
        "id": ANALYSIS_ID,
        "status": "completed",
        "playlist": False,
        "created_at": iso(),
        "updated_at": iso(),
        "expires_at": iso(1),
        "error": None,
        "result": {
            "kind": "single",
            "id": "visual-fixture",
            "extractor": "YouTube",
            "platform": "YouTube",
            "title": "A Field Guide to Signals — 4K Workshop Film",
            "uploader": "Signal Archive",
            "duration": 754,
            "description": "A visual test fixture used to verify the complete media inspection and transfer interface.",
            "thumbnail": {
                "url": "https://images.unsplash.com/photo-1519608487953-e999c86e7455?auto=format&fit=crop&w=1400&q=85",
                "width": 1400,
                "height": 788,
                "id": "cover",
            },
            "formats": [
                {
                    "format_id": "401",
                    "ext": "mp4",
                    "height": 2160,
                    "has_video": True,
                    "has_audio": False,
                },
                {
                    "format_id": "137",
                    "ext": "mp4",
                    "height": 1080,
                    "has_video": True,
                    "has_audio": False,
                },
                {
                    "format_id": "22",
                    "ext": "mp4",
                    "height": 720,
                    "has_video": True,
                    "has_audio": True,
                },
            ],
            "subtitles": [
                {"code": "zh-Hans", "name": "简体中文", "kind": "manual"},
                {"code": "en", "name": "English", "kind": "automatic"},
            ],
            "choices": [
                {
                    "id": CHOICE_VIDEO,
                    "kind": "video",
                    "policy": "best",
                    "label": "最佳画质",
                    "description": "自动选择最佳视频与音频；优先 MP4，必要时使用 MKV。",
                    "badge": "AUTO",
                },
                {
                    "id": "77777777-7777-4777-8777-777777777777",
                    "kind": "video",
                    "policy": "resolution",
                    "label": "最高 1080p",
                    "description": "将画面限制在 1080p 以内并自动合并音频。",
                    "height": 1080,
                    "badge": "1080P",
                },
                {
                    "id": "88888888-8888-4888-8888-888888888888",
                    "kind": "video",
                    "policy": "exact",
                    "technical": True,
                    "format_id": "137",
                    "label": "1080p · 30fps · 需合并音频 · MP4",
                    "description": "137 · avc1 · 54.3 MB · HD",
                    "expected_size": 56_938_000,
                    "badge": "MP4",
                },
                {
                    "id": CHOICE_AUDIO,
                    "kind": "audio",
                    "policy": "audio",
                    "codec": "mp3",
                    "label": "MP3 音频",
                    "description": "通用兼容，使用 ffmpeg 转换为高质量 MP3。",
                    "badge": "MP3",
                },
                {
                    "id": CHOICE_COVER,
                    "kind": "thumbnail",
                    "policy": "thumbnail",
                    "format": "original",
                    "label": "原始封面",
                    "description": "单独保存最高质量封面图。",
                    "badge": "COVER",
                },
            ],
            "restriction": None,
            "webpage_domain": "youtube.com",
        },
    }


def job_record(status: str, progress: float) -> dict:
    artifacts = []
    error = None
    phase = {
        "queued": "queued",
        "running": "downloading",
        "postprocessing": "postprocessing:ffmpegextractaudio",
        "completed": "ready",
    }[status]
    if status == "completed":
        artifacts = [
            {
                "id": ARTIFACT_ID,
                "filename": "A Field Guide to Signals.mp3",
                "size": 18_420_000,
                "media_type": "audio/mpeg",
                "sha256": "a" * 64,
                "primary": True,
                "created_at": iso(),
                "expires_at": iso(12),
                "download_url": f"/api/v1/artifacts/{ARTIFACT_ID}",
            }
        ]
    return {
        "id": JOB_ID,
        "analysis_id": ANALYSIS_ID,
        "status": status,
        "phase": phase,
        "progress": progress,
        "downloaded_bytes": int(18_420_000 * progress / 100),
        "total_bytes": 18_420_000,
        "speed": 3_400_000 if status == "running" else None,
        "eta": 4 if status == "running" else None,
        "playlist_index": None,
        "playlist_count": None,
        "title": "A Field Guide to Signals — 4K Workshop Film",
        "platform": "YouTube",
        "choice": {
            "id": CHOICE_AUDIO,
            "kind": "audio",
            "policy": "audio",
            "codec": "mp3",
            "label": "MP3 音频",
        },
        "artifacts": artifacts,
        "created_at": iso(),
        "updated_at": iso(),
        "started_at": iso(),
        "completed_at": iso() if status == "completed" else None,
        "expires_at": iso(12),
        "cancel_requested": False,
        "error": error,
    }


def fulfill_json(route: Route, payload: dict, status: int = 200) -> None:
    route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))


def install_mock_flow(page: Page) -> None:
    poll_count = {"value": 0}
    current_job = {"value": None}

    def analyses(route: Route) -> None:
        fulfill_json(route, analysis_record(), 202)

    def jobs(route: Route) -> None:
        request_path = route.request.url.split("?", 1)[0].rstrip("/")
        is_collection = request_path.endswith("/api/v1/jobs")
        if route.request.method == "POST":
            current_job["value"] = job_record("queued", 0)
            fulfill_json(route, current_job["value"], 202)
            return
        if is_collection:
            items = [current_job["value"]] if current_job["value"] else []
            fulfill_json(route, {"items": items, "total": len(items), "limit": 50, "offset": 0})
            return
        poll_count["value"] += 1
        if poll_count["value"] == 1:
            current_job["value"] = job_record("running", 43)
        elif poll_count["value"] == 2:
            current_job["value"] = job_record("postprocessing", 96)
        else:
            current_job["value"] = job_record("completed", 100)
        fulfill_json(route, current_job["value"])

    page.route(f"{BASE_URL}/api/v1/analyses", analyses)
    page.route(f"{BASE_URL}/api/v1/jobs**", jobs)


def assert_no_horizontal_overflow(page: Page) -> None:
    dimensions = page.evaluate(
        """() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth})"""
    )
    assert dimensions["scroll"] <= dimensions["client"] + 1, dimensions


def run() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    browser_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        desktop = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        desktop.on(
            "console",
            lambda message: (
                browser_errors.append(f"console:{message.text}")
                if message.type == "error"
                else None
            ),
        )
        desktop.on("pageerror", lambda error: browser_errors.append(f"page:{error}"))
        install_mock_flow(desktop)
        desktop.goto(BASE_URL, wait_until="networkidle")
        expect(desktop.get_by_role("heading", name="CAPTURE EVERY FRAME.")).to_be_visible()
        expect(desktop.get_by_role("button", name="解析链接")).to_be_visible()
        desktop.wait_for_timeout(1_400)
        assert_no_horizontal_overflow(desktop)
        desktop.screenshot(path=str(ARTIFACT_DIR / "home-desktop.png"), full_page=True)

        desktop.get_by_label("媒体链接").fill("https://www.youtube.com/watch?v=fixture")
        desktop.get_by_role("button", name="解析链接").click()
        expect(
            desktop.get_by_text("A Field Guide to Signals — 4K Workshop Film", exact=True)
        ).to_be_visible()
        assert_no_horizontal_overflow(desktop)
        expect(
            desktop.locator("#choice-list .choice-copy strong", has_text="最佳画质")
        ).to_be_visible()
        desktop.screenshot(path=str(ARTIFACT_DIR / "analysis-desktop.png"), full_page=True)

        recommended_tab = desktop.get_by_role("tab", name="推荐")
        recommended_tab.focus()
        desktop.keyboard.press("ArrowRight")
        expect(desktop.get_by_role("tab", name="具体格式")).to_have_attribute(
            "aria-selected", "true"
        )
        desktop.keyboard.press("ArrowRight")
        expect(desktop.get_by_role("tab", name="音频")).to_have_attribute("aria-selected", "true")

        desktop.get_by_role("tab", name="音频").click()
        expect(
            desktop.locator("#choice-list .choice-copy strong", has_text="MP3 音频")
        ).to_be_visible()
        desktop.get_by_role("button", name="开始传输").click()
        expect(desktop.get_by_text("文件已经落地。", exact=True)).to_be_visible(timeout=10_000)
        expect(desktop.get_by_text("A Field Guide to Signals.mp3", exact=True)).to_be_visible()
        assert_no_horizontal_overflow(desktop)
        desktop.screenshot(path=str(ARTIFACT_DIR / "completed-desktop.png"), full_page=True)

        desktop.get_by_role("button", name="传输记录").click()
        expect(desktop.get_by_role("heading", name="传输记录", exact=True)).to_be_visible()
        expect(
            desktop.locator("#history-list .history-item h3", has_text="A Field Guide to Signals")
        ).to_be_visible()
        desktop.wait_for_timeout(450)
        desktop.screenshot(path=str(ARTIFACT_DIR / "history-drawer.png"), full_page=False)
        desktop.get_by_role("button", name="关闭传输记录").click()

        mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        mobile.on(
            "console",
            lambda message: (
                browser_errors.append(f"mobile-console:{message.text}")
                if message.type == "error"
                else None
            ),
        )
        mobile.on("pageerror", lambda error: browser_errors.append(f"mobile-page:{error}"))
        queued_record = analysis_record()
        queued_record["status"] = "queued"
        queued_record["result"] = None
        mobile.route(
            f"{BASE_URL}/api/v1/analyses",
            lambda route: fulfill_json(route, queued_record, 202),
        )
        mobile.route(
            f"{BASE_URL}/api/v1/analyses/{ANALYSIS_ID}",
            lambda route: fulfill_json(route, queued_record),
        )
        mobile.goto(BASE_URL, wait_until="networkidle")
        expect(mobile.get_by_role("button", name="解析链接")).to_be_visible()
        expect(mobile.get_by_role("button", name="传输记录", exact=True)).to_be_visible()
        expect(mobile.get_by_role("button", name="从剪贴板粘贴链接", exact=True)).to_be_visible()
        mobile.wait_for_timeout(1_400)
        assert_no_horizontal_overflow(mobile)
        mobile.screenshot(path=str(ARTIFACT_DIR / "home-mobile.png"), full_page=True)
        mobile.get_by_label("媒体链接").fill("https://example.com/pending")
        mobile.get_by_role("button", name="解析链接").click()
        expect(mobile.get_by_text("正在向来源站点询问信号…", exact=True)).to_be_visible()
        mobile.get_by_role("button", name="分析另一个链接").click()
        analyze_again = mobile.get_by_role("button", name="解析链接")
        expect(analyze_again).to_be_enabled()
        assert "is-loading" not in (analyze_again.get_attribute("class") or "")

        auth_page = browser.new_page(viewport={"width": 900, "height": 800})
        auth_page.on(
            "console",
            lambda message: (
                browser_errors.append(f"auth-console:{message.text}")
                if message.type == "error"
                else None
            ),
        )
        auth_page.on("pageerror", lambda error: browser_errors.append(f"auth-page:{error}"))
        auth_page.route(
            f"{BASE_URL}/api/v1/session",
            lambda route: fulfill_json(route, {"auth_required": True, "authenticated": False}),
        )
        auth_page.route(
            f"{BASE_URL}/api/v1/auth/session",
            lambda route: fulfill_json(route, {"auth_required": True, "authenticated": True}),
        )
        auth_page.goto(BASE_URL, wait_until="networkidle")
        auth_dialog = auth_page.get_by_role("dialog", name="这台下载器需要通行码。")
        expect(auth_dialog).to_be_visible()
        auth_page.screenshot(path=str(ARTIFACT_DIR / "auth-dialog.png"), full_page=False)
        auth_page.locator("#token-input").fill("browser-test-token")
        auth_page.get_by_role("button", name="解锁工作台").click()
        expect(auth_dialog).not_to_be_visible()
        browser.close()

    filtered = [error for error in browser_errors if "favicon" not in error.lower()]
    assert not filtered, filtered
    print(f"Browser smoke PASS; screenshots: {ARTIFACT_DIR}")


if __name__ == "__main__":
    run()
