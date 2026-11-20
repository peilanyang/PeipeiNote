from collections.abc import Iterator
from pathlib import Path
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app

ORIGIN = "https://peipeinote.test"


@pytest.fixture
def credentials() -> dict[str, str]:
    # 每次產生測試用密碼，避免在版本庫留下固定憑證。
    return {"username": "owner", "password": secrets.token_urlsafe(24)}


@pytest.fixture
def client(tmp_path: Path, credentials: dict[str, str]) -> Iterator[TestClient]:
    # 以臨時 SQLite 與測試站台啟動完整應用程式。
    settings = auth.Settings(
        username=credentials["username"], password_hash=auth.hasher.hash(credentials["password"]),
        secret=secrets.token_urlsafe(48), origin=ORIGIN,
    )
    with TestClient(create_app(settings, tmp_path / "test.db"), base_url=ORIGIN) as browser:
        yield browser


def test_login_logout_and_cookie(client: TestClient, credentials: dict[str, str]) -> None:
    # 驗證登入表單、Cookie 安全屬性、登入後首頁與伺服器端登出。
    blocked = client.get("/", follow_redirects=False)
    assert (blocked.status_code, blocked.headers["location"]) == (303, "/login")
    assert "已登入" not in blocked.text
    assert 'name="password"' in client.get("/login").text
    response = client.post("/login", data=credentials, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", f"max-age={auth.COOKIE_SECONDS}"))
    token = client.cookies[auth.COOKIE]
    assert "已登入" in client.get("/").text
    assert client.post("/logout", headers={"Origin": ORIGIN}, follow_redirects=False).status_code == 303
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_lockout(client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch,
                 caplog: pytest.LogCaptureFixture) -> None:
    # 同一 IP 第五次失敗立即鎖定，15 分鐘後才接受正確密碼。
    clock = SimpleNamespace(now=10_000)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock.now))
    headers = {"Origin": ORIGIN}
    invalid = {**credentials, "password": secrets.token_urlsafe(24)}
    for _ in range(4):
        assert client.post("/login", data=invalid, headers=headers).status_code == 401
    locked = client.post("/login", data=invalid, headers=headers)
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "900"
    assert "已鎖定 IP" in caplog.text
    clock.now += 899
    assert client.post("/login", data=credentials, headers=headers).headers["retry-after"] == "1"
    clock.now += 1
    assert client.post("/login", data=credentials, headers=headers, follow_redirects=False).status_code == 303


def test_csrf_and_invalid_session(client: TestClient, credentials: dict[str, str]) -> None:
    # 跨站或缺少來源時拒絕變更登入狀態，無效 Cookie 無法讀取首頁。
    assert client.post("/login", data=credentials, headers={"Origin": "https://elsewhere.test"}).status_code == 403
    assert client.post("/login", data=credentials).status_code == 403
    assert client.post("/logout", headers={"Origin": "null"}).status_code == 403
    client.cookies.set(auth.COOKIE, secrets.token_urlsafe(32))
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_survives_past_original_timeout(client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # 模擬超過原本的 8 小時，確認登入仍有效。
    clock = SimpleNamespace(now=10_000)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock.now))
    client.post("/login", data=credentials, headers={"Origin": ORIGIN}).raise_for_status()
    assert "已登入" in client.get("/").text
    clock.now += 30 * 24 * 60 * 60
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "已登入" in response.text
    assert f"Max-Age={auth.COOKIE_SECONDS}" in response.headers["set-cookie"]


def test_unconfigured_app(tmp_path: Path) -> None:
    # 貼上範本或遺失設定時，服務顯示初始化提示並拒絕登入。
    url = auth.Settings.origin
    with TestClient(create_app(auth.Settings(), tmp_path / "test.db"), base_url=url) as browser:
        assert "帳號設定指令" in browser.get("/login").text
        assert browser.post("/login", headers={"Origin": url}).status_code == 503


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/register"), ("POST", "/signup"), ("GET", "/.env"),
     ("GET", "/uploads/private.pdf"), ("GET", "/openapi.json")],
)
def test_private_paths_are_not_exposed(client: TestClient, method: str, path: str) -> None:
    # 確認公開註冊、機密設定、上傳路徑及 API 文件均未開放。
    assert client.request(method, path).status_code == 404

