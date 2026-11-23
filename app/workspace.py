from dataclasses import dataclass
from enum import Enum
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import Select, column, delete, func, or_, select, table
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.access import containing_book, notebook_items, visible_to, workspace_tree, get_visible_tag_ids
from app.attachments import remove_stored_attachment
from app.auth import ROOT, Settings, require_user
from app.database import (
    Attachment, Note, Notebook, NotebookPosition, Tag, Template, Topic, TopicList, User, new_note_version, note_tags,
)

from app.richtext import NoteFormat, clean_html, editor_source, template_html

MAX_ID = 2**63 - 1
NAME_LIMIT = 200
CONTENT_LIMIT = 100_000
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
search_text = table("note_search", column("rowid"), column("body"))


class Kind(str, Enum):
    notebook = "notebook"
    section = "section"
    topic = "topic"
    note = "note"


class Action(str, Enum):
    save = "save"
    move = "move"
    delete = "delete"
    share = "share"


Item = Notebook | TopicList | Topic | Note


@dataclass(frozen=True)
class Level:
    model: type[Notebook] | type[TopicList] | type[Topic] | type[Note]
    label: str
    parent: Kind | None = None
    parent_key: str = ""


LEVELS = {
    Kind.notebook: Level(Notebook, "筆記本"),
    Kind.section: Level(TopicList, "主題清單", Kind.notebook, "notebook_id"),
    Kind.topic: Level(Topic, "主題", Kind.section, "topic_list_id"),
    Kind.note: Level(Note, "記事", Kind.topic, "topic_id"),
}
CHILD = {level.parent: kind for kind, level in LEVELS.items()}


def item_url(kind: Kind | None, item_id: int | None) -> str:
    # 一律建立站內連結，避免把表單資料用於重新導向。
    return f"/?kind={kind.value}&item_id={item_id}" if kind is not None else "/"


def get_item(db: Session, kind: Kind, item_id: int, user_id: int, *, owner_only: bool = False) -> Item:
    # 依允許的層級和主鍵查詢，找不到時回傳 404。
    model = LEVELS[kind].model
    query = notebook_items(model, user_id).where(model.id == item_id)
    if owner_only:
        query = query.where(Notebook.owner_id == user_id)
    item = db.scalar(query)
    if item is None:
        raise HTTPException(404, f"找不到這個{LEVELS[kind].label}。")
    return item


def notebook_order(db: Session, user_id: int, *, owner_only: bool = False) -> list[Notebook]:
    # 已排序的筆記本排在前面，新筆記本依建立順序接在後面。
    query = notebook_items(Notebook, user_id).outerjoin(NotebookPosition)
    if owner_only:
        query = query.where(Notebook.owner_id == user_id)
    return list(db.scalars(query.order_by(
        NotebookPosition.position.is_(None), NotebookPosition.position, Notebook.id
    )))


def title_path(item: Item, kind: Kind, items: dict[Kind, dict[int, Item]]) -> str:
    # 移動清單顯示完整路徑，以區分同名主題。
    names = [item.title if isinstance(item, Note) else item.name]
    while LEVELS[kind].parent is not None:
        level = LEVELS[kind]
        parent = level.parent
        item = items[parent][getattr(item, level.parent_key)]
        if parent != Kind.section:
            names.append(item.name)
        kind = parent
    return " / ".join(reversed(names))


