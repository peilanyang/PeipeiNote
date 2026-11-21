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