def submit(client: TestClient, path: str, **fields: str | int) -> str:
    # 送出同源表單並傳回成功後的工作區網址。
    response = client.post(path, data=fields, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_workspace_creates_edits_moves_and_deletes_all_levels(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 從登入後的三欄介面建立四層資料，逐層改名、移動和刪除。
    submit(client, "/login", **credentials)
    assert 'class="workspace"' in client.get("/").text
    names = ("第一本", "第二本")
    first_book = submit(client, "/workspace/notebook", name=names[0])
    second_book = submit(client, "/workspace/notebook", name=names[1])
    submit(client, "/workspace/notebook/2/move", direction=-1)
    ordered = client.get("/").text
    assert ordered.index(names[1]) < ordered.index(names[0])
    submit(client, "/workspace/notebook/1/save", name="工作筆記本")
    assert "工作筆記本" in client.get(first_book).text

    section = submit(client, "/workspace/section", parent_id=1, name="工作清單")
    other_section = submit(client, "/workspace/section", parent_id=2, name="其他清單")
    submit(client, "/workspace/section/1/save", name="專案清單")
    submit(client, "/workspace/section/1/move", target_id=2)
    section_page = client.get(section).text
    assert "專案清單" in section_page and 'item_id=2" aria-current="page"' in client.get(second_book).text
    assert '<details open>' in section_page

    topic = submit(client, "/workspace/topic", parent_id=1, name="工作主題")
    destination = submit(client, "/workspace/topic", parent_id=2, name="另一個主題")
    submit(client, "/workspace/topic/1/save", name="新主題")
    submit(client, "/workspace/topic/1/move", target_id=2)
    assert "新主題" in client.get(topic).text

    note = submit(client, "/workspace/note", parent_id=1, name="我的筆記")
    title = "O'Reilly & 研究筆記"
    body = "\n測試 <標記> 與引號\n第二行"
    submit(client, "/workspace/note/1/save", name=title, content=body)
    editor = client.get(note).text
    assert "O&#39;Reilly &amp; 研究筆記" in editor
    assert "\n測試 &lt;標記&gt; 與引號\n第二行" in editor
    assert 'action="/workspace/note/1/save"' in editor
    assert 'action="/logout"' in editor
    submit(client, "/workspace/note/1/move", target_id=2)
    assert "O&#39;Reilly" not in client.get(topic).text
    assert "O&#39;Reilly" in client.get(destination).text

    assert submit(client, "/workspace/note/1/delete", confirmed="true") == destination
    assert submit(client, "/workspace/topic/2/delete", confirmed="true") == other_section
    assert submit(client, "/workspace/topic/1/delete", confirmed="true") == other_section
    assert submit(client, "/workspace/section/2/delete", confirmed="true") == second_book
    assert submit(client, "/workspace/section/1/delete", confirmed="true") == second_book
    assert submit(client, "/workspace/notebook/1/delete", confirmed="true") == "/"
    assert submit(client, "/workspace/notebook/2/delete", confirmed="true") == "/"
    assert "先從左欄新增一本筆記本" in client.get("/").text


def test_workspace_guards_mutations_and_preserves_children(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 未登入、跨來源、未確認刪除與無效的父層都不能寫入資料。
    assert client.post("/workspace/notebook", data={"name": "測試筆記本"}, headers={"Origin": ORIGIN}).status_code == 401
    submit(client, "/login", **credentials)
    assert client.post("/workspace/notebook", data={"name": "跨站"}, headers={"Origin": "https://elsewhere.test"}).status_code == 403
    book = submit(client, "/workspace/notebook", name="原有筆記本")
    submit(client, "/workspace/section", name="要保留的清單", parent_id=1)
    for action, data in [
        ("save", {"name": "新名稱"}), ("move", {"direction": "1"}), ("delete", {"confirmed": "true"}),
    ]:
        assert client.post(f"/workspace/notebook/1/{action}", data=data).status_code == 403
    headers = {"Origin": ORIGIN}
    assert client.post("/workspace/notebook/1/delete", headers=headers).status_code == 422
    assert client.post("/workspace/notebook/1/delete", headers=headers, data={"confirmed": "true"}).status_code == 409
    assert client.post("/workspace/topic", headers=headers, data={"name": "孤立主題", "parent_id": 250}).status_code == 404
    assert client.post("/workspace/section/1/move", headers=headers, data={"target_id": 250}).status_code == 404
    result = client.get(book).text
    assert "原有筆記本" in result and "要保留的清單" in result
    assert "跨站" not in result


def test_workspace_sanitizes_html_before_storing(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 驗證真正的儲存入口與 SQLite 欄位，舊格式的純文字入口仍有原有行為。
    from sqlite3 import connect

    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="格式")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    note = submit(client, "/workspace/note", parent_id=1, name="內容")
    submit(client, "/workspace/note/1/save", name="內容", content="<b>舊純文字</b>")
    assert "&lt;b&gt;舊純文字&lt;/b&gt;" in client.get(note).text
    submit(
        client, "/workspace/note/1/save", name="內容", content_format="html",
        content='<h1 onerror="">段落</h1><script type="text/plain">標記</script>',
    )
    with connect(tmp_path / "test.db") as db:
        assert db.execute("SELECT content, content_format FROM notes").fetchone() == ("<h1>段落</h1>", "html")
    result = client.get(note).text
    assert "&lt;h1&gt;段落&lt;/h1&gt;" in result
    assert "/static/editor.bundle.js" in result
    assert client.get("/static/editor.bundle.js").status_code == 200


def test_private_attachments_lifecycle(
    client: TestClient, credentials: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 驗證檔案識別、大小、CSRF、圖片內嵌與登入後的下載和移除。
    from io import BytesIO
    from urllib.parse import quote
    from PIL import Image
    from app import attachments

    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="附件")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    note = submit(client, "/workspace/note", parent_id=1, name="筆記")
    upload = "/api/notes/1/files"
    headers = {"Origin": ORIGIN}
    fake = client.post(upload, content=b"plain text", headers={**headers, "X-Filename": quote("假.pdf")})
    assert fake.status_code == 415
    assert client.post(upload, headers={"X-Filename": "file.txt"}, content=b"hey").status_code == 403
    with monkeypatch.context() as limits:
        limits.setattr(attachments, "MAX_FILE_BYTES", 3)
        assert client.post(upload, content=b"four", headers={**headers, "X-Filename": quote("file.txt")}).status_code == 413
    image = BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="PNG")
    picture = client.post(upload, content=image.getvalue(), headers={**headers, "X-Filename": quote("圖片.png")})
    assert picture.status_code == 201
    info = picture.json()
    assert attachments.FILE_ID.fullmatch(info["id"])
    assert info["name"] == "圖片.png"
    assert (tmp_path / "uploads" / info["id"]).read_bytes() == image.getvalue()
    picture_url = info["url"]
    assert client.get(picture_url).headers["content-disposition"].startswith("inline")
    assert client.get(picture_url + "?download=true").headers["content-disposition"].startswith("attachment")
    disguised = client.post(
        upload, content=b"%PDF-1.7\n%%EOF\n", headers={**headers, "X-Filename": quote("假.pdf")},
    )
    assert disguised.status_code == 415
    document = BytesIO()
    Image.new("RGB", (2, 2)).save(document, format="PDF")
    pdf = client.post(upload, content=document.getvalue(), headers={**headers, "X-Filename": quote("手冊.pdf")})
    assert pdf.status_code == 201
    pdf_url = pdf.json()["url"]
    assert client.get(pdf_url).headers["content-disposition"].startswith("attachment")
    assert "手冊.pdf" in client.get(note).text

    submit(client, "/workspace/note/1/save", name="筆記", content_format="html",
           content=f'<img src="{picture_url}" alt="圖片"><img src="https://example.com/a.png">')
    markup = client.get(note).text
    assert f'src=&#34;{picture_url}' in markup
    assert "https://example.com/a.png" not in markup
    assert client.post(picture_url + "/delete", headers=headers).status_code == 409
    submit(client, "/workspace/note/1/save", name="筆記", content_format="html", content="<p>更新</p>")
    assert submit(client, picture_url + "/delete") == note
    assert client.get(picture_url).status_code == 404
    assert not (tmp_path / "uploads" / info["id"]).exists()
    submit(client, "/logout")
    assert client.get(pdf_url).status_code == 401
    assert client.post(upload, content=b"test", headers={**headers, "X-Filename": quote("file.txt")}).status_code == 401