def render_workspace(
    request: Request, engine: Engine, user_id: int, kind: Kind | None, item_id: int | None, new_note: bool = False,
) -> Response:
    # 左欄讀取階層，中欄只查詢目前主題的記事，右欄載入選取的項目。
    # 優化：使用 CTE 遞迴查詢一次取得 notebooks → sections → topics，查詢數從 9 降至 ≤3。
    with Session(engine) as db, db.begin():
        db.connection().exec_driver_sql("BEGIN")
        selection = get_item(db, kind, item_id, user_id) if kind is not None and item_id is not None else None
        if isinstance(selection, TopicList):
            # 舊主題清單連結改顯示所屬筆記本，仍先確認原項目的存取權。
            selection = get_item(db, Kind.notebook, selection.notebook_id, user_id)
            kind = Kind.notebook
        books = notebook_order(db, user_id)
        own_books = [book for book in books if book.owner_id == user_id]

        # 單一 CTE 查詢取得完整樹：notebooks → topic_lists (sections) → topics
        tree_rows = db.execute(workspace_tree(user_id)).all()

        # 從 CTE 結果組裝 section_map、topics、topics_by_book
        section_map: dict[int, TopicList] = {}
        topics: list[Topic] = []
        topics_by_book: dict[int, list[Topic]] = {}
        for (notebook_id, _nb_name, section_id, section_name, topic_id, topic_name) in tree_rows:
            # 建立 section 物件（若尚未建立）
            if section_id not in section_map:
                section_map[section_id] = TopicList(id=section_id, notebook_id=notebook_id, name=section_name)
            # 建立 topic 物件
            topic = Topic(id=topic_id, topic_list_id=section_id, name=topic_name)
            topics.append(topic)
            topics_by_book.setdefault(notebook_id, []).append(topic)

        items = {
            Kind.notebook: {row.id: row for row in books},
            Kind.section: section_map,
            Kind.topic: {row.id: row for row in topics},
        }
        active = {}
        ancestor = selection
        ancestor_kind = kind
        while ancestor is not None and ancestor_kind is not None:
            active[ancestor_kind.value] = ancestor.id
            level = LEVELS[ancestor_kind]
            ancestor_kind = level.parent
            if ancestor_kind is not None:
                ancestor = items[ancestor_kind][getattr(ancestor, level.parent_key)]
        topic_id = active.get("topic")
        active_book = items[Kind.notebook].get(active.get("notebook"))
        owns_notebook = active_book is not None and active_book.owner_id == user_id

        # 記事列表：同主題下的記事，含 FTS5 摘要
        notes = db.execute(
            notebook_items(Note, user_id).with_only_columns(
                Note.id, Note.title, Note.updated_at, func.substr(search_text.c.body, 1, 100).label("preview")
            )
            .outerjoin(search_text, search_text.c.rowid == Note.id)
            .where(Note.topic_id == topic_id).order_by(Note.id)
        ).all() if topic_id else []

        parent = LEVELS[kind].parent if kind is not None else None
        destinations = []
        if kind == Kind.topic:
            # 主題的移動目的地以筆記本顯示，跨本移動只提供自己的筆記本。
            if owns_notebook:
                destinations = [(book.id, book.name) for book in own_books if book.id != active_book.id]
        elif parent is not None:
            allowed = notebook_items(LEVELS[parent].model, user_id).where(or_(
                Notebook.id == active_book.id,
                Notebook.owner_id == user_id if owns_notebook else False,
            ))
            destinations = [
                (row.id, title_path(row, parent, items))
                for row in db.scalars(allowed)
                if row.id != active.get(parent.value)
            ]

        # 標籤：使用快取的可見標籤 ID，避免重複執行子查詢
        visible_tag_ids = get_visible_tag_ids(db, user_id)
        tags = list(db.scalars(select(Tag).where(Tag.id.in_(visible_tag_ids)).order_by(Tag.name))) if visible_tag_ids else []
        assigned_tags = []
        if isinstance(selection, Note):
            # 可見標籤限制：擁有者或共用，且需能存取該記事
            visible_note_tags = notebook_items(Note, user_id).join(note_tags, note_tags.c.tag_id == Tag.id).where(Note.id == selection.id)
            query = visible_note_tags.join(Tag, Tag.id == note_tags.c.tag_id).where(Note.id == selection.id)
            assigned_tags = list(db.scalars(query.with_only_columns(Tag).order_by(Tag.name)))
        assigned_tag_ids = {tag.id for tag in assigned_tags}

        # 附件：僅在選取記事時查詢
        attachments = []
        if isinstance(selection, Note):
            attachments = list(db.scalars(notebook_items(Attachment, user_id).where(
                Attachment.note_id == selection.id
            ).order_by(Attachment.id)))

        # 範本：僅在有主題或快速建立時查詢
        note_templates = []
        if topic_id or (new_note and selection is None):
            note_templates = db.execute(select(Template.id, Template.name).where(
                visible_to(Template, user_id)
            ).order_by(Template.name)).all()

        return templates.TemplateResponse(
            request=request,
            name="workspace.html",
            context={
                "books": books, "topics_by_book": topics_by_book, "notes": notes,
                "active": active, "current_topic": items[Kind.topic].get(topic_id),
                "quick_create": new_note and selection is None,
                "quick_topics": [(row.id, title_path(row, Kind.topic, items)) for row in topics]
                if new_note and selection is None else [],
                "selection": selection, "kind": kind.value if kind else None,
                "attachments": attachments,
                "tags": tags, "assigned_tags": assigned_tags,
                "available_tags": [tag for tag in tags if tag.id not in assigned_tag_ids],
                "note_templates": note_templates,
                "owns_notebook": owns_notebook,
                "web_backup": db.get(User, 2) is None and user_id == 1,
                "editor_content_format": selection.content_format if isinstance(selection, Note) else "plain",
                "editor_content": editor_source(selection.content, selection.content_format)
                if isinstance(selection, Note) else "",
                "label": LEVELS[kind].label if kind else None,
                "destinations": destinations,
                "has_children": selection is not None and kind is not None and has_children(db, kind, selection.id),
                "can_raise": bool(owns_notebook and kind == Kind.notebook and own_books[0].id != selection.id),
                "can_lower": bool(owns_notebook and kind == Kind.notebook and own_books[-1].id != selection.id),
            },
            headers={"Cache-Control": "no-store"},
        )


