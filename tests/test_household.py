from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import secrets
import sqlite3

from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import auth
from app.main import create_app
from test_main import ORIGIN, note_version, submit

HEADERS = {"Origin": ORIGIN}


@pytest.fixture
def accounts() -> tuple[auth.Settings, dict[str, str], dict[str, str]]:
    # 每次產生彼此獨立的測試憑證。
    a = {"username": "account-a", "password": secrets.token_urlsafe(24)}
    b = {"username": "account-b", "password": secrets.token_urlsafe(24)}
    settings = auth.Settings(
        username=a["username"], password_hash=auth.hasher.hash(a["password"]),
        partner_username=b["username"], partner_password_hash=auth.hasher.hash(b["password"]),
        secret=secrets.token_urlsafe(48), origin=ORIGIN,
    )
    return settings, a, b


@pytest.fixture
def family(tmp_path: Path, accounts: tuple[auth.Settings, dict[str, str], dict[str, str]]) -> Iterator[tuple[TestClient, TestClient]]:
    # 兩個瀏覽器各自登入，使用同一個實際應用程式與測試資料庫。
    settings, a_credentials, b_credentials = accounts
    app = create_app(settings, tmp_path / "family.db")
    with TestClient(app, base_url=ORIGIN) as a, TestClient(app, base_url=ORIGIN) as b:
        submit(a, "/login", **a_credentials)
        submit(b, "/login", **b_credentials)
        yield a, b


def create_hierarchy(client: TestClient, prefix: str) -> tuple[int, int, int, int]:
    # 透過正式工作區建立完整四層記事。
    book = int(submit(client, "/workspace/notebook", name=f"{prefix}筆記本").split("=")[-1])
    section = int(submit(client, "/workspace/section", name=f"{prefix}清單", parent_id=book).split("=")[-1])
    topic = int(submit(client, "/workspace/topic", name=f"{prefix}主題", parent_id=section).split("=")[-1])
    note = int(submit(client, "/workspace/note", name=f"{prefix}記事", parent_id=topic).split("=")[-1])
    return book, section, topic, note


def test_private_notebook_and_library_are_isolated(family: tuple[TestClient, TestClient]) -> None:
    # A 能正常使用自己的資料，B 的私人資料即使知道 ID 也回傳 404。
    a, b = family
    a_book, a_section, a_topic, a_note = create_hierarchy(a, "甲的記錄")
    book, section, topic, note = create_hierarchy(b, "乙的機密")
    submit(b, f"/workspace/note/{note}/save", name="乙的機密記事", content="私密關鍵詞")
    tag = int(submit(b, "/library/tags", name="乙的機密標籤").split("=")[-1])
    template = int(submit(b, "/library/templates", name="乙的機密範本", content="保密文字").split("=")[-1])
    submit(b, f"/library/notes/{note}/tags", tag_id=tag, action="add")
    assert "甲的記錄" in a.get("/").text and "乙的機密" not in a.get("/").text
    assert "甲的記錄" not in b.get("/").text
    for kind, item, destination in (("notebook", book, None), ("section", section, a_book),
                                    ("topic", topic, a_section), ("note", note, a_topic)):
        assert a.get(f"/?kind={kind}&item_id={item}").status_code == 404
        for action, data in (("save", {"name": "越權修改", "version": "stale"}),
                             ("delete", {"confirmed": "true"}),
                             ("move", {"target_id": destination} if destination else {"direction": 1})):
            result = a.post(f"/workspace/{kind}/{item}/{action}", data=data, headers=HEADERS)
            assert result.status_code == 404, (kind, action, result.text)
    for kind, private_parent in (("section", book), ("topic", section), ("note", topic)):
        assert a.post(f"/workspace/{kind}", headers=HEADERS, data={
            "name": "不可新增", "parent_id": private_parent,
        }).status_code == 404
    assert a.post("/workspace/topic", headers=HEADERS, data={
        "name": "不可新增", "notebook_id": book,
    }).status_code == 404
    assert a.post(f"/workspace/topic/{a_topic}/move", headers=HEADERS, data={
        "notebook_id": book,
    }).status_code == 404
    for path in (
        f"/export/notebooks/{book}.docx", f"/export/notes/{note}.docx", f"/print/notes/{note}",
        f"/library/search?q=機密&book={book}", f"/library/search?tag={tag}",
        f"/library/tags?selected={tag}", f"/library/templates?selected={template}",
    ):
        assert a.get(path).status_code == 404, path
    for path in ("/library/search?q=私密關鍵詞", "/library/search?q=私密&partial=true",
                 "/library/tags", "/library/templates"):
        response = a.get(path)
        assert response.status_code == 200 and "乙的機密" not in response.text
    assert "乙的機密記事" in b.get("/library/search?q=私密關鍵詞").text
    for kind, record in (("tags", tag), ("templates", template)):
        for action in ("save", "delete"):
            assert a.post(f"/library/{kind}", headers=HEADERS, data={
                "record_id": record, "action": action, "name": "越權修改",
            }).status_code == 404
    assert a.post("/workspace/note", headers=HEADERS, data={
        "name": "不可套用", "parent_id": a_topic, "template_id": template,
    }).status_code == 404
    assert a.post("/library/templates", headers=HEADERS, data={
        "name": "不可複製", "note_id": note,
    }).status_code == 404
    for action in ("add", "remove"):
        for target_note in (a_note, note):
            assert a.post(f"/library/notes/{target_note}/tags", headers=HEADERS, data={
                "action": action, "tag_id": tag,
            }).status_code == 404
    assert a.post("/backup", headers=HEADERS).status_code == 403
    assert b.post("/backup", headers=HEADERS).status_code == 403
    submit(a, "/library/tags", name="乙的機密標籤")


