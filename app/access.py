from typing import TypeVar

from sqlalchemy import Select, or_, select
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
    # 只有能讀取連結筆記的人，才能看到該筆記使用的標籤。
    linked = notebook_items(Note, user_id).join(note_tags, note_tags.c.note_id == Note.id)
    return or_(visible_to(Tag, user_id), Tag.id.in_(linked.with_only_columns(note_tags.c.tag_id)))