def save_fields(item: Item, name: str, content: str, content_format: NoteFormat = "plain") -> None:
    # 純文字原樣儲存，富文字先經過 HTML 允許清單淨化。
    name = name.strip()
    if not 1 <= len(name) <= NAME_LIMIT:
        raise HTTPException(422, "名稱須為 1 至 200 個字元。")
    if isinstance(item, Note):
        if len(content) > CONTENT_LIMIT:
            raise HTTPException(422, "記事超過 100,000 個字元。")
        item.title = name
        item.content = clean_html(content) if content_format == "html" else content
        item.content_format = content_format
    else:
        item.name = name


def move_notebook(db: Session, item_id: int, direction: int, user_id: int) -> None:
    # 筆記本位於最上層，移動操作調整清單順序。
    ordered = [book.id for book in notebook_order(db, user_id, owner_only=True)]
    index = ordered.index(item_id)
    target = index + direction
    if direction not in {-1, 1} or not 0 <= target < len(ordered):
        raise HTTPException(422, "這個筆記本無法往該方向移動。")
    ordered[index], ordered[target] = ordered[target], ordered[index]
    for position, notebook_id in enumerate(ordered):
        db.merge(NotebookPosition(notebook_id=notebook_id, position=position))


def has_children(db: Session, kind: Kind, item_id: int) -> bool:
    # 保護下層資料，有子項目或附件時必須先移走或清空。
    child_kind = CHILD.get(kind)
    if kind == Kind.notebook:
        existing = db.scalar(select(Topic.id).join(TopicList).where(TopicList.notebook_id == item_id).limit(1))
    elif child_kind is not None:
        child = LEVELS[child_kind]
        model = child.model
        existing = db.scalar(select(model.id).where(getattr(model, child.parent_key) == item_id).limit(1))
    else:
        existing = db.scalar(select(Attachment.id).where(Attachment.note_id == item_id).limit(1))
    return existing is not None


