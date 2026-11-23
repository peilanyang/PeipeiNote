"""P3-2「手機版編輯器工具列虛擬鍵盤適配」的自動化測試。

涵蓋線上驗收的三個條件：輸入中工具列始終可見且不被虛擬鍵盤蓋住、橫向／直向
切換後會重新計算鍵盤高度，以及桌面版樣式不受影響。另依 2_資安與規矩.md §3 要求，
加入異常與惡意尺寸的防護測試，確保程式不會寫出無效的 CSS 數值，也不會開啟
任何注入或資料外傳管道。
"""

from collections.abc import Iterator
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from app import auth
from app.main import create_app
from test_main import ORIGIN, submit

VIEWPORT_SCRIPT = "/static/editor-viewport.js"
HARNESS = Path(__file__).parent / "js" / "editor_viewport_harness.js"
SCRIPT_FILE = Path(__file__).parent.parent / "app" / "static" / "editor-viewport.js"
CSS_FILE = Path(__file__).parent.parent / "app" / "static" / "workspace.css"
# 手機版樣式區塊的媒體查詢條件。
MOBILE_QUERY = "max-width: 640px"
# 視窗適配使用的 CSS 變數名稱，也用來確認第三方套件沒有被塞入這段邏輯。
KEYBOARD_INSET_VARIABLE = "--keyboard-inset"
TOOLBAR_HEIGHT_VARIABLE = "--toolbar-height"
# 程式不得使用的注入、資料外傳與 DOM 改寫管道。
FORBIDDEN_CHANNELS = (
    "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
    "eval", "fetch", "XMLHttpRequest", "navigator.sendBeacon", "WebSocket",
    "appendChild", "removeChild", "insertBefore", "replaceChild",
)


@pytest.fixture
def credentials() -> dict[str, str]:
    # 每次產生測試密碼，避免版本庫留下固定憑證。
    return {"username": "owner", "password": secrets.token_urlsafe(24)}


@pytest.fixture
def client(tmp_path: Path, credentials: dict[str, str]) -> Iterator[TestClient]:
    # 以臨時 SQLite 啟動完整應用程式。
    settings = auth.Settings(
        username=credentials["username"], password_hash=auth.hasher.hash(credentials["password"]),
        secret=secrets.token_urlsafe(48), origin=ORIGIN,
    )
    with TestClient(create_app(settings, tmp_path / "test.db"), base_url=ORIGIN) as browser:
        yield browser


def create_workspace(client: TestClient, title: str = "週五會議") -> None:
    # 建立測試用四層結構，記事編號固定為 1。
    submit(client, "/workspace/notebook", name="工作記事")
    submit(client, "/workspace/section", parent_id=1, name="專案清單")
    submit(client, "/workspace/topic", parent_id=1, name="例會主題")
    submit(client, "/workspace/note", parent_id=1, name=title)


def read_css() -> str:
    # 讀取桌面版與手機版共用的樣式表。
    return CSS_FILE.read_text(encoding="utf-8")


def extract_media_blocks(css: str, condition: str) -> str:
    """取出所有符合指定媒體查詢的區塊內容（以大括號配對）。"""
    needle = f"@media ({condition})"
    blocks: list[str] = []
    index = css.find(needle)
    while index != -1:
        start = css.find("{", index)
        if start == -1:
            break
        depth = 0
        end = -1
        for position in range(start, len(css)):
            if css[position] == "{":
                depth += 1
            elif css[position] == "}":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        if end == -1:
            break
        blocks.append(css[start + 1:end])
        index = css.find(needle, end)
    return "\n".join(blocks)


def run_harness() -> dict:
    # 以 node 實際執行編輯器視窗程式，取得各情境的 CSS 變數快照。
    result = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT_FILE)], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# 正常案例：頁面引入與靜態檔
# ---------------------------------------------------------------------------

def test_workspace_page_loads_the_viewport_script(client: TestClient, credentials: dict[str, str]) -> None:
    # 工作區的記事編輯頁必須載入視窗適配程式，鍵盤彈出時工具列才會上移。
    submit(client, "/login", **credentials)
    create_workspace(client)
    page = client.get("/?kind=note&item_id=1").text
    assert f'<script defer src="{VIEWPORT_SCRIPT}"></script>' in page


