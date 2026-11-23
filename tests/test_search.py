from pathlib import Path
import html

from sqlalchemy.orm import Session

from app.database import Note, Notebook, Topic, TopicList, initialize_household, make_engine
from app.search import initialize_search, search_rows


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