def remove_item(db: Session, kind: Kind, item: Item, delete_descendants: bool) -> list[str]:
    # 已驗權的項目若有下層資料，須收到另外一次明確確認。
    if not delete_descendants and has_children(db, kind, item.id):
        raise HTTPException(409, "此項目仍有下層資料或附件，請重新開啟頁面並確認是否一併刪除。")

    # 沿外鍵取得子樹，在同一筆交易由附件、記事、主題到上層依序刪除。
    levels: list[tuple[type[Notebook] | type[TopicList] | type[Topic] | type[Note], Select[tuple[int]]]] = []
    model = LEVELS[kind].model
    ids = select(model.id).where(model.id == item.id)
    levels.append((model, ids))
    child_kind = CHILD.get(kind)
    while child_kind is not None:
        child = LEVELS[child_kind]
        model = child.model
        ids = select(model.id).where(getattr(model, child.parent_key).in_(ids))
        levels.append((model, ids))
        child_kind = CHILD.get(child_kind)

    attachments = list(db.scalars(select(Attachment.storage_name).where(Attachment.note_id.in_(ids))))
    db.execute(delete(Attachment).where(Attachment.note_id.in_(ids)))
    # 舊版資料庫的關聯表可能沒有記事刪除串聯規則。
    db.execute(delete(note_tags).where(note_tags.c.note_id.in_(ids)))
    for model, ids in reversed(levels):
        db.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False))
    return attachments


def inbox_topic(db: Session, user_id: int) -> int:
    # 在同一筆交易建立或重用首則記事的收件匣，避免重複建立。
    book = db.scalar(select(Notebook).where(Notebook.name == "收件匣", Notebook.owner_id == user_id, Notebook.is_shared.is_(False)).order_by(Notebook.id).limit(1))
    if book is None:
        book = Notebook(name="收件匣", owner_id=user_id)
        db.add(book)
        db.flush()
    section = db.scalar(select(TopicList).where(
        TopicList.notebook_id == book.id, TopicList.name == "一般",
    ).order_by(TopicList.id).limit(1))
    if section is None:
        section = TopicList(notebook_id=book.id, name="一般")
        db.add(section)
        db.flush()
    topic = db.scalar(select(Topic).where(
        Topic.topic_list_id == section.id, Topic.name == "隨手記",
    ).order_by(Topic.id).limit(1))
    if topic is None:
        topic = Topic(topic_list_id=section.id, name="隨手記")
        db.add(topic)
        db.flush()
    return topic.id


def topic_list_for_book(db: Session, notebook_id: int, user_id: int) -> int:
    # 保留現有資料庫結構，從已驗權的筆記本取得或建立主題用的內部容器。
    get_item(db, Kind.notebook, notebook_id, user_id)
    section_id = db.scalar(select(TopicList.id).where(
        TopicList.notebook_id == notebook_id,
    ).order_by(TopicList.id).limit(1))
    if section_id is not None:
        return section_id
    section = TopicList(notebook_id=notebook_id, name="一般")
    db.add(section)
    db.flush()
    return section.id