def test_sharing_grants_and_revokes_content_and_attachment_access(family: tuple[TestClient, TestClient]) -> None:
    # 用同一組 ID 驗證私人、共用和收回權限三個狀態。
    a, b = family
    _, _, a_topic, _ = create_hierarchy(a, "甲的記錄")
    book, _, topic, note = create_hierarchy(b, "乙的待共享")
    _, _, hidden_topic, hidden_note = create_hierarchy(b, "乙的另一個私人")
    tag = int(submit(b, "/library/tags", name="家庭行程").split("=")[-1])
    submit(b, f"/library/notes/{note}/tags", tag_id=tag, action="add")
    submit(b, f"/library/notes/{hidden_note}/tags", tag_id=tag, action="add")
    note_url = f"/?kind=note&item_id={note}"
    book_url = f"/?kind=notebook&item_id={book}"
    share_url = f"/workspace/notebook/{book}/share"
    file_route = f"/api/notes/{note}/files"
    png = BytesIO()
    Image.new("RGB", (2, 2)).save(png, format="PNG")
    pdf = BytesIO()
    Image.new("RGB", (2, 2)).save(pdf, format="PDF")
    files = []
    for filename, data in (("family.png", png.getvalue()), ("family.pdf", pdf.getvalue())):
        headers = {**HEADERS, "X-Filename": filename}
        created = b.post(file_route, content=data, headers=headers)
        assert created.status_code == 201, created.text
        file_url = created.json()["url"]
        files.append((file_url, data))
        assert b.get(file_url).content == data
        assert a.get(file_url).status_code == 404
        assert a.get(f"{file_url}?download=true").status_code == 404
        assert a.post(f"{file_url}/delete", headers=HEADERS).status_code == 404
        assert a.post(file_route, content=data, headers=headers).status_code == 404

    assert "設為共用" in b.get(book_url).text
    assert a.post(share_url, data={"is_shared": "true"}, headers=HEADERS).status_code == 404
    assert b.post(share_url, data={"is_shared": "true"}).status_code == 403
    submit(b, share_url, is_shared="true")
    assert 'aria-label="家庭共用"' in a.get("/").text
    assert "設為私人" in b.get(book_url).text and share_url not in a.get(book_url).text
    assert a.post(share_url, data={"is_shared": "false"}, headers=HEADERS).status_code == 404
    assert a.post(f"/workspace/notebook/{book}/delete", data={"confirmed": "true"}, headers=HEADERS).status_code == 404
    assert a.post(f"/workspace/notebook/{book}/delete", data={
        "confirmed": "true", "delete_descendants": "true",
    }, headers=HEADERS).status_code == 404
    for target in (a_topic, hidden_topic):
        assert a.post(f"/workspace/note/{note}/move", data={"target_id": target}, headers=HEADERS).status_code == 404
    for file_url, data in files:
        assert a.get(file_url).content == data
        assert a.get(f"{file_url}?download=true").content == data
    added = submit(a, "/workspace/note", name="甲新增給家人", parent_id=topic)
    assert b.get(added).status_code == 200
    own_tag = int(submit(a, "/library/tags", name="甲的標籤").split("=")[-1])
    shared_note = a.get(note_url).text
    assert "家庭行程" in shared_note
    assert f'<option value="{own_tag}">甲的標籤</option>' in shared_note
    submit(a, f"/library/notes/{note}/tags", tag_id=own_tag, action="add")
    tagged = a.get(f"/library/search?tag={tag}")
    assert tagged.status_code == 200 and "乙的待共享記事" in tagged.text
    assert "乙的另一個私人" not in tagged.text
    assert a.get(f"/library/tags?selected={tag}").status_code == 404

    first = note_version(a.get(note_url).text)
    assert note_version(b.get(note_url).text) == first
    def save(client: TestClient, name: str) -> tuple[str, int]:
        # 同時從兩個帳號提交同一個版本。
        result = client.post(f"/workspace/note/{note}/save", headers={**HEADERS, "Accept": "application/json"},
                             data={"name": name, "content": name, "version": first})
        return name, result.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        saves = [pool.submit(save, a, "甲的編輯"), pool.submit(save, b, "乙的編輯")]
        results = {status: title for title, status in (attempt.result() for attempt in saves)}
    assert results.keys() == {200, 409}
    assert results[200] in a.get(note_url).text and results[409] not in b.get(note_url).text
    assert results[200] in a.get("/library/search", params={"q": results[200]}).text
    for path in (f"/export/notes/{note}.docx", f"/export/notebooks/{book}.docx"):
        exported = a.get(path)
        assert exported.status_code == 200
        assert results[200] in [p.text for p in Document(BytesIO(exported.content)).paragraphs]

    submit(b, share_url, is_shared="false")
    for path in (book_url, added, note_url, f"/library/search?tag={tag}",
                 f"/export/notebooks/{book}.docx", *(url for url, _ in files)):
        assert a.get(path).status_code == 404, path
    assert b.get(added).status_code == 200
    assert "乙的待共享" not in a.get("/").text


