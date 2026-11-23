from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Note, Notebook, Tag, Topic, TopicList, initialize_household, make_engine, note_tags


def test_database_schema_and_foreign_keys(tmp_path: Path) -> None:
    # 建立四層結構，驗證標籤關聯與 SQLite 對孤兒記事的攔截。
    engine = make_engine(tmp_path / "test.db")
    try:
        initialize_household(engine)
        assert {
            "notebooks", "notebook_positions", "topic_lists", "topics", "notes", "tags", "note_tags",
            "templates", "attachments", "login_attempts", "login_sessions", "users",
        } == set(inspect(engine).get_table_names())
        with Session(engine) as db, db.begin():
            notebook = Notebook(owner_id=1, name="個人")
            db.add(notebook)
            db.flush()
            section = TopicList(name="資料", notebook_id=notebook.id)
            db.add(section)
            db.flush()
            topic = Topic(name="讀書", topic_list_id=section.id)
            db.add(topic)
            db.flush()
            note = Note(title="摘要", topic_id=topic.id)
            tag = Tag(owner_id=1, name="閱讀")
            db.add_all([note, tag])
            db.flush()
            db.execute(note_tags.insert().values(note_id=note.id, tag_id=tag.id))
        with pytest.raises(IntegrityError), Session(engine) as db, db.begin():
            db.add(Note(title="無主題", topic_id=999))
    finally:
        engine.dispose()


def test_wal_allows_writes_during_a_read_snapshot(tmp_path: Path) -> None:
    # 每條連線等候寫入鎖，讀取交易期間另一條連線仍能提交。
    engine = make_engine(tmp_path / "wal.db")
    try:
        initialize_household(engine)
        with engine.connect() as reader, engine.connect() as writer:
            for connection in (reader, writer):
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
                assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 15_000
                assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                connection.commit()
            reader.exec_driver_sql("BEGIN")
            assert reader.execute(Notebook.__table__.select()).all() == []
            with writer.begin():
                writer.execute(Notebook.__table__.insert().values(name="寫入成功", owner_id=1))
            assert reader.execute(Notebook.__table__.select()).all() == []
            reader.commit()
            assert reader.execute(Notebook.__table__.select()).one().name == "寫入成功"
    finally:
        engine.dispose()


def test_fetch_tag_notes_does_not_load_full_content(tmp_path: Path) -> None:
    # 標籤文章列表只需要清單欄位，不應把記事全文從資料庫搬回來。
    from sqlalchemy import event

    from app.library import fetch_tag_notes

    engine = make_engine(tmp_path / "tags.db")
    captured: list[str] = []

    def remember_statement(
        _connection: object, _cursor: object, statement: str,
        _parameters: object, _context: object, _executemany: object,
    ) -> None:
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", remember_statement)
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            book = Notebook(owner_id=1, name="工作")
            db.add(book)
            db.flush()
            section = TopicList(notebook_id=book.id, name="清單")
            db.add(section)
            db.flush()
            topic = Topic(name="主題", topic_list_id=section.id)
            db.add(topic)
            db.flush()
            note = Note(title="記事", content="列表不需要搬運的完整內文", topic_id=topic.id)
            tag = Tag(owner_id=1, name="標籤")
            db.add_all([note, tag])
            db.flush()
            db.execute(note_tags.insert().values(note_id=note.id, tag_id=tag.id))
            tag_id = tag.id
        with Session(engine) as db:
            notes, has_more = fetch_tag_notes(db, tag_id, 1, 1)
        assert has_more is False
        assert [set(row) for row in notes] == [{"id", "title", "notebook", "topic", "updated_at"}]
        tag_queries = [sql for sql in captured if "FROM notes AS n" in sql]
        assert tag_queries and all("n.content" not in sql for sql in tag_queries)
    finally:
        engine.dispose()


def test_workspace_tree_returns_six_columns_per_visible_topic(tmp_path: Path) -> None:
    # workspace_tree 每行固定 6 欄，且只含目前帳號可見筆記本下的主題。
    from app.access import workspace_tree

    engine = make_engine(tmp_path / "tree.db")
    try:
        initialize_household(engine)
        with Session(engine) as db, db.begin():
            book = Notebook(owner_id=1, name="我的筆記本")
            db.add(book)
            db.flush()
            section = TopicList(notebook_id=book.id, name="清單")
            db.add(section)
            db.flush()
            topics = [Topic(name="甲", topic_list_id=section.id), Topic(name="乙", topic_list_id=section.id)]
            db.add_all(topics)
            db.flush()
            expected = {(book.id, "我的筆記本", section.id, "清單", topic.id, topic.name) for topic in topics}
        with Session(engine) as db:
            rows_a = db.execute(workspace_tree(1)).all()
            rows_b = db.execute(workspace_tree(2)).all()
        assert all(len(row) == 6 for row in rows_a)
        assert set(rows_a) == expected
        # 第二帳號看不到第一帳號的私人主題。
        assert rows_b == []
    finally:
        engine.dispose()