def workspace_actions(engine: Engine, settings: Settings, uploads: FilePath) -> APIRouter:
    # 所有資料變更共用登入驗證與同源檢查。
    router = APIRouter(prefix="/workspace")

    @router.post("/{kind}")
    def create(
        request: Request,
        kind: Kind,
        name: Annotated[str, Form()],
        parent_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        notebook_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        template_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        use_inbox: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        # 建立項目前確認父層存在，並在同一交易中寫入。
        user_id = require_user(engine, settings, request, write=True)
        level = LEVELS[kind]
        item = level.model()
        save_fields(item, name, "")
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if use_inbox:
                if kind != Kind.note or parent_id is not None:
                    raise HTTPException(422, "收件匣只能用於沒有指定主題的新記事。")
                parent_id = inbox_topic(db, user_id)
            if notebook_id is not None:
                if kind != Kind.topic or parent_id is not None:
                    raise HTTPException(422, "指定筆記本的欄位僅適用於新增主題。")
                parent_id = topic_list_for_book(db, notebook_id, user_id)
            if level.parent is not None:
                if parent_id is None:
                    raise HTTPException(422, "請先選擇上層項目。")
                get_item(db, level.parent, parent_id, user_id)
                setattr(item, level.parent_key, parent_id)
            elif parent_id is not None:
                raise HTTPException(422, "筆記本位於最上層。")
            if isinstance(item, Notebook):
                item.owner_id = user_id
            if template_id is not None:
                if not isinstance(item, Note):
                    raise HTTPException(422, "範本只能套用至記事。")
                template = db.scalar(select(Template).where(Template.id == template_id, visible_to(Template, user_id)))
                if template is None:
                    raise HTTPException(404, "找不到這個範本。")
                item.content_format = template.content_format
                item.content = template_html(template.content) if template.content_format == "html" else template.content
            db.add(item)
            db.flush()
            return RedirectResponse(item_url(kind, item.id), status_code=303)

    @router.post("/{kind}/{item_id}/{action}")
    def change(
        request: Request,
        kind: Kind,
        item_id: Annotated[int, Path(gt=0, le=MAX_ID)],
        action: Action,
        name: Annotated[str, Form()] = "",
        content: Annotated[str, Form()] = "",
        content_format: Annotated[NoteFormat, Form()] = "plain",
        version: Annotated[str | None, Form(max_length=32)] = None,
        target_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        notebook_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        direction: Annotated[int, Form(ge=-1, le=1)] = 0,
        confirmed: Annotated[bool, Form()] = False,
        delete_descendants: Annotated[bool, Form()] = False,
        is_shared: Annotated[bool, Form()] = False,
    ) -> Response:
        # 將讀取、父層驗證和修改放在同一筆 SQLite 交易中。
        user_id = require_user(engine, settings, request, write=True)
        level = LEVELS[kind]
        next_kind, next_id = kind, item_id
        saved_version = None
        removed_files: list[str] = []
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if action == Action.share and kind != Kind.notebook:
                raise HTTPException(404, "找不到這個筆記本。")
            owner_only = kind == Kind.notebook and action != Action.save
            item = get_item(db, kind, item_id, user_id, owner_only=owner_only)
            if action == Action.share:
                item.is_shared = is_shared
            elif action == Action.save:
                if isinstance(item, Note):
                    if version is None:
                        raise HTTPException(422, "缺少記事版本。請重新開啟記事。")
                    if item.version != version:
                        if request.headers.get("accept") == "application/json":
                            raise HTTPException(409, "這則記事已有新版。你的草稿仍在此分頁。")
                        return templates.TemplateResponse(
                            request=request, name="note_conflict.html", status_code=409,
                            context={"note_url": item_url(kind, item_id), "title": name, "draft": content},
                            headers={"Cache-Control": "no-store"},
                        )
                    item.version = saved_version = new_note_version()
                save_fields(item, name, content, content_format)
            elif action == Action.move:
                if level.parent is None:
                    move_notebook(db, item_id, direction, user_id)
                else:
                    if notebook_id is not None:
                        if kind != Kind.topic or target_id is not None:
                            raise HTTPException(422, "指定筆記本的欄位僅適用於移動主題。")
                        target_id = topic_list_for_book(db, notebook_id, user_id)
                    if target_id is None:
                        raise HTTPException(422, "請選擇移動目的地。")
                    get_item(db, level.parent, target_id, user_id)
                    source = containing_book(db, level.model, item_id, user_id)
                    destination = containing_book(db, LEVELS[level.parent].model, target_id, user_id)
                    if source.id != destination.id and (source.owner_id != user_id or destination.owner_id != user_id):
                        raise HTTPException(404, "找不到可移動的目的地。")
                    setattr(item, level.parent_key, target_id)
                    if isinstance(item, Note):
                        item.version = new_note_version()
            else:
                if not confirmed:
                    raise HTTPException(422, "請勾選確認後再刪除。")
                next_kind = level.parent
                next_id = getattr(item, level.parent_key) if next_kind else None
                removed_files = remove_item(db, kind, item, delete_descendants)
        for file_id in removed_files:
            remove_stored_attachment(uploads, file_id)
        if saved_version and request.headers.get("accept") == "application/json":
            return JSONResponse({"version": saved_version}, headers={"Cache-Control": "no-store"})
        return RedirectResponse(item_url(next_kind, next_id), status_code=303)

    return router