def test_word_exports_respect_attachment_access_after_unsharing(family: tuple[TestClient, TestClient]) -> None:
    # 共用期間可匯出圖片，收回共用後已有的圖片參照也必須重新驗權。
    owner, partner = family
    source_book, _, _, source_note = create_hierarchy(owner, "圖片來源")
    book, _, _, note = create_hierarchy(partner, "引用圖片")
    picture = BytesIO()
    Image.new("RGB", (2, 2)).save(picture, format="PNG")
    uploaded = owner.post(
        f"/api/notes/{source_note}/files", content=picture.getvalue(),
        headers={**HEADERS, "X-Filename": "photo.png"},
    )
    assert uploaded.status_code == 201
    image_url = uploaded.json()["url"]
    share = f"/workspace/notebook/{source_book}/share"
    submit(owner, share, is_shared="true")
    assert partner.get(image_url).status_code == 200
    submit(partner, f"/workspace/note/{note}/save", name="引用圖片", content_format="html",
           content=f'<img src="{image_url}" alt="共用圖片">')
    for path in (f"/export/notes/{note}.docx", f"/export/notebooks/{book}.docx"):
        response = partner.get(path)
        assert response.status_code == 200
        assert len(Document(BytesIO(response.content)).inline_shapes) == 1

    submit(owner, share, is_shared="false")
    assert partner.get(image_url).status_code == 404
    for path in (f"/export/notes/{note}.docx", f"/export/notebooks/{book}.docx"):
        response = partner.get(path)
        assert response.status_code == 200
        assert len(Document(BytesIO(response.content)).inline_shapes) == 0


