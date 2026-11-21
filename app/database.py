from pathlib import Path
from sqlite3 import Connection
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, ForeignKey, Table, UniqueConstraint, URL, create_engine, event, inspect, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.search import note_text

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("id IN (1, 2)"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str | None]


class Owned:
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    is_shared: Mapped[bool] = mapped_column(server_default="0")


class Notebook(Owned, Base):
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


class Tag(Owned, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


note_tags = Table(
    "note_tags",
    Base.metadata,
    Column("note_id", ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Template(Owned, Base):
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
    storage_name: Mapped[str]
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


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


def initialize_household(engine: Engine, partner_username: str | None = None) -> None:
    # 保留原有所有權，並把第二帳號槽位固定綁定到原使用者名稱。
    with engine.connect() as connection:
        # SQLite 只有在停用此連線的外鍵檢查時，才允許新增帶預設值的外鍵。
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        try:
            with connection.begin():
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                Base.metadata.create_all(connection)
                schema = inspect(connection)
                if "username" not in {column["name"] for column in schema.get_columns("users")}:
                    connection.exec_driver_sql("ALTER TABLE users ADD COLUMN username VARCHAR")
                    # 已停用且沒有舊名稱可核對的槽位永久保留，由本機管理員處理。
                    connection.execute(update(User).where(User.id == 2).values(username=partner_username or ""))
                connection.execute(insert(User).values(id=1).on_conflict_do_nothing())
                if partner_username:
                    connection.execute(insert(User).values(id=2, username=partner_username).on_conflict_do_nothing())
                    if connection.scalar(select(User.username).where(User.id == 2)) != partner_username:
                        raise RuntimeError("第二帳號的名稱不符原有資料歸屬。請恢復原設定或先由本機管理員處理舊資料。")
                if "owner_id" not in {column["name"] for column in schema.get_columns("notebooks")}:
                    connection.exec_driver_sql(
                        "ALTER TABLE notebooks ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id)"
                    )
                    connection.exec_driver_sql("ALTER TABLE notebooks ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT 0")
                if "owner_id" not in {column["name"] for column in schema.get_columns("templates")}:
                    connection.exec_driver_sql(
                        "ALTER TABLE templates ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES users(id)"
                    )
                    connection.exec_driver_sql("ALTER TABLE templates ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT 0")
                if "owner_id" not in {column["name"] for column in schema.get_columns("tags")}:
                    # 保留標籤 ID 與既有連結，名稱改為每帳號各自唯一。
                    connection.exec_driver_sql("CREATE TEMP TABLE legacy_tags AS SELECT id, name FROM tags")
                    Tag.__table__.drop(connection)
                    Tag.__table__.create(connection)
                    connection.exec_driver_sql(
                        "INSERT INTO tags (id, name, owner_id) SELECT id, name, 1 FROM legacy_tags"
                    )
                    connection.exec_driver_sql("DROP TABLE legacy_tags")
                if "user_id" not in {column["name"] for column in schema.get_columns("login_sessions")}:
                    LoginSession.__table__.drop(connection)
                    LoginSession.__table__.create(connection)
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("資料庫外鍵檢查失敗，已取消雙帳號遷移。")
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