def test_library_page_loads_the_viewport_script(client: TestClient, credentials: dict[str, str]) -> None:
    # 範本管理頁沿用同一個編輯器，必須同樣載入視窗適配程式。
    submit(client, "/login", **credentials)
    page = client.get("/library/templates").text
    assert f'<script defer src="{VIEWPORT_SCRIPT}"></script>' in page


def test_viewport_script_is_served_as_javascript(client: TestClient) -> None:
    # 靜態檔必須能正常取得，並以 JavaScript 的型別回傳。
    response = client.get(VIEWPORT_SCRIPT)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_viewport_script_does_not_replace_the_editor_bundle(client: TestClient) -> None:
    # 視窗適配必須是獨立檔案，不得把邏輯塞進第三方的 TipTap 套件。
    source = SCRIPT_FILE.read_text(encoding="utf-8")
    assert "tiptap" not in source.lower()
    bundle = (SCRIPT_FILE.parent / "editor.bundle.js").read_text(encoding="utf-8")
    assert KEYBOARD_INSET_VARIABLE not in bundle
    assert TOOLBAR_HEIGHT_VARIABLE not in bundle


# ---------------------------------------------------------------------------
# 樣式斷言：手機版固定在鍵盤上方，桌面版不動
# ---------------------------------------------------------------------------

def test_mobile_toolbar_is_fixed_and_uses_the_keyboard_inset() -> None:
    # 手機版工具列改用 fixed，並以鍵盤高度變數決定底部位移。
    css = read_css()
    mobile = extract_media_blocks(css, MOBILE_QUERY)
    assert "position: fixed" in mobile
    assert "bottom: var(--keyboard-inset, 0px)" in mobile
    assert "left: 0" in mobile and "right: 0" in mobile
    assert "--keyboard-inset" in mobile


def test_mobile_toolbar_no_longer_sticks_to_the_bottom() -> None:
    # sticky 在 iOS 虛擬鍵盤下不會跟著移動，手機版不得再使用。
    mobile = extract_media_blocks(read_css(), MOBILE_QUERY)
    assert "position: sticky" not in mobile


def test_focus_within_static_position_removed() -> None:
    # focus-within 讓工具列在打字時離開固定位置，違反驗收條件，必須整條刪除。
    css = read_css()
    assert "focus-within" not in css


def test_desktop_toolbar_rules_are_untouched() -> None:
    # 桌面版仍使用原本的 sticky 定位，不得受到手機版改動影響。
    css = read_css()
    assert "@media (min-width:900px)" in css
    assert ".note-form .rich-toolbar{z-index:1;position:sticky;bottom:0}" in css
    assert ".note-page .rich-toolbar{z-index:2;top:0;bottom:auto}" in css
    desktop = extract_media_blocks(css, "min-width:900px")
    assert "--keyboard-inset" not in desktop
    assert "position: fixed" not in desktop


def test_mobile_content_reserves_room_for_the_fixed_toolbar() -> None:
    # 工具列不再佔據版面，內容必須補上等高的空白避免壓住最後一段。
    mobile = extract_media_blocks(read_css(), MOBILE_QUERY)
    assert "padding-bottom: calc(var(--toolbar-height, 3.25rem) + 1rem)" in mobile


def test_safe_area_padding_does_not_double_count_the_keyboard() -> None:
    # 鍵盤彈出時安全區內距要被扣掉，否則工具列會離鍵盤過遠。
    css = read_css()
    assert "calc(env(safe-area-inset-bottom, 0px) - var(--keyboard-inset, 0px))" in css


def test_fixed_toolbar_keeps_its_content_off_the_screen_edge() -> None:
    # 工具列固定到螢幕邊緣後，左右仍要留白，否則按鈕會貼齊畫面邊界。
    mobile = extract_media_blocks(read_css(), MOBILE_QUERY)
    assert "padding-inline: clamp(1rem, 2.4vw, 2rem)" in mobile


def test_fixed_position_is_only_used_on_small_screens() -> None:
    # 固定定位只允許出現在手機版區塊，桌面與平板維持原本的版面行為。
    css = read_css()
    mobile = extract_media_blocks(css, MOBILE_QUERY)
    assert mobile.count("position: fixed") == 1
    assert css.count("position: fixed") == 1
    # 壓縮過的基礎樣式不得另行固定元素。
    assert "position:fixed" not in css.split("\n")[1]