def test_shared_template_and_tag_can_be_used_but_only_owner_manages_them(family: tuple[TestClient, TestClient]) -> None:
    # 個人標籤和範本可單獨分享，套用後仍遵循目標筆記本的權限。
    a, b = family
    _, _, topic, _ = create_hierarchy(a, "甲的私人")
    template = int(submit(b, "/library/templates", name="家庭範本", content="共用範本文字",
                          is_shared="true").split("=")[-1])
    tag = int(submit(b, "/library/tags", name="家庭標籤", is_shared="true").split("=")[-1])
    note_url = submit(a, "/workspace/note", name="私人副本", parent_id=topic, template_id=template)
    note = int(note_url.split("=")[-1])
    submit(a, f"/library/notes/{note}/tags", tag_id=tag, action="add")
    assert "共用範本文字" in a.get(note_url).text
    assert "家庭標籤" in a.get(note_url).text
    assert b.get(note_url).status_code == 404
    assert "私人副本" not in b.get(f"/library/search?tag={tag}").text
    for kind, record in (("tags", tag), ("templates", template)):
        assert a.get(f"/library/{kind}?selected={record}").status_code == 200
        assert a.post(f"/library/{kind}", data={"record_id": record, "action": "delete"}, headers=HEADERS).status_code == 404
    submit(b, "/library/templates", record_id=template, name="家庭範本", content="共用範本文字", is_shared="false")
    assert a.get(f"/library/templates?selected={template}").status_code == 404
    assert "共用範本文字" in a.get(note_url).text


