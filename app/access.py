from typing import TypeVar

from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.database import Attachment, Note, Notebook, Owned, Tag, Topic, TopicList, note_tags

NotebookItem = TypeVar("NotebookItem", Notebook, TopicList, Topic, Note, Attachment)


def visible_to(model: type[Owned], user_id: int) -> ColumnElement[bool]:
    # 獨立資源的共用條件，也用於筆記本祖先。
    return or_(model.owner_id == user_id, model.is_shared.is_(True))


def notebook_items(model: type[NotebookItem], user_id: int) -> Select[tuple[NotebookItem]]:
    # 由實際外鍵回溯到筆記本，在 SQL 查詢內限制存取。
    query = select(model)
    if model is Attachment:
        query = query.join(Note, Attachment.note_id == Note.id)
    if model in (Attachment, Note):
        query = query.join(Topic, Note.topic_id == Topic.id)
    if model in (Attachment, Note, Topic):
        query = query.join(TopicList, Topic.topic_list_id == TopicList.id)
    if model is not Notebook:
        query = query.join(Notebook, TopicList.notebook_id == Notebook.id)
    return query.where(visible_to(Notebook, user_id))


def containing_book(db: Session, model: type[NotebookItem], item_id: int, user_id: int) -> Notebook:
    # 呼叫者已在同一交易中取得該項目，只接受其實際所屬筆記本。
    query = notebook_items(model, user_id).where(model.id == item_id)
    return db.scalars(query.with_only_columns(Notebook, maintain_column_froms=True)).one()


def search_label(user_id: int) -> ColumnElement[bool]:
    # 只有能讀取連結記事的人，才能看到該記事使用的標籤。
    linked = notebook_items(Note, user_id).join(note_tags, note_tags.c.note_id == Note.id)
    return or_(visible_to(Tag, user_id), Tag.id.in_(linked.with_only_columns(note_tags.c.tag_id)))


def workspace_tree(user_id: int) -> Select[tuple[int, str, int, str, int, str, int]]:
    """
    單一 CTE 遞迴查詢：一次取得使用者可見的完整工作區樹狀結構。
    回傳欄位：(notebook_id, notebook_name, section_id, section_name, topic_id, topic_name, topic_notebook_id)

    階層：notebooks → topic_lists (sections) → topics
    """
    # 基礎查詢：使用者可見的筆記本
    visible_books = select(
        Notebook.id.label("notebook_id"),
        Notebook.name.label("notebook_name"),
    ).where(visible_to(Notebook, user_id)).cte("visible_books")

    # 以 section 為單位向上接筆記本
    sections = select(
        visible_books.c.notebook_id,
        visible_books.c.notebook_name,
        TopicList.id.label("section_id"),
        TopicList.name.label("section_name"),
    ).join(TopicList, TopicList.notebook_id == visible_books.c.notebook_id).cte("sections")

    # 以 topic 為單位向上接 section 和筆記本
    topics = select(
        sections.c.notebook_id,
        sections.c.notebook_name,
        sections.c.section_id,
        sections.c.section_name,
        Topic.id.label("topic_id"),
        Topic.name.label("topic_name"),
    ).join(Topic, Topic.topic_list_id == sections.c.section_id)

    return topics


def get_visible_tag_ids(db: Session, user_id: int) -> set[int]:
    """
    單次查詢取得使用者可見的標籤 ID 集合（請求級快取）。
    包含：1) 使用者擁有的標籤 2) 共用標籤 3) 連結在使用者可見記事上的標籤
    """
    # 使用 search_label 條件：擁有者或共用，或連結在可見記事上
    linked = notebook_items(Note, user_id).join(note_tags, note_tags.c.note_id == Note.id)
    condition = or_(visible_to(Tag, user_id), Tag.id.in_(linked.with_only_columns(note_tags.c.tag_id)))
    return set(db.scalars(select(Tag.id).where(condition)).all())
