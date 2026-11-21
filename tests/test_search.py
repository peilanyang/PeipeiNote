from pathlib import Path

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
