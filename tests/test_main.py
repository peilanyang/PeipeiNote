from collections.abc import Iterator
from contextlib import closing
from io import BytesIO
from pathlib import Path
import re
import secrets
import sqlite3
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.exports import WordWriter
from app.database import Note
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
    assert 'action="/logout"' not in blocked.text
    assert 'name="password"' in client.get("/login").text
    response = client.post("/login", data=credentials, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", f"max-age={auth.COOKIE_SECONDS}"))
    token = client.cookies[auth.COOKIE]
    assert 'action="/logout"' in client.get("/").text
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


def test_login_uses_only_trusted_proxy_client_address(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 驗證 README 指定的 Uvicorn 代理信任範圍會傳遞到登入計數。
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    proxy_app = ProxyHeadersMiddleware(client.app, trusted_hosts="127.0.0.1")
    headers = {"Origin": ORIGIN, "X-Forwarded-For": "198.51.100.10"}
    invalid = {**credentials, "password": secrets.token_urlsafe(24)}
    trusted = TestClient(proxy_app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    direct = TestClient(proxy_app, base_url=ORIGIN, client=("192.0.2.10", 50000))
    assert trusted.post("/login", data=invalid, headers=headers).status_code == 401
    assert direct.post("/login", data=invalid, headers=headers).status_code == 401
    with closing(sqlite3.connect(tmp_path / "test.db")) as database:
        assert database.execute("SELECT ip, failures FROM login_attempts ORDER BY ip").fetchall() == [
            ("192.0.2.10", 1), ("198.51.100.10", 1),
        ]


def test_csrf_and_invalid_session(client: TestClient, credentials: dict[str, str]) -> None:
    # 跨站或缺少來源時拒絕變更登入狀態，無效 Cookie 無法讀取首頁。
    assert client.post("/login", data=credentials, headers={"Origin": "https://elsewhere.test"}).status_code == 403
    assert client.post("/login", data=credentials).status_code == 403
    assert client.post("/logout", headers={"Origin": "null"}).status_code == 403
    client.cookies.set(auth.COOKIE, secrets.token_urlsafe(32))
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_expires_on_server(
    client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # 即使客戶端保留 Cookie，伺服器也會在到期時拒絕憑證。
    clock = SimpleNamespace(now=10_000)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock.now))
    client.post("/login", data=credentials, headers={"Origin": ORIGIN}).raise_for_status()
    assert 'action="/logout"' in client.get("/").text
    clock.now += auth.COOKIE_SECONDS - 1
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'action="/logout"' in response.text
    assert "set-cookie" not in response.headers
    clock.now += 1
    assert client.get("/", follow_redirects=False).status_code == 303

    # 現有資料庫中未設期限的舊工作階段在升級後也會失效。
    client.post("/login", data=credentials, headers={"Origin": ORIGIN}).raise_for_status()
    assert client.get("/", follow_redirects=False).status_code == 200
    with closing(sqlite3.connect(tmp_path / "test.db")) as database, database:
        database.execute("UPDATE login_sessions SET expires_at = 0")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_password_verification_does_not_hold_sqlite_write_lock(
    client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # 實際驗證密碼時，另一條 SQLite 連線仍可取得寫入鎖。
    original = auth.hasher

    def verify(password_hash: str, password: str) -> bool:
        with closing(sqlite3.connect(tmp_path / "test.db", timeout=0.2)) as database:
            database.execute("BEGIN IMMEDIATE")
            database.rollback()
        return original.verify(password_hash, password)

    monkeypatch.setattr(auth, "hasher", SimpleNamespace(verify=verify))
    submit(client, "/login", **credentials)


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


def note_version(html: str) -> str:
    # 像瀏覽器一樣使用頁面載入時拿到的記事版本。
    match = re.search(r'name="version" value="([^"]+)"', html)
    assert match is not None
    return match[1]


def open_test_note(client: TestClient, credentials: dict[str, str]) -> str:
    # 登入並透過正式工作區建立測試記事。
    submit(client, "/login", **credentials)
    return submit(client, "/workspace/note", name="原標題", use_inbox=1)


def submit(client: TestClient, path: str, **fields: str | int) -> str:
    # 送出同源表單並傳回成功後的工作區網址。
    if path.startswith("/workspace/note/") and path.endswith("/save") and "version" not in fields:
        note_id = path.split("/")[3]
        fields["version"] = note_version(client.get(f"/?kind=note&item_id={note_id}").text)
    response = client.post(path, data=fields, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_two_tabs_keep_the_later_draft_on_version_conflict(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 兩個分頁以同一版本開始編輯，舊版本不得覆蓋已成功儲存的內容。
    url = open_test_note(client, credentials)
    tab_a = note_version(client.get(url).text)
    tab_b = note_version(client.get(url).text)
    assert tab_a == tab_b
    path = "/workspace/note/1/save"
    headers = {"Origin": ORIGIN, "Accept": "application/json"}
    fields = {"name": "分頁甲", "content": "<p>甲先儲存</p>", "content_format": "html", "version": tab_a}
    first = client.post(path, files={key: (None, value) for key, value in fields.items()}, headers=headers)
    assert first.status_code == 200
    new_version = first.json()["version"]
    assert new_version != tab_a and note_version(client.get(url).text) == new_version

    draft = {"name": "分頁乙", "content": "<p>乙的草稿</p>", "content_format": "html", "version": tab_b}
    assert client.post(path, data=draft, headers={**headers, "Origin": "https://elsewhere.test"}).status_code == 403
    stale = client.post(path, data=draft, headers=headers)
    assert stale.status_code == 409 and "草稿" in stale.json()["detail"]
    fallback = client.post(path, data=draft, headers={"Origin": ORIGIN})
    assert fallback.status_code == 409 and "&lt;p&gt;乙的草稿&lt;/p&gt;" in fallback.text
    assert client.post(path, data={"name": "未提供版本"}, headers=headers).status_code == 422
    stored = client.get(url).text
    assert "甲先儲存" in stored and "乙的草稿" not in stored
    assert note_version(stored) == new_version

    second = client.post(path, data={**fields, "content": "甲繼續編輯", "version": new_version}, headers=headers)
    assert second.status_code == 200
    assert second.json()["version"] != new_version
    assert "甲繼續編輯" in client.get(url).text


def test_list_only_loads_a_100_character_summary_and_opens_full_note(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 中欄只顯示 100 個可見字元，選取記事後右欄仍可讀完整正文。
    url = open_test_note(client, credentials)
    with closing(sqlite3.connect(tmp_path / "test.db")) as database, database:
        database.execute("UPDATE notes SET updated_at = ?", ("2000-01-02 03:04:05",))
    before = client.get(url).text
    assert 'datetime="2000-01-02T03:04:05Z"' in before
    assert 'id="library-search"' in before and 'name="book"' in before
    assert 'href="/static/workspace.css"' in before
    css = client.get("/static/workspace.css")
    assert css.status_code == 200 and r".tw\:grid" in css.text
    visible = "字" * 99 + "&"
    tail = "這段只應出現在右欄"
    submit(client, "/workspace/note/1/save", name="原標題", content_format="html",
           content=f"<p><strong>{'字' * 99}&amp;</strong>{tail}</p>")
    topic = client.get("/?kind=topic&item_id=1")
    assert topic.status_code == 200
    assert f'<span class="note-preview">{visible[:-1]}&amp;</span>' in topic.text
    assert '<time class="note-updated"' in topic.text and "2000-01-02T03:04:05Z" not in topic.text
    assert tail not in topic.text
    assert tail in client.get(url).text


def test_backup_requires_login_and_same_origin(client: TestClient, credentials: dict[str, str]) -> None:
    # 備份 ZIP 不可透過未登入、跨站、缺來源或 GET 的請求下載。
    assert client.post("/backup", headers={"Origin": ORIGIN}).status_code == 401
    submit(client, "/login", **credentials)
    assert 'action="/backup"' in client.get("/").text
    assert client.get("/backup").status_code == 405
    assert client.post("/backup").status_code == 403
    assert client.post("/backup", headers={"Origin": "https://elsewhere.test"}).status_code == 403
    assert client.post("/backup", headers={"Origin": "null", "Referer": f"{ORIGIN}/"}).status_code == 403


def test_backup_contains_a_consistent_snapshot_without_secrets(
    client: TestClient, credentials: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 壓縮前讓另一請求移除附件，備份仍包含快照中對應的檔案。
    open_test_note(client, credentials)
    submit(client, "/workspace/note/1/save", name="有備份", content="重要記事")
    origin = {"Origin": ORIGIN}
    uploaded = client.post("/api/notes/1/files", content="附件本文".encode(),
                           headers={**origin, "X-Filename": "document.txt"})
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]
    (tmp_path / ".env").write_text("private marker", encoding="utf-8")
    uploads = tmp_path / "uploads"
    (uploads / ".env").write_text("private marker", encoding="utf-8")
    write = ZipFile.write

    def delete_before_archive_write(
        archive: ZipFile, filename: Path, arcname: str, *args: int, **kwargs: int,
    ) -> None:
        # 在寫入第一個 ZIP 項目時刪除來源附件，確認交易已釋放。
        if arcname == "peipeinote.db":
            deleted = client.post(f"/api/files/{file_id}/delete", headers=origin, follow_redirects=False)
            assert deleted.status_code == 303
            assert not (uploads / file_id).exists()
        write(archive, filename, arcname, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "write", delete_before_archive_write)
    response = client.post("/backup", headers=origin)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-type"] == "application/zip"
    assert ".zip" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"peipeinote.db", "uploads/", f"uploads/{file_id}"}
        assert archive.read(f"uploads/{file_id}").decode() == "附件本文"
        restored = tmp_path / "restored.db"
        restored.write_bytes(archive.read("peipeinote.db"))
    with closing(sqlite3.connect(restored)) as database:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert database.execute("SELECT title, content FROM notes").fetchone() == ("有備份", "重要記事")
        assert database.execute("SELECT storage_name FROM attachments").fetchone() == (file_id,)
        row = database.execute(
            "SELECT title FROM note_search WHERE note_search MATCH ?", ("重要記事",)
        ).fetchone()
        assert row == ("有備份",)
    assert not list(uploads.glob(".backup-*"))


def test_backup_reports_missing_files_and_cleans_up(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 缺少快照所需的檔案時回報失敗，避免下載看似完整的備份。
    open_test_note(client, credentials)
    origin = {"Origin": ORIGIN}
    uploaded = client.post("/api/notes/1/files", content=b"contents",
                           headers={**origin, "X-Filename": "file.txt"})
    assert uploaded.status_code == 201
    uploads = tmp_path / "uploads"
    (uploads / uploaded.json()["id"]).unlink()
    assert client.post("/backup", headers=origin).status_code == 503
    assert not list(uploads.glob(".backup-*"))


def test_notebook_export_releases_its_read_transaction_before_rendering(
    client: TestClient, credentials: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Word 產生時另一連線可以提交並完整回收 WAL，證實匯出快照已釋放。
    open_test_note(client, credentials)
    original = WordWriter.note

    def render(writer: WordWriter, note: Note, heading_offset: int = 0) -> None:
        # 匯出讀取交易若仍存在，含新提交資料的 WAL 將無法截斷。
        with closing(sqlite3.connect(tmp_path / "test.db", timeout=0.2)) as connection:
            connection.execute("UPDATE notebooks SET name = ?", ("已在匯出時改名",))
            connection.commit()
            assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        original(writer, note, heading_offset)

    monkeypatch.setattr(WordWriter, "note", render)
    response = client.get("/export/notebooks/1.docx")
    assert response.status_code == 200
    assert "已在匯出時改名" in client.get("/").text


def test_workspace_creates_edits_moves_and_deletes_all_levels(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 從登入後的三欄介面建立四層資料，逐層改名、移動和刪除。
    submit(client, "/login", **credentials)
    home = client.get("/").text
    assert 'class="workspace workspace-wide"' in home
    assert 'id="notes-heading"' not in home
    assert '<h2 class="heading"><a href="/library/tags">標籤管理</a></h2>' in home
    assert '<h2 class="heading"><a href="/library/templates">範本管理</a></h2>' in home
    header, _, workspace = home.partition("</header>")
    assert 'action="/workspace/notebook"' in header and 'action="/workspace/notebook"' not in workspace
    names = ("第一本", "第二本")
    first_book = submit(client, "/workspace/notebook", name=names[0])
    second_book = submit(client, "/workspace/notebook", name=names[1])
    assert 'id="notes-heading"' not in client.get(first_book).text
    submit(client, "/workspace/notebook/2/move", direction=-1)
    ordered = client.get("/").text
    assert ordered.index(names[1]) < ordered.index(names[0])
    submit(client, "/workspace/notebook/1/save", name="工作筆記本")
    assert "工作筆記本" in client.get(first_book).text

    section = submit(client, "/workspace/section", parent_id=1, name="工作清單")
    section_html = client.get(section).text
    assert 'id="notes-heading"' not in section_html
    assert 'action="/workspace/topic"' in section_html.partition('aria-labelledby="editor-heading"')[2]
    other_section = submit(client, "/workspace/section", parent_id=2, name="其他清單")
    submit(client, "/workspace/section/1/save", name="專案清單")
    submit(client, "/workspace/section/1/move", target_id=2)
    section_page = client.get(section).text
    assert 'id="editor-heading">第二本</h3>' in section_page
    assert "專案清單" not in section_page and 'item_id=2" aria-current="page"' in client.get(second_book).text
    assert '<details open>' in section_page

    topic = submit(client, "/workspace/topic", parent_id=1, name="工作主題")
    assert 'id="notes-heading"' in client.get(topic).text
    destination = submit(client, "/workspace/topic", parent_id=2, name="另一個主題")
    submit(client, "/workspace/topic/1/save", name="新主題")
    submit(client, "/workspace/topic/1/move", target_id=2)
    assert "新主題" in client.get(topic).text

    note = submit(client, "/workspace/note", parent_id=1, name="我的記事")
    title = "O'Reilly & 研究記事"
    body = "\n測試 <標記> 與引號\n第二行"
    submit(client, "/workspace/note/1/save", name=title, content=body)
    editor = client.get(note).text
    assert 'id="notes-heading"' in editor
    assert editor.index('id="rich-toolbar"') < editor.index('class="note-title"') < editor.index('id="rich-editor"')
    assert 'aria-label="記事標題"' in editor and 'aria-label="記事位置"' in editor
    assert "O&#39;Reilly &amp; 研究記事" in editor
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
    assert "從右上方新增第一本筆記本" in client.get("/").text


def test_workspace_guards_mutations_and_preserves_children(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 未登入、跨來源、未確認刪除與無效的父層都不能寫入資料。
    assert client.post("/workspace/notebook", data={"name": "測試筆記本"}, headers={"Origin": ORIGIN}).status_code == 401
    submit(client, "/login", **credentials)
    response = client.post(
        "/workspace/notebook",
        data={"name": "跨站"},
        headers={"Origin": "https://elsewhere.test"},
    )
    assert response.status_code == 403
    book = submit(client, "/workspace/notebook", name="原有筆記本")
    submit(client, "/workspace/section", name="要保留的清單", parent_id=1)
    submit(client, "/workspace/topic", name="要保留的主題", notebook_id=1)
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
    assert "原有筆記本" in result and "要保留的主題" in result
    assert "跨站" not in result


def test_topics_are_created_and_moved_directly_by_notebook(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 新介面以筆記本建立與移動主題，保留不同舊清單中的主題。
    submit(client, "/login", **credentials)
    first = submit(client, "/workspace/notebook", name="來源筆記本")
    second = submit(client, "/workspace/notebook", name="目的筆記本")
    submit(client, "/workspace/section", parent_id=1, name="舊版隱藏分類")
    submit(client, "/workspace/topic", parent_id=1, name="原有主題")
    submit(client, "/workspace/section", parent_id=1, name="第二個隱藏分類")
    submit(client, "/workspace/topic", parent_id=2, name="另一個原有主題")
    topic = submit(client, "/workspace/topic", notebook_id=1, name="新增的主題")
    first_html = client.get(first).text
    assert "原有主題" in first_html and "新增的主題" in first_html
    assert "另一個原有主題" in first_html and "第二個隱藏分類" not in first_html
    assert "舊版隱藏分類" not in first_html and "kind=section" not in first_html
    assert 'select name="notebook_id"' in client.get(topic).text
    assert submit(client, "/workspace/topic/3/move", notebook_id=2) == topic
    assert "新增的主題" in client.get(second).text
    assert 'id="editor-heading">新增的主題</h3>' in client.get(topic).text


def test_confirmed_notebook_removal_deletes_its_tree_and_files(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 一般確認保留原資料。第二個確認才會一併移除附件、標籤關聯及搜尋結果。
    open_test_note(client, credentials)
    submit(client, "/workspace/note/1/save", name="待刪記事", content="整本刪除關鍵詞")
    submit(client, "/library/tags", name="保留標籤")
    submit(client, "/library/notes/1/tags", tag_id=1, action="add")
    uploaded = client.post(
        "/api/notes/1/files", content=b"attachment", headers={"Origin": ORIGIN, "X-Filename": "sample.txt"},
    )
    assert uploaded.status_code == 201
    file_info = uploaded.json()
    submit(client, "/workspace/notebook", name="另一本")
    book = client.get("/?kind=notebook&item_id=1")
    assert 'name="delete_descendants"' in book.text
    path = "/workspace/notebook/1/delete"
    assert client.post(path, data={"confirmed": "true"}, headers={"Origin": ORIGIN}).status_code == 409
    assert client.get(file_info["url"]).status_code == 200
    assert submit(client, path, confirmed="true", delete_descendants="true") == "/"
    assert client.get("/?kind=note&item_id=1").status_code == 404
    assert client.get(file_info["url"]).status_code == 404
    assert not (tmp_path / "uploads" / file_info["id"]).exists()
    assert "待刪記事" not in client.get("/library/search", params={"q": "整本刪除關鍵詞"}).text
    assert "保留標籤" in client.get("/library/tags").text
    assert "另一本" in client.get("/").text
    with closing(sqlite3.connect(tmp_path / "test.db")) as database:
        assert database.execute("SELECT count(*) FROM note_tags").fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM topics").fetchone() == (0,)


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
    from docx import Document
    from PIL import Image
    from pypdf import PdfWriter
    from app import attachments

    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="附件")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    note = submit(client, "/workspace/note", parent_id=1, name="記事")
    upload = "/api/notes/1/files"
    headers = {"Origin": ORIGIN}
    fake = client.post(upload, content=b"plain text", headers={**headers, "X-Filename": quote("假.pdf")})
    assert fake.status_code == 415
    assert client.post(upload, headers={"X-Filename": "file.txt"}, content=b"hey").status_code == 403
    with monkeypatch.context() as limits:
        limits.setattr(attachments, "MAX_FILE_BYTES", 3)
        response = client.post(
            upload, content=b"four", headers={**headers, "X-Filename": quote("file.txt")}
        )
        assert response.status_code == 413
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
    empty_pdf = BytesIO()
    with PdfWriter() as writer:
        writer.write(empty_pdf)
    assert client.post(upload, content=empty_pdf.getvalue(), headers={
        **headers, "X-Filename": "no-pages.pdf",
    }).status_code == 415

    # 僅有幾個預期的部件名稱，無法替代完整的 Word 結構。
    invalid_docx = BytesIO()
    with ZipFile(invalid_docx, "w") as package:
        for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            package.writestr(name, "<empty/>")
    docx_headers = {**headers, "X-Filename": "document.docx"}
    assert client.post(upload, content=invalid_docx.getvalue(), headers=docx_headers).status_code == 415
    valid_docx = BytesIO()
    docx = Document()
    docx.add_paragraph("測試文件")
    docx.save(valid_docx)
    attached_docx = client.post(upload, content=valid_docx.getvalue(), headers=docx_headers)
    assert attached_docx.status_code == 201
    assert client.get(attached_docx.json()["url"]).content == valid_docx.getvalue()

    document = BytesIO()
    Image.new("RGB", (2, 2)).save(document, format="PDF")
    pdf = client.post(upload, content=document.getvalue(), headers={**headers, "X-Filename": quote("手冊.pdf")})
    assert pdf.status_code == 201
    pdf_url = pdf.json()["url"]
    assert client.get(pdf_url).headers["content-disposition"].startswith("attachment")
    assert "手冊.pdf" in client.get(note).text

    submit(client, "/workspace/note/1/save", name="記事", content_format="html",
           content=f'<img src="{picture_url}" alt="圖片"><img src="https://example.com/a.png">')
    markup = client.get(note).text
    assert f'src=&#34;{picture_url}' in markup
    assert "https://example.com/a.png" not in markup
    assert client.post(picture_url + "/delete", headers=headers).status_code == 409
    submit(client, "/workspace/note/1/save", name="記事", content_format="html", content="<p>更新</p>")
    assert submit(client, picture_url + "/delete") == note
    assert client.get(picture_url).status_code == 404
    assert not (tmp_path / "uploads" / info["id"]).exists()
    submit(client, "/logout")
    assert client.get(pdf_url).status_code == 401
    assert client.post(upload, content=b"test", headers={**headers, "X-Filename": quote("file.txt")}).status_code == 401


def test_search_tags_and_template_workflow(client: TestClient, credentials: dict[str, str]) -> None:
    # 跨筆記本搜尋和標籤篩選，並驗證範本會複製淨化後的格式。
    headers = {"Origin": ORIGIN}
    assert client.get("/library/search?q=旅遊").status_code == 401
    assert client.post("/library/tags", data={"name": "未登入"}, headers=headers).status_code == 401
    submit(client, "/login", **credentials)
    for book in ("工作", "生活"):
        index = 1 if book == "工作" else 2
        submit(client, "/workspace/notebook", name=book)
        submit(client, "/workspace/section", parent_id=index, name="清單")
        submit(client, "/workspace/topic", parent_id=index, name="主題")
    submit(client, "/library/tags", name="旅遊")
    submit(client, "/library/tags", name="備忘")
    assert client.post("/library/tags", data={"name": "旅遊"}, headers=headers).status_code == 409
    assert client.post("/library/templates", data={"name": "來源錯誤"}).status_code == 403
    saved = submit(
        client, "/library/templates", name="行程表", content_format="html",
        content='<h2>行程規劃</h2><p>明年到北海道旅行</p><script type="text/plain">應被移除</script>',
    )
    assert "&lt;h2&gt;行程規劃&lt;/h2&gt;" in client.get(saved).text
    submit(client, "/library/templates", record_id=1, name="出差範本", content_format="html",
           content="<h2>行程規劃</h2><p>明年到北海道旅行</p>")
    topic = client.get("/?kind=topic&item_id=1").text
    assert "出差範本" in topic
    note = submit(client, "/workspace/note", parent_id=1, name="北方之旅", template_id=1)
    assert "&lt;h2&gt;行程規劃&lt;/h2&gt;" in client.get(note).text
    assert "內嵌圖片和附件不會複製" in client.get(note).text
    response = client.post(
        "/workspace/note",
        data={"parent_id": 1, "name": "錯誤", "template_id": 99},
        headers=headers,
    )
    assert response.status_code == 404
    submit(client, "/workspace/note", parent_id=2, name="假期準備", template_id="")
    submit(client, "/workspace/note/2/save", name="假期準備", content='行前記事：搭飛機到北海道。查找 "登機門"。')
    for _ in range(2):
        submit(client, "/library/notes/1/tags", action="add", tag_id=1)
    submit(client, "/library/notes/1/tags", action="add", tag_id=2)
    submit(client, "/library/notes/2/tags", action="add", tag_id=1)
    submit(client, "/library/tags", record_id=1, name="旅行")
    tagged = client.get("/library/search", params={"tag": 1}).text
    assert "旅行" in tagged and "北方之旅" in tagged and "假期準備" in tagged

    across = client.get("/library/search", params={"q": "北海道", "partial": "true"}).text
    assert "北方之旅" in across and "假期準備" in across and "明年到北海道旅行" in across
    assert '<time class="note-updated"' in across
    scoped = client.get("/library/search", params={"q": "北海道", "book": 2}).text
    assert "假期準備" in scoped and "北方之旅" not in scoped
    title_only = client.get("/library/search", params={"q": "北方"}).text
    assert "北方之旅" in title_only and "明年到北海道旅行" not in title_only
    assert "假期準備" in client.get("/library/search", params={"q": '"登機門"'}).text

    submit(client, "/library/notes/1/tags", action="remove", tag_id=1)
    assert "北方之旅" not in client.get("/library/search", params={"tag": 1}).text
    submit(client, "/library/tags", record_id=2, action="delete")
    assert client.get("/library/search", params={"tag": 2}).status_code == 404
    submit(client, "/library/templates", record_id=1, action="delete")
    assert "行程規劃" in client.get(note).text
    submit(client, "/workspace/note/2/save", name="假期準備", content="東京")
    assert "假期準備" not in client.get("/library/search", params={"q": "北海道"}).text
    submit(client, "/workspace/note/1/delete", confirmed="true")
    assert "北方之旅" not in client.get("/library/search", params={"q": "北海道"}).text


def test_library_tags_manage_tag_notes_page_links(
    client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 標籤文章列表分頁連結：頁數不足時不顯示，跨頁時產生上一頁/下一頁
    from app import library
    monkeypatch.setattr(library, "PAGE_SIZE", 2)
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    submit(client, "/library/tags", name="測試標籤")
    for i in range(1, 6):
        submit(client, "/workspace/note", parent_id=1, name=f"文章{i}")
        submit(client, f"/library/notes/{i}/tags", action="add", tag_id=1)

    # 第 1 頁：2 筆，有下一頁、無上一頁
    first = client.get("/library/tags?selected=1&tag_page=1")
    assert first.status_code == 200
    assert "tag_page=2" in first.text
    assert "tag_page=0" not in first.text
    assert "上一頁" not in first.text

    # 第 2 頁：2 筆，兩頁都有
    second = client.get("/library/tags?selected=1&tag_page=2")
    assert second.status_code == 200
    assert "tag_page=1" in second.text
    assert "tag_page=3" in second.text
    assert "上一頁" in second.text and "下一頁" in second.text

    # 最後一頁：1 筆，有上一頁、無下一頁
    last = client.get("/library/tags?selected=1&tag_page=3")
    assert last.status_code == 200
    assert "tag_page=2" in last.text
    assert "tag_page=4" not in last.text
    assert "上一頁" in last.text and "下一頁" not in last.text


def test_library_tags_manage_with_selected_tag(client: TestClient, credentials: dict[str, str]) -> None:
    # 標籤管理頁：選取標籤時右側顯示該標籤下的記事列表
    submit(client, "/login", **credentials)
    # 建立筆記本結構
    submit(client, "/workspace/notebook", name="工作")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    # 建立標籤
    submit(client, "/library/tags", name="旅遊")
    # 建立記事並加上標籤
    note1 = submit(client, "/workspace/note", parent_id=1, name="北方之旅")
    submit(client, "/workspace/note/1/save", name="北方之旅", content="北海道旅行規劃")
    submit(client, "/library/notes/1/tags", action="add", tag_id=1)
    note2 = submit(client, "/workspace/note", parent_id=1, name="南方之旅")
    submit(client, "/workspace/note/2/save", name="南方之旅", content="沖繩旅行規劃")
    submit(client, "/library/notes/2/tags", action="add", tag_id=1)

    # GET /library/tags?selected=1 應回傳該標籤的文章
    response = client.get("/library/tags?selected=1")
    assert response.status_code == 200
    html = response.text
    assert "標籤「旅遊」的文章" in html
    assert "北方之旅" in html
    assert "南方之旅" in html
    # 文章標題應可點擊跳轉編輯頁（新分頁）
    assert 'href="/?kind=note&item_id=1"' in html
    assert 'href="/?kind=note&item_id=2"' in html


def test_library_tags_manage_tag_notes_pagination(client: TestClient, credentials: dict[str, str]) -> None:
    # 標籤文章列表分頁：測試 tag_page 參數傳遞與連結生成
    # 先用少量資料驗證基本功能，再用較多資料驗證不分頁
    from app.search import PAGE_SIZE
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    submit(client, "/library/tags", name="測試標籤")

    # 建立 3 篇文章
    for i in range(1, 4):
        submit(client, "/workspace/note", parent_id=1, name=f"文章{i}")
        submit(client, f"/workspace/note/{i}/save", name=f"文章{i}", content=f"內容{i}")
        submit(client, f"/library/notes/{i}/tags", action="add", tag_id=1)

    # 第 1 頁 - 3 筆 < PAGE_SIZE(50) 不會分頁
    response = client.get("/library/tags?selected=1&tag_page=1")
    assert response.status_code == 200
    assert "tag_page=2" not in response.text

    # 驗證所有 3 篇文章都在第一頁顯示
    for note_id in range(1, 4):
        assert f'item_id={note_id}' in response.text

    # 測試傳入不存在的頁碼不會報錯
    response = client.get("/library/tags?selected=1&tag_page=999")
    assert response.status_code == 200


def test_library_tags_manage_nonexistent_tag_404(client: TestClient, credentials: dict[str, str]) -> None:
    # 選取不存在的標籤回傳 404
    submit(client, "/login", **credentials)
    response = client.get("/library/tags?selected=999")
    assert response.status_code == 404


def test_library_tags_manage_tag_notes_empty_state(client: TestClient, credentials: dict[str, str]) -> None:
    # 標籤無文章時顯示空狀態與 CTA
    submit(client, "/login", **credentials)
    submit(client, "/library/tags", name="空標籤")
    response = client.get("/library/tags?selected=1")
    assert response.status_code == 200
    html = response.text
    assert "此標籤下尚無記事" in html
    assert 'href="/?new_note=true"' in html


def test_library_tags_manage_without_selected_tag_has_no_dead_panel(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 未選取標籤時不再渲染舊 AJAX 遺留的空面板；選取後頁面只有一個文章面板。
    submit(client, "/login", **credentials)
    submit(client, "/library/tags", name="空標籤")
    unselected = client.get("/library/tags").text
    assert 'id="tag-notes-content"' not in unselected
    assert 'id="tag-notes-panel"' not in unselected
    selected = client.get("/library/tags?selected=1").text
    assert selected.count('id="tag-notes-panel"') == 1
    assert 'id="tag-notes-content"' not in selected


def test_empty_topic_offers_create_first_note_call_to_action(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 主題沒有記事時，空狀態提供「新增第一則記事」按鈕並指向同頁的新增表單。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作")
    submit(client, "/workspace/section", parent_id=1, name="清單")
    submit(client, "/workspace/topic", parent_id=1, name="主題")
    response = client.get("/?kind=topic&item_id=1")
    assert response.status_code == 200
    html = response.text
    assert "這個主題還沒有記事。" in html
    # CTA 本身必須指向 #new-note，且同頁存在該錨點才能跳到新增記事表單。
    assert re.search(r'<a[^>]+href="#new-note"[^>]*>\s*新增第一則記事</a>', html)
    assert 'id="new-note"' in html
    # 已有記事時不再顯示空狀態引導，避免重複提示。
    submit(client, "/workspace/note", parent_id=1, name="第一則")
    html = client.get("/?kind=topic&item_id=1").text
    assert "這個主題還沒有記事。" not in html
    assert "新增第一則記事" not in html


def test_empty_search_results_offer_search_and_create_actions(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 搜尋沒有結果時，提供「調整關鍵字」與「新增記事」兩個明確動作。
    submit(client, "/login", **credentials)
    html = client.get("/library/search", params={"q": "不存在的關鍵字"}).text
    assert "找不到符合條件的記事。" in html
    assert re.search(r'<a[^>]+href="/library/search"[^>]*>\s*調整關鍵字</a>', html)
    assert re.search(r'<a[^>]+href="/\?new_note=true"[^>]*>\s*新增記事</a>', html)
    # 搜尋頁沒有 #new-note 錨點，不可使用會失效的片段連結。
    assert 'href="#new-note"' not in html
    # 點擊新增記事應導向首頁的快速新增表單。
    assert 'id="quick-note"' in client.get("/?new_note=true").text
    # 依標籤篩選但沒有結果時，同樣提供新增記事的動作。
    submit(client, "/library/tags", name="旅遊")
    tagged = client.get("/library/search", params={"tag": 1, "q": "查不到"}).text
    assert "找不到符合條件的記事。" in tagged
    assert re.search(r'<a[^>]+href="/\?new_note=true"[^>]*>\s*新增記事</a>', tagged)
    assert re.search(r'<a[^>]+href="/library/search\?tag=1"[^>]*>\s*調整關鍵字</a>', tagged)


def test_partial_search_results_keep_create_action_target(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 工作區的局部搜尋同樣提供可用的新增記事動作，不使用失效的片段連結。
    submit(client, "/login", **credentials)
    html = client.get("/library/search", params={"q": "查不到", "partial": "true"}).text
    assert "找不到符合條件的記事。" in html
    assert re.search(r'<a[^>]+href="/\?new_note=true"[^>]*>\s*新增記事</a>', html)
    assert 'href="#new-note"' not in html


def test_empty_search_results_keep_malicious_keyword_escaped(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 惡意關鍵字不會被寫入空狀態的行動連結，空狀態區塊也不產生可執行標籤。
    submit(client, "/login", **credentials)
    payload = '<script>alert(1)</script>'
    response = client.get("/library/search", params={"q": payload})
    assert response.status_code == 200
    html = response.text
    assert "找不到符合條件的記事。" in html
    assert payload not in html
    assert "<script>alert(1)</script>" not in html
    assert re.search(r'<a[^>]+href="/\?new_note=true"[^>]*>\s*新增記事</a>', html)


def test_empty_template_library_cta_links_to_working_form(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 還沒有範本時，空狀態 CTA 必須指向會顯示新增表單的有效網址，而不是 422 的 selected=0。
    submit(client, "/login", **credentials)
    html = client.get("/library/templates").text
    assert "目前沒有範本。" in html
    assert re.search(r'<a[^>]+href="/library/templates"[^>]*>\s*新增第一個範本</a>', html)
    # selected=0 會被端點的 gt=0 驗證拒絕，頁面上不可出現這類失效連結。
    assert "selected=0" not in html
    # 沿 CTA 網址重新開啟頁面，應看到可直接送出的新增範本表單。
    target = client.get("/library/templates")
    assert target.status_code == 200
    assert 'action="/library/templates"' in target.text
    assert 'name="name"' in target.text and "新增範本" in target.text
    # 實際送出表單能建立第一份範本，確認 CTA 之後的操作流暢。
    saved = submit(client, "/library/templates", name="第一份範本")
    assert "第一份範本" in client.get(saved).text


def test_export_word_and_print(client: TestClient, credentials: dict[str, str]) -> None:
    # 以實際上傳圖片與含表格的富文字驗證 Word 和列印輸出。
    from io import BytesIO
    from zipfile import ZipFile

    from docx import Document
    from PIL import Image

    assert client.get("/export/notes/1.docx").status_code == 401
    assert client.get("/export/notebooks/1.docx").status_code == 401
    assert client.get("/print/notes/1").status_code == 401
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作記事")
    submit(client, "/workspace/section", parent_id=1, name="專案清單")
    submit(client, "/workspace/topic", parent_id=1, name="例會主題")
    note_url = submit(client, "/workspace/note", parent_id=1, name="週五會議")
    image = BytesIO()
    Image.new("RGB", (80, 40), "blue").save(image, format="WEBP")
    upload = client.post(
        "/api/notes/1/files", content=image.getvalue(),
        headers={"Origin": ORIGIN, "X-Filename": "test.webp"},
    )
    assert upload.status_code == 201
    image_url = upload.json()["url"]
    submit(
        client, "/workspace/note/1/save", name="週五會議", content_format="html",
        content=(
            '<h2>會議摘要</h2><p><strong>重點</strong> <a href="https://example.com">參考</a></p>'
            '<table><tr><th colspan="2">議程</th></tr><tr><td>時間</td><td>週五</td></tr></table>'
            '<ul data-type="taskList"><li data-type="taskItem" data-checked="true"><p>完成事項</p></li></ul>'
            f'<img src="{image_url}" alt="圖示" width="64"><script type="text/plain">不輸出</script>'
        ),
    )
    assert "匯出 Word" in client.get(note_url).text
    file = client.get("/export/notes/1.docx")
    assert file.status_code == 200
    assert file.headers["content-type"].endswith("wordprocessingml.document")
    assert "filename*=UTF-8" in file.headers["content-disposition"]
    document = Document(BytesIO(file.content))
    assert document.paragraphs[0].text == "週五會議"
    assert document.paragraphs[1].style.name == "Heading 2"
    assert document.paragraphs[2].runs[0].bold
    assert document.paragraphs[2].hyperlinks[0].url == "https://example.com"
    assert document.tables[0].cell(0, 0).grid_span == 2
    assert document.tables[0].cell(1, 1).text == "週五"
    assert len(document.inline_shapes) == 1
    assert any("☑ 完成事項" in p.text for p in document.paragraphs)
    assert all("不輸出" not in p.text for p in document.paragraphs)

    submit(client, "/workspace/note", parent_id=1, name="第二篇")
    submit(client, "/workspace/note/2/save", name="第二篇", content="舊純文字 <保留>\n下一行")
    book = client.get("/export/notebooks/1.docx")
    assert book.status_code == 200
    combined = Document(BytesIO(book.content))
    for level, title in [(1, "例會主題"), (2, "週五會議")]:
        assert any(p.text == title and p.style.name == f"Heading {level}" for p in combined.paragraphs)
    assert any("舊純文字 <保留>\n下一行" == p.text for p in combined.paragraphs)
    with ZipFile(BytesIO(book.content)) as package:
        xml = package.read("word/document.xml").decode()
        assert 'w:anchor="outline_2"' in xml and 'w:name="outline_2"' in xml
        assert "專案清單" not in xml
    page = client.get("/print/notes/1")
    assert page.status_code == 200 and page.headers["cache-control"] == "private, no-store"
    assert "<table>" in page.text and f'src="{image_url}"' in page.text
    assert 'class="workspace"' not in page.text and "不輸出" not in page.text
    assert "舊純文字 &lt;保留&gt;" in client.get("/print/notes/2").text
    assert client.get("/export/notebooks/99.docx").status_code == 404


def test_home_quick_create_supports_first_note_and_existing_topics(
    client: TestClient, credentials: dict[str, str],
) -> None:
    # 從首頁開啟新增表單，並確認收件匣及選擇主題兩條流程。
    submit(client, "/login", **credentials)
    home = client.get("/").text
    assert 'href="/?new_note=true#quick-note">新增記事</a>' in home
    assert 'id="quick-note"' not in home
    quick = client.get("/?new_note=true").text
    assert 'id="quick-note"' in quick and 'name="use_inbox"' in quick
    assert client.post("/workspace/note", data={"name": "未授權", "use_inbox": "true"}).status_code == 403
    first = submit(client, "/workspace/note", name="第一則速記", use_inbox="true")
    assert "隨手記" in client.get(first).text
    submit(client, "/workspace/notebook", name="工作")
    submit(client, "/workspace/section", parent_id=2, name="任務")
    submit(client, "/workspace/topic", parent_id=2, name="例會")
    quick = client.get("/?new_note=true").text
    assert "收件匣 / 隨手記" in quick and "工作 / 例會" in quick
    assert 'name="use_inbox"' not in quick
    second = submit(client, "/workspace/note", name="會議記事", parent_id=2)
    assert "會議記事" in client.get(second).text
    assert "會議記事" in client.get("/?kind=topic&item_id=2").text
    assert "會議記事" not in client.get("/?kind=topic&item_id=1").text


def test_duplicate_notebook_name_returns_conflict(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 同帳號建立或改名到既有筆記本名稱時回 409，不回 500，也不留半筆資料。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/notebook", name="工作")
    duplicate = client.post(
        "/workspace/notebook", data={"name": "工作"},
        headers={"Origin": ORIGIN}, follow_redirects=False,
    )
    assert duplicate.status_code == 409
    assert "名稱已存在" in duplicate.json()["detail"]
    second = int(submit(client, "/workspace/notebook", name="備份").split("=")[-1])
    renamed = client.post(
        f"/workspace/notebook/{second}/save", data={"name": "工作"},
        headers={"Origin": ORIGIN}, follow_redirects=False,
    )
    assert renamed.status_code == 409
    with closing(sqlite3.connect(tmp_path / "test.db")) as database:
        names = [row[0] for row in database.execute("SELECT name FROM notebooks ORDER BY id")]
    assert names == ["工作", "備份"]
    assert "Internal Server Error" not in client.get("/").text


def test_shared_inbox_still_receives_quick_notes(
    client: TestClient, credentials: dict[str, str], tmp_path: Path,
) -> None:
    # 自己的收件匣設為家庭共用後，快速建立仍重用同一本，不會再建第二本或報 500。
    submit(client, "/login", **credentials)
    submit(client, "/workspace/note", name="第一篇速記", use_inbox="true")
    with closing(sqlite3.connect(tmp_path / "test.db")) as database:
        inbox_id = database.execute("SELECT id FROM notebooks WHERE name = '收件匣'").fetchone()[0]
        topic_id = database.execute(
            "SELECT t.id FROM topics AS t JOIN topic_lists AS l ON t.topic_list_id = l.id "
            "WHERE l.notebook_id = ? AND t.name = '隨手記'", (inbox_id,),
        ).fetchone()[0]
    submit(client, f"/workspace/notebook/{inbox_id}/share", is_shared="true")
    # 刪掉匣內主題後，首頁快速建立表單會改用 use_inbox 送出。
    submit(client, f"/workspace/topic/{topic_id}/delete", confirmed="true", delete_descendants="true")
    assert "收件匣" in client.get("/?new_note=true").text
    second = submit(client, "/workspace/note", name="第二篇速記", use_inbox="true")
    assert "第二篇速記" in client.get(second).text
    with closing(sqlite3.connect(tmp_path / "test.db")) as database:
        assert database.execute("SELECT COUNT(*) FROM notebooks WHERE name = '收件匣'").fetchone()[0] == 1
        assert database.execute("SELECT is_shared FROM notebooks WHERE id = ?", (inbox_id,)).fetchone()[0] == 1
