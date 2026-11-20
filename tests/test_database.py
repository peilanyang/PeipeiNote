from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, Note, Notebook, Tag, Topic, TopicList, make_engine, note_tags


def test_database_schema_and_foreign_keys(tmp_path: Path) -> None:
    # 建立四層結構，驗證標籤關聯與 SQLite 對孤兒筆記的攔截。
    engine = make_engine(tmp_path / "test.db")
    try:
        Base.metadata.create_all(engine)
        assert {
            "notebooks", "notebook_positions", "topic_lists", "topics", "notes", "tags", "note_tags",
            "templates", "attachments", "login_attempts", "login_sessions",
        } == set(inspect(engine).get_table_names())
        with Session(engine) as db, db.begin():
            notebook = Notebook(name="個人")
            db.add(notebook)
            db.flush()
            section = TopicList(name="資料", notebook_id=notebook.id)
            db.add(section)
            db.flush()
            topic = Topic(name="讀書", topic_list_id=section.id)
            db.add(topic)
            db.flush()
            note = Note(title="摘要", topic_id=topic.id)
            tag = Tag(name="閱讀")
            db.add_all([note, tag])
            db.flush()
            db.execute(note_tags.insert().values(note_id=note.id, tag_id=tag.id))
        with pytest.raises(IntegrityError), Session(engine) as db, db.begin():
            db.add(Note(title="無主題", topic_id=999))
    finally:
        engine.dispose()