def test_keyboard_inset_environment_fallback_is_applied() -> None:
    # 依驗收清單補上 keyboard-inset-height 的漸進式增強，取兩者較大值。
    css = read_css()
    assert "@supports (bottom: env(keyboard-inset-height))" in css
    assert "max(var(--keyboard-inset, 0px), env(keyboard-inset-height, 0px))" in css


# ---------------------------------------------------------------------------
# 原始碼安全斷言：不得開啟注入或資料外傳管道
# ---------------------------------------------------------------------------

def test_viewport_script_has_no_injection_or_network_channels() -> None:
    # 程式只讀取尺寸，不得改寫 DOM 或送出任何網路請求。
    source = SCRIPT_FILE.read_text(encoding="utf-8")
    for channel in FORBIDDEN_CHANNELS:
        assert not re.search(rf"\b{re.escape(channel)}\b", source), channel


def test_viewport_script_clamps_sizes_before_writing_css() -> None:
    # 寫入 CSS 變數前必須檢查數值，NaN 或負值不得進入樣式。
    source = SCRIPT_FILE.read_text(encoding="utf-8")
    assert "Number.isFinite" in source
    assert "Math.max(0" in source
    assert "removeProperty" in source


# ---------------------------------------------------------------------------
# 行為案例：以 node 實際執行編輯器視窗程式
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_keyboard_inset_is_written_when_the_keyboard_opens() -> None:
    # 鍵盤彈出時可視區域縮減，工具列必須被抬高到鍵盤上方。
    report = run_harness()
    assert report["keyboardOpen"]["inset"] == "400px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_keyboard_inset_returns_to_zero_when_the_keyboard_closes() -> None:
    # 鍵盤收起後必須歸零，工具列回到貼齊底部。
    report = run_harness()
    assert report["keyboardClosed"]["inset"] == "0px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_visual_viewport_scroll_updates_the_keyboard_inset() -> None:
    # iOS 捲動時 offsetTop 會變動，值必須跟著更新。
    report = run_harness()
    assert report["scroll"]["initial"] == "150px"
    assert report["scroll"]["afterScroll"] == "100px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_toolbar_height_is_published_from_the_resize_observer() -> None:
    # 工具列高度由 ResizeObserver 量測，提供內容補償所需的高度。
    report = run_harness()
    assert report["toolbar"]["afterReport"] == "52px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_rotation_recalculates_the_keyboard_inset() -> None:
    # 橫向／直向切換後視窗高度改變，必須重新計算鍵盤高度。
    report = run_harness()
    assert report["rotation"]["before"] == "400px"
    assert report["rotation"]["after"] == "100px"
    assert report["rotation"]["afterResize"] == "100px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_malformed_viewport_sizes_never_produce_invalid_css_values() -> None:
    # 惡意或異常尺寸（負值、NaN、Infinity、非數字）必須被夾住並視為沒有鍵盤。
    report = run_harness()
    malformed = report["malformed"]
    assert malformed["nan"] == "0px"
    assert malformed["negative"] == "0px"
    assert malformed["text"] == "0px"
    assert malformed["infinity"] == "0px"
    assert malformed["windowNaN"] == "0px"
    assert malformed["offsetTopNegative"] == "0px"
    for value in malformed["variables"].values():
        assert "NaN" not in value
        assert not value.startswith("-")


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_unmeasurable_toolbar_height_removes_the_css_variable() -> None:
    # 量不到工具列高度（0 或負數）時必須移除變數，讓樣式表使用預設值。
    report = run_harness()
    toolbar = report["toolbar"]
    assert toolbar["initial"] is None
    assert toolbar["afterZero"] is None
    assert toolbar["afterNegative"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_toolbar_mounted_after_the_script_is_still_measured() -> None:
    # 工具列若比程式晚一步出現在畫面上，之後仍要量得到高度。
    report = run_harness()
    assert report["lateToolbar"]["before"] is None
    assert report["lateToolbar"]["after"] == "48px"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_old_browsers_fall_back_without_raising_errors() -> None:
    # 沒有 visualViewport 或 ResizeObserver 時不得拋出例外，安靜退回底部貼齊。
    report = run_harness()
    # 沒有 visualViewport：無法推算鍵盤高度，一律視為沒有鍵盤。
    assert report["noViewport"]["inset"] == "0px"
    # 沒有 ResizeObserver：鍵盤高度仍可計算，工具列高度則交由樣式表預設值。
    assert report["noObserver"]["inset"] == "400px"
    assert report["noObserver"]["toolbar"] is None
