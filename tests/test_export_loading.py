"""P3-1「匯出 Word 加入載入狀態」的自動化測試。

涵蓋三個驗收條件：按鈕即時停用並顯示「產生中…」、載入狀態持續到伺服器真的
回應（不得以固定秒數計時器猜測）、以及沒有 JavaScript 時仍可直接下載。
同時驗證新的介面不會削弱既有的登入與跨帳號驗權。
"""

from collections.abc import Iterator
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from urllib.parse import unquote

from docx import Document
from fastapi.testclient import TestClient
import pytest

from app import auth
from app.main import create_app
from test_main import ORIGIN, submit

EXPORT_SCRIPT = "/static/export-loading.js"
# 檔名不得出現的路徑分隔符與保留字元。
FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'
HARNESS = Path(__file__).parent / "js" / "export_loading_harness.js"
SCRIPT_FILE = Path(__file__).parent.parent / "app" / "static" / "export-loading.js"


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


@pytest.fixture
def family(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    # 兩個瀏覽器各自登入同一個應用程式與資料庫，用於驗證跨帳號權限。
    first = {"username": "account-a", "password": secrets.token_urlsafe(24)}
    second = {"username": "account-b", "password": secrets.token_urlsafe(24)}
    settings = auth.Settings(
        username=first["username"], password_hash=auth.hasher.hash(first["password"]),
        partner_username=second["username"], partner_password_hash=auth.hasher.hash(second["password"]),
        secret=secrets.token_urlsafe(48), origin=ORIGIN,
    )
    app = create_app(settings, tmp_path / "family.db")
    with TestClient(app, base_url=ORIGIN) as owner, TestClient(app, base_url=ORIGIN) as partner:
        submit(owner, "/login", **first)
        submit(partner, "/login", **second)
        yield owner, partner


def create_workspace(client: TestClient, title: str = "週五會議") -> None:
    # 建立測試用四層結構，記事編號固定為 1。
    submit(client, "/workspace/notebook", name="工作記事")
    submit(client, "/workspace/section", parent_id=1, name="專案清單")
    submit(client, "/workspace/topic", parent_id=1, name="例會主題")
    submit(client, "/workspace/note", parent_id=1, name=title)


def test_note_export_button_uses_progressive_enhancement(client: TestClient, credentials: dict[str, str]) -> None:
    # 單則匯出改為按鈕並帶上匯出網址，同時保留無 JavaScript 的下載連結。
    submit(client, "/login", **credentials)
    create_workspace(client)
    page = client.get("/?kind=note&item_id=1").text
    assert re.search(r'<button type="button"[^>]*data-export-url="/export/notes/1\.docx"[^>]*hidden', page)
    assert re.search(r'<a href="/export/notes/1\.docx"[^>]*data-export-fallback', page)
    assert f'<script defer src="{EXPORT_SCRIPT}"></script>' in page


def test_notebook_export_button_uses_progressive_enhancement(client: TestClient, credentials: dict[str, str]) -> None:
    # 整本匯出同樣改為按鈕，並保留無 JavaScript 的下載連結。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作記事")
    page = client.get("/?kind=notebook&item_id=1").text
    assert re.search(r'<button type="button"[^>]*data-export-url="/export/notebooks/1\.docx"[^>]*hidden', page)
    assert re.search(r'<a[^>]*href="/export/notebooks/1\.docx"[^>]*data-export-fallback', page)
    assert f'<script defer src="{EXPORT_SCRIPT}"></script>' in page


def test_export_links_survive_without_javascript(client: TestClient, credentials: dict[str, str]) -> None:
    # 沒有 JavaScript 時降級連結必須留在畫面上並保留 href，且不被 noscript 包住。
    submit(client, "/login", **credentials)
    create_workspace(client)
    page = client.get("/?kind=note&item_id=1").text
    assert "<noscript" not in page
    assert 'href="/export/notes/1.docx"' in page
    # 按鈕預設隱藏，只有 JavaScript 啟動後才顯示，避免沒有作用的可點元素。
    assert re.search(r'<button type="button"[^>]*data-export-url="/export/notes/1\.docx"[^>]*hidden', page)


def test_workspace_drops_the_timer_based_inline_export_script(client: TestClient, credentials: dict[str, str]) -> None:
    # 舊的八秒計時器 inline script 必須移除，載入狀態改由外部程式檔負責。
    submit(client, "/login", **credentials)
    create_workspace(client)
    page = client.get("/?kind=note&item_id=1").text
    assert "<script>" not in page
    assert "setTimeout" not in page
    assert "8000" not in page
    assert "querySelectorAll('.export-btn')" not in page


def test_print_link_keeps_navigation_behaviour(client: TestClient, credentials: dict[str, str]) -> None:
    # 列印是導向另一頁，不是檔案下載，必須維持原本的連結行為。
    submit(client, "/login", **credentials)
    create_workspace(client)
    page = client.get("/?kind=note&item_id=1").text
    assert '<a href="/print/notes/1" class="export-btn" data-export-format="pdf">列印／另存 PDF</a>' in page
    assert 'data-export-url="/print/notes/1"' not in page


def test_export_script_sets_loading_state_and_uses_safe_requests(client: TestClient) -> None:
    # 驗證載入狀態的程式內容：停用、忙碌標記、錯誤提示與同源憑證。
    response = client.get(EXPORT_SCRIPT)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    source = response.text
    assert "credentials: 'same-origin'" in source
    assert "credentials: 'include'" not in source
    assert "'aria-busy'" in source
    assert "產生中…" in source
    assert "匯出失敗，請重試" in source
    assert "revokeObjectURL" in source
    assert "download" in source


def test_export_script_sanitizes_download_filename(client: TestClient) -> None:
    # 下載檔名必須過濾危險字元並限制長度，避免以記事標題衍生路徑穿越型檔名。
    source = client.get(EXPORT_SCRIPT).text
    assert "sanitizeFileName" in source
    classes = re.findall(r"/\[([^\]]*)\]/g", source)
    assert classes, "匯出檔名必須保留字元過濾規則"
    assert any(all(char in klass for char in FORBIDDEN_NAME_CHARS) for klass in classes), classes
    assert re.search(r"MAX[A-Z_]*NAME[A-Z_]*LENGTH\s*=\s*\d+", source)
    assert "decodeURIComponent" in source


def test_export_script_falls_back_when_utf8_file_name_is_broken(client: TestClient) -> None:
    # 百分號編碼壞掉時必須改用一般 filename 或預設名，不得直接使用原始編碼字串。
    source = client.get(EXPORT_SCRIPT).text
    assert "decodeFileName" in source
    assert re.search(r"if \(decoded\)\s*return sanitizeFileName\(decoded\);", source)
    assert "return value;" not in source


def test_export_script_waits_for_the_real_response_instead_of_a_fixed_timer(client: TestClient) -> None:
    # 載入狀態必須等到伺服器真的回應才恢復，不得用固定秒數計時器猜測完成時間。
    source = client.get(EXPORT_SCRIPT).text
    assert "await fetch(" in source
    assert "await response.blob()" in source
    assert "finally" in source
    assert source.count("setTimeout") == 1, "只允許釋放 blob 網址的計時器"
    timer = source.index("setTimeout")
    assert "revokeObjectURL" in source[timer:timer + 200]
    assert "8000" not in source


def test_export_script_keeps_the_blob_alive_long_enough_to_download(client: TestClient) -> None:
    # 釋放 blob 網址必須留足時間，否則大筆記本會在下載開始前就失效。
    source = client.get(EXPORT_SCRIPT).text
    matched = re.search(r"REVOKE_DELAY_MS\s*=\s*(\d+)", source)
    assert matched is not None
    assert int(matched.group(1)) >= 10000, "釋放 blob 網址的延遲太短會讓大型匯出下載失敗"


def test_export_script_does_not_download_print_pages_as_blob(client: TestClient) -> None:
    # 列印連結只做短暫回饋，不得改成 blob 下載。
    source = client.get(EXPORT_SCRIPT).text
    handler = source[source.index("function markPrintBusy"):]
    handler = handler[:handler.index("\n}")]
    assert "fetch(" not in handler
    assert "createObjectURL" not in handler


def test_export_endpoints_require_login(client: TestClient) -> None:
    # 未登入不得取得任何匯出或列印內容。
    assert client.get("/export/notes/1.docx").status_code == 401
    assert client.get("/export/notebooks/1.docx").status_code == 401
    assert client.get("/print/notes/1").status_code == 401


def test_export_endpoints_hide_other_users_notebooks(family: tuple[TestClient, TestClient]) -> None:
    # 跨帳號匯出必須回傳 404，新介面也不得洩漏他人匯出連結。
    owner, partner = family
    create_workspace(owner, "甲的私密記事")
    for path in ("/export/notes/1.docx", "/export/notebooks/1.docx", "/print/notes/1"):
        assert partner.get(path).status_code == 404, path
    assert "/export/notes/1.docx" not in partner.get("/").text


def test_export_file_name_has_no_path_separator_or_crlf(client: TestClient, credentials: dict[str, str]) -> None:
    # 含路徑穿越與換行的標題，下載檔名仍不得出現分隔符或注入的回頭資訊。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name='../../etc/passwd" \r\nX-Injected: 1')
    submit(client, "/workspace/section", parent_id=1, name="專案清單")
    submit(client, "/workspace/topic", parent_id=1, name="例會主題")
    submit(client, "/workspace/note", parent_id=1, name='../../秘密" \r\nY-Injected: 1')
    for path in ("/export/notes/1.docx", "/export/notebooks/1.docx"):
        response = client.get(path)
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert not any(ord(char) < 0x20 for char in disposition)
        match = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
        assert match is not None
        filename = unquote(match.group(1))
        assert "/" not in filename and "\\" not in filename
        assert "\r" not in filename and "\n" not in filename
        assert not any(ord(char) < 0x20 for char in filename)


def test_large_notebook_export_still_returns_every_note(client: TestClient, credentials: dict[str, str]) -> None:
    # 逾五十篇的大筆記本仍要完整匯出，載入狀態才有意義。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="大筆記本")
    submit(client, "/workspace/section", parent_id=1, name="專案清單")
    submit(client, "/workspace/topic", parent_id=1, name="例會主題")
    for index in range(55):
        submit(client, "/workspace/note", parent_id=1, name=f"第{index}篇")
    response = client.get("/export/notebooks/1.docx")
    assert response.status_code == 200
    titles = [paragraph.text for paragraph in Document(BytesIO(response.content)).paragraphs]
    for index in range(55):
        assert f"第{index}篇" in titles


def test_shared_notebook_export_keeps_working_for_partner(family: tuple[TestClient, TestClient]) -> None:
    # 家庭共用筆記本的夥伴仍可匯出，且介面上同樣提供按鈕與降級連結。
    owner, partner = family
    create_workspace(owner, "共用記事")
    submit(owner, "/workspace/notebook/1/share", is_shared="true")
    for path in ("/export/notes/1.docx", "/export/notebooks/1.docx"):
        assert partner.get(path).status_code == 200
    page = partner.get("/?kind=note&item_id=1").text
    assert 'data-export-url="/export/notes/1.docx"' in page
    assert 'data-export-fallback' in page


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_export_script_disables_the_button_until_the_file_arrives() -> None:
    # 實際執行匯出程式：按下後必須立刻停用並顯示「產生中…」，
    # 而且要等到伺服器真的回應才恢復，不得提早解除。
    result = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT_FILE)], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    normal = report["normal"]
    assert normal["afterSetup"]["fallbackHidden"] is True
    assert normal["afterSetup"]["buttonRevealed"] is True
    assert normal["busyNow"] == {"disabled": True, "ariaBusy": "true", "text": "產生中…"}
    assert normal["busyWhileWaiting"] is True
    assert normal["done"] == {"disabled": False, "ariaBusy": None, "text": "匯出 Word"}
    assert normal["fetchCalls"][0]["options"]["credentials"] == "same-origin"
    assert len(normal["downloads"]) == 1
    assert normal["downloads"][0]["name"] == "週五會議.docx"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_export_script_sanitizes_the_download_name_at_runtime() -> None:
    # 即使伺服器送來含路徑穿越的檔名，下載名稱仍必須被淨化。
    result = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT_FILE)], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    name = report["malicious"]["downloads"][0]["name"]
    assert "/" not in name and "\\" not in name
    assert '"' not in name
    assert not name.startswith(".")
    assert report["malicious"]["revoked"] == 0
    assert report["malicious"]["revokeDelay"] >= 10000


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能執行 JavaScript 行為測試")
def test_export_script_reports_failures_instead_of_failing_silently() -> None:
    # 伺服器回應失敗時要顯示錯誤訊息，且按鈕必須恢復可點擊。
    result = subprocess.run(
        ["node", str(HARNESS), str(SCRIPT_FILE)], capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    failure = json.loads(result.stdout)["failure"]
    assert failure["statusText"] == "匯出失敗，請重試"
    assert failure["disabled"] is False
