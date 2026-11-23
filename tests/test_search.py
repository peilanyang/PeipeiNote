from pathlib import Path
import html

from sqlalchemy.orm import Session

from app.database import Note, Notebook, Topic, TopicList, initialize_household, make_engine
from app.search import initialize_search, search_clauses, search_rows


def test_index_existing_notes(tmp_path: Path) -> None:
    # 先建立舊版記事再加入搜尋索引，確認回填可重跑且不重複。
    engine = make_engine(tmp_path / "existing.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="原有筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            db.add(Note(title="舊記事", topic_id=1, content="<p>已有<strong>北海道</strong>行程</p>", content_format="html"))
        initialize_search(engine)
        initialize_search(engine)
        with Session(engine) as db:
            rows, more = search_rows(db, "北海道", None, None, 1, 1)
            assert not more and len(rows) == 1
            assert rows[0]["title"] == "舊記事"
            assert rows[0]["summary"] == "已有北海道行程"
    finally:
        engine.dispose()


def test_search_summary_xss_safe(tmp_path: Path) -> None:
    # 驗證搜尋摘要會被 HTML 轉義，防止 XSS。
    # 若純文字內文含 <script>alert(1)</script>，搜尋 "script" 時摘要應為轉義後的純文字。
    engine = make_engine(tmp_path / "xss.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="測試筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            # 純文字內文保留原始標籤文字
            db.add(Note(
                title="測試文章",
                topic_id=1,
                content="正常文字 <script>alert(1)</script> 更多文字",
                content_format="plain"
            ))
        initialize_search(engine)
        with Session(engine) as db:
            rows, more = search_rows(db, "script", None, None, 1, 1)
            assert not more and len(rows) == 1
            assert rows[0]["title"] == "測試文章"
            # 摘要應被 html.escape() 轉義，不含原始 <script> 標籤
            summary = rows[0]["summary"]
            assert "<script>" not in summary  # 原始標籤不應出現
            assert "&lt;script&gt;" in summary  # 應為轉義後的 HTML 實體 <script>
            # 驗證摘要內容包含預期的轉義文字片段
            expected = html.escape("正常文字 <script>alert(1)</script> 更多文字")[:120]
            assert expected in summary
    finally:
        engine.dispose()


def test_search_clauses_builds_and_terms_and_escapes_quotes() -> None:
    # 空格拆詞：長詞合併為一個 FTS AND 字面量、短詞各走 instr，引號加倍轉義。
    clauses, params = search_clauses('a "tokyo" os 北海道')
    assert clauses[0] == "note_search MATCH :phrase"
    assert params["phrase"] == '"""tokyo""" AND "北海道"'
    assert params["short0"] == "a"
    assert params["short1"] == "os"
    assert len(clauses) == 3
    empty_clauses, empty_params = search_clauses("   ")
    assert empty_clauses == [] and empty_params == {}


def test_search_multiword_requires_all_terms_and_may_cross_columns(tmp_path: Path) -> None:
    # 多詞為 AND：各詞可分別在標題或內文、不須相鄰；缺任一词的記事不命中。
    engine = make_engine(tmp_path / "and.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="測試筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            db.add(Note(id=1, title="Tokyo Notes", topic_id=1,
                        content="<p>之後也要去 osaka 玩</p>", content_format="html"))
            db.add(Note(id=2, title="Osaka Diary", topic_id=1,
                        content="<p>只有 osaka 的內容在這裡</p>", content_format="html"))
            db.add(Note(id=3, title="鄰近城市", topic_id=1,
                        content="<p>tokyo 和 osaka 是鄰近城市</p>", content_format="html"))
        initialize_search(engine)
        with Session(engine) as db:
            rows, more = search_rows(db, "tokyo osaka", None, None, 1, 1)
            assert not more and [row["id"] for row in rows] == [3, 1]
            assert any("osaka" in row["summary"] for row in rows if row["id"] == 1)
            rows, _ = search_rows(db, "osaka", None, None, 1, 1)
            assert {row["id"] for row in rows} == {1, 2, 3}
    finally:
        engine.dispose()


def test_search_mixes_short_and_long_terms(tmp_path: Path) -> None:
    # 短於 3 字元的詞組不成 trigram，改走 instr 子字串，並與長詞做 AND。
    engine = make_engine(tmp_path / "mixed.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="測試筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            db.add(Note(id=1, title="Zebra House", topic_id=1,
                        content="<p>the zebra sleeps in the house</p>", content_format="html"))
            db.add(Note(id=2, title="Zebra Garden", topic_id=1,
                        content="<p>the zebra grazes in the garden</p>", content_format="html"))
        initialize_search(engine)
        with Session(engine) as db:
            rows, _ = search_rows(db, "zebra ho", None, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
            rows, _ = search_rows(db, "ho", None, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
    finally:
        engine.dispose()


def test_search_cjk_multiword_short_terms(tmp_path: Path) -> None:
    # 中文短詞（<3 字元）多詞搜尋：舊行為把含空格整串當連續片段永遠查不到，現為 AND。
    engine = make_engine(tmp_path / "cjk.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="測試筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            db.add(Note(id=1, title="東京指南", topic_id=1,
                        content="<p>住宿在札幌市</p>", content_format="html"))
            db.add(Note(id=2, title="大阪指南", topic_id=1,
                        content="<p>札幌市也是好選擇</p>", content_format="html"))
        initialize_search(engine)
        with Session(engine) as db:
            rows, _ = search_rows(db, "東京 札幌", None, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
            rows, _ = search_rows(db, "東京", None, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
    finally:
        engine.dispose()


def test_search_multiword_is_case_insensitive(tmp_path: Path) -> None:
    # FTS5 trigram 預設大小寫不敏感（含跨欄位 AND），與短詞 instr 分支一致。
    engine = make_engine(tmp_path / "case.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="測試筆記本"))
            db.flush()
            db.add(TopicList(id=1, notebook_id=1, name="清單"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題"))
            db.flush()
            db.add(Note(id=1, title="BLUE SKY PLAN", topic_id=1,
                        content="<p>細節之後再補</p>", content_format="html"))
            db.add(Note(id=2, title="blue note", topic_id=1,
                        content="<p>sky high above</p>", content_format="html"))
        initialize_search(engine)
        with Session(engine) as db:
            rows, _ = search_rows(db, "blue sky", None, None, 1, 1)
            assert {row["id"] for row in rows} == {1, 2}
            rows, _ = search_rows(db, "BLUE SKY", None, None, 1, 1)
            assert {row["id"] for row in rows} == {1, 2}
    finally:
        engine.dispose()


def test_search_multiword_with_book_scope(tmp_path: Path) -> None:
    # AND 條件與筆記本範圍篩選可並用：另一本只有部分詞的記事不會漏出。
    engine = make_engine(tmp_path / "scoped.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            db.add(Notebook(id=1, owner_id=1, name="本一"))
            db.add(Notebook(id=2, owner_id=1, name="本二"))
            db.add(TopicList(id=1, notebook_id=1, name="清單一"))
            db.add(TopicList(id=2, notebook_id=2, name="清單二"))
            db.flush()
            db.add(Topic(id=1, topic_list_id=1, name="主題一"))
            db.add(Topic(id=2, topic_list_id=2, name="主題二"))
            db.flush()
            db.add(Note(id=1, title="行程規劃", topic_id=1,
                        content="<p>目的地是東京</p>", content_format="html"))
            db.add(Note(id=2, title="行程規劃", topic_id=2,
                        content="<p>目的地是大阪</p>", content_format="html"))
        initialize_search(engine)
        with Session(engine) as db:
            rows, _ = search_rows(db, "東京 行程", None, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
            rows, _ = search_rows(db, "東京 行程", 2, None, 1, 1)
            assert rows == []
            rows, _ = search_rows(db, "東京 行程", 1, None, 1, 1)
            assert [row["id"] for row in rows] == [1]
    finally:
        engine.dispose()
