from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, Table, URL, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.search import note_text

class Base(DeclarativeBase):
    pass


class Notebook(Base):
    __tablename__ = "notebooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class NotebookPosition(Base):
    __tablename__ = "notebook_positions"

    notebook_id: Mapped[int] = mapped_column(ForeignKey("notebooks.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int]


class TopicList(Base):
    __tablename__ = "topic_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    notebook_id: Mapped[int] = mapped_column(ForeignKey("notebooks.id"), index=True)
    name: Mapped[str]


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_list_id: Mapped[int] = mapped_column(ForeignKey("topic_lists.id"), index=True)
    name: Mapped[str]


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)
    title: Mapped[str]
    content: Mapped[str] = mapped_column(default="")
    content_format: Mapped[str] = mapped_column(server_default="plain")
    version: Mapped[str] = mapped_column(default=lambda: uuid4().hex, server_default="legacy")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)


note_tags = Table(
    "note_tags",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    content: Mapped[str] = mapped_column(default="")


    content_format: Mapped[str] = mapped_column(server_default="plain")


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("notes.id"), index=True)
    original_name: Mapped[str]
    storage_name: Mapped[str] = mapped_column(unique=True)
    media_type: Mapped[str]
    size_bytes: Mapped[int]


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    ip: Mapped[str] = mapped_column(primary_key=True)
    failures: Mapped[int]
    locked_until: Mapped[int]


class LoginSession(Base):
    __tablename__ = "login_sessions"

    digest: Mapped[str] = mapped_column(primary_key=True)
    expires_at: Mapped[int] = mapped_column(index=True)


def configure_connection(connection: Connection, _record: object) -> None:
    # 每條連線開啟外鍵與忙碌等待，資料庫則持久使用 WAL。
    connection.execute("PRAGMA busy_timeout = 15000").close()
    cursor = connection.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    cursor.close()
    if mode not in {"wal", "memory"}:
        cursor = connection.execute("PRAGMA journal_mode = WAL")
        mode = cursor.fetchone()[0]
        cursor.close()
        if mode != "wal":
            raise RuntimeError("無法啟用 SQLite WAL 模式。")
    connection.execute("PRAGMA foreign_keys = ON").close()
    connection.create_function("peipeinote_text", 2, note_text, deterministic=True)


def make_engine(path: Path) -> Engine:
    # 使用 URL 物件傳遞路徑，避免特殊字元被當成連線設定。
    engine = create_engine(URL.create("sqlite", database=str(path)))
    event.listen(engine, "connect", configure_connection)
    return engine