def test_legacy_database_preserves_notes_tags_templates_and_files(
    tmp_path: Path, accounts: tuple[auth.Settings, dict[str, str], dict[str, str]],
) -> None:
    # 建立沒有所有權欄位的舊 SQLite，連續啟動兩次也保留主鍵、標籤與附件。
    database = tmp_path / "legacy.db"
    file_id = "a" * 32 + ".txt"
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
            CREATE TABLE notebooks (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
            CREATE TABLE topic_lists (id INTEGER PRIMARY KEY, notebook_id INTEGER REFERENCES notebooks(id), name VARCHAR);
            CREATE TABLE topics (id INTEGER PRIMARY KEY, topic_list_id INTEGER REFERENCES topic_lists(id), name VARCHAR);
            CREATE TABLE notes (id INTEGER PRIMARY KEY, topic_id INTEGER REFERENCES topics(id), title VARCHAR, content VARCHAR);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE NOT NULL);
            CREATE TABLE note_tags (note_id INTEGER REFERENCES notes(id), tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
                                    PRIMARY KEY(note_id, tag_id));
            CREATE TABLE templates (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, content VARCHAR NOT NULL);
            CREATE TABLE attachments (id INTEGER PRIMARY KEY, note_id INTEGER REFERENCES notes(id),
                                      original_name VARCHAR, storage_name VARCHAR UNIQUE, media_type VARCHAR, size_bytes INTEGER);
            CREATE TABLE login_sessions (digest VARCHAR PRIMARY KEY, expires_at INTEGER);
            INSERT INTO notebooks VALUES (10, '舊筆記本');
            INSERT INTO topic_lists VALUES (20, 10, '舊清單');
            INSERT INTO topics VALUES (30, 20, '舊主題');
            INSERT INTO notes VALUES (40, 30, '舊記事', '原有內文');
            INSERT INTO tags VALUES (50, '原有標籤');
            INSERT INTO note_tags VALUES (40, 50);
            INSERT INTO templates VALUES (60, '原有範本', '原有版型');
            INSERT INTO login_sessions VALUES ('legacy', 0);
        """)
        db.execute("INSERT INTO attachments VALUES (70, 40, 'old.txt', ?, 'text/plain', 3)", (file_id,))
        db.commit()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / file_id).write_bytes(b"old")
    settings, a_credentials, b_credentials = accounts
    for credentials, visible in ((a_credentials, True), (b_credentials, False)):
        with TestClient(create_app(settings, database), base_url=ORIGIN) as browser:
            assert browser.get("/?kind=note&item_id=40", follow_redirects=False).status_code == 303
            submit(browser, "/login", **credentials)
            note = browser.get("/?kind=note&item_id=40")
            assert note.status_code == (200 if visible else 404)
            if visible:
                assert "原有內文" in note.text and "原有標籤" in note.text
                assert "更新時間未知" in note.text
                assert browser.get(f"/api/files/{file_id}").content == b"old"
                assert "舊記事" in browser.get("/library/search?q=原有內文").text
                assert "原有版型" in browser.get("/library/templates?selected=60").text
            else:
                assert browser.get(f"/api/files/{file_id}").status_code == 404
    with closing(sqlite3.connect(database)) as db:
        for table in ("notebooks", "tags", "templates"):
            assert db.execute(f"SELECT owner_id, is_shared FROM {table}").fetchall() == [(1, 0)]
        assert db.execute("SELECT 1 FROM login_sessions WHERE digest = 'legacy'").fetchone() is None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_web_backup_stays_disabled_after_partner_is_removed(
    tmp_path: Path, accounts: tuple[auth.Settings, dict[str, str], dict[str, str]],
) -> None:
    # 移除第二帳號設定後，主帳號仍不能從網頁下載伴侶留下的私人資料。
    database = tmp_path / "family.db"
    settings, primary, partner = accounts
    with TestClient(create_app(settings, database), base_url=ORIGIN) as browser:
        submit(browser, "/login", **partner)
        create_hierarchy(browser, "曾啟用的伴侶")
    single = replace(settings, partner_username="", partner_password_hash="")
    with TestClient(create_app(single, database), base_url=ORIGIN) as browser:
        submit(browser, "/login", **primary)
        homepage = browser.get("/")
        assert 'action="/backup"' not in homepage.text
        assert "曾啟用的伴侶" not in homepage.text
        assert browser.post("/backup", headers=HEADERS).status_code == 403

    replacement = replace(settings, partner_username="different-person")
    with pytest.raises(RuntimeError, match="第二帳號的名稱不符原有資料歸屬"):
        with TestClient(create_app(replacement, database), base_url=ORIGIN):
            pytest.fail("新的身分不得使用已保留的槽位")

    # 同一使用者可換密碼後恢復登入，原有所有權不變。
    new_password = secrets.token_urlsafe(24)
    restored = replace(settings, partner_password_hash=auth.hasher.hash(new_password))
    with TestClient(create_app(restored, database), base_url=ORIGIN) as browser:
        submit(browser, "/login", username=partner["username"], password=new_password)
        assert "曾啟用的伴侶" in browser.get("/").text


def test_legacy_inactive_partner_slot_cannot_be_reassigned(
    tmp_path: Path, accounts: tuple[auth.Settings, dict[str, str], dict[str, str]],
) -> None:
    # 舊資料只有帳號槽位且已停用時，保留私人記事並拒絕自動重綁。
    database = tmp_path / "legacy-family.db"
    settings, primary, partner = accounts
    with TestClient(create_app(settings, database), base_url=ORIGIN) as browser:
        submit(browser, "/login", **partner)
        create_hierarchy(browser, "保留的私人資料")
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("ALTER TABLE users DROP COLUMN username")
    single = replace(settings, partner_username="", partner_password_hash="")
    with TestClient(create_app(single, database), base_url=ORIGIN) as browser:
        submit(browser, "/login", **primary)
        assert "保留的私人資料" not in browser.get("/").text
    with pytest.raises(RuntimeError, match="第二帳號的名稱不符原有資料歸屬"):
        with TestClient(create_app(settings, database), base_url=ORIGIN):
            pytest.fail("缺少舊名稱時應交由本機管理員處理")
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT owner_id, name FROM notebooks").fetchall() == [(2, "保留的私人資料筆記本")]
