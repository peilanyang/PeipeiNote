from dataclasses import dataclass
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Path, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.auth import COOKIE, ROOT, Settings, require_same_origin, session_active
from app.database import Attachment, Note, Notebook, NotebookPosition, Tag, Template, Topic, TopicList, note_tags

from app.richtext import NoteFormat, clean_html, editor_source, template_html

MAX_ID = 2**63 - 1
NAME_LIMIT = 200
CONTENT_LIMIT = 100_000
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


class Kind(str, Enum):
    notebook = "notebook"
    section = "section"
    topic = "topic"
    note = "note"


class Action(str, Enum):
    save = "save"
    move = "move"
    delete = "delete"


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
    Kind.note: Level(Note, "筆記", Kind.topic, "topic_id"),
}
CHILD = {level.parent: kind for kind, level in LEVELS.items()}


def item_url(kind: Kind | None, item_id: int | None) -> str:
    # 一律建立站內連結，避免把表單資料用於重新導向。
    return f"/?kind={kind.value}&item_id={item_id}" if kind is not None else "/"


def get_item(db: Session, kind: Kind, item_id: int) -> Item:
    # 依允許的層級和主鍵查詢，找不到時回傳 404。
    item = db.get(LEVELS[kind].model, item_id)
    if item is None:
        raise HTTPException(404, f"找不到這個{LEVELS[kind].label}。")
    return item


def notebook_order(db: Session) -> list[Notebook]:
    # 已排序的筆記本排在前面，新筆記本依建立順序接在後面。
    positions = dict(db.execute(select(NotebookPosition.notebook_id, NotebookPosition.position)).tuples().all())
    return sorted(db.scalars(select(Notebook)), key=lambda book: (book.id not in positions, positions.get(book.id, book.id)))


def title_path(item: Item, kind: Kind, items: dict[Kind, dict[int, Item]]) -> str:
    # 移動清單顯示完整路徑，以區分同名主題。
    names = [item.title if isinstance(item, Note) else item.name]
    while LEVELS[kind].parent is not None:
        level = LEVELS[kind]
        parent = level.parent
        item = items[parent][getattr(item, level.parent_key)]
        names.append(item.name)
        kind = parent
    return " / ".join(reversed(names))


def render_workspace(request: Request, engine: Engine, kind: Kind | None, item_id: int | None) -> Response:
    # 左欄讀取階層，中欄只查詢目前主題的筆記，右欄載入選取的項目。
    with Session(engine) as db, db.begin():
        db.connection().exec_driver_sql("BEGIN")
        selection = get_item(db, kind, item_id) if kind is not None and item_id is not None else None
        books = notebook_order(db)
        sections = list(db.scalars(select(TopicList).order_by(TopicList.id)))
        topics = list(db.scalars(select(Topic).order_by(Topic.id)))
        items = {
            Kind.notebook: {row.id: row for row in books},
            Kind.section: {row.id: row for row in sections},
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
        notes = list(db.scalars(select(Note).where(Note.topic_id == topic_id).order_by(Note.id))) if topic_id else []
        parent = LEVELS[kind].parent if kind is not None else None
        destinations = [
            (row.id, title_path(row, parent, items))
            for row in items[parent].values()
            if row.id != active.get(parent.value)
        ] if parent is not None else []
        child = CHILD.get(kind)
        tags = list(db.scalars(select(Tag).order_by(Tag.name)))
        assigned_tag_ids = set(db.scalars(select(note_tags.c.tag_id).where(
            note_tags.c.note_id == selection.id
        ))) if isinstance(selection, Note) else set()
        return templates.TemplateResponse(
            request=request,
            name="workspace.html",
            context={
                "books": books, "sections_by_book": group_by(sections, "notebook_id"),
                "topics_by_section": group_by(topics, "topic_list_id"), "notes": notes,
                "active": active, "current_topic": items[Kind.topic].get(topic_id),
                "selection": selection, "kind": kind.value if kind else None,
                "attachments": list(db.scalars(select(Attachment).where(
                    Attachment.note_id == selection.id
                ).order_by(Attachment.id))) if isinstance(selection, Note) else [],
                "tags": tags, "assigned_tag_ids": assigned_tag_ids,
                "note_templates": db.execute(select(Template.id, Template.name).order_by(Template.name)).all() if topic_id else [],
                "editor_content_format": selection.content_format if isinstance(selection, Note) else "plain",
                "editor_content": editor_source(selection.content, selection.content_format)
                if isinstance(selection, Note) else "",
                "label": LEVELS[kind].label if kind else None,
                "child_kind": child.value if child else None, "child_label": LEVELS[child].label if child else None,
                "destinations": destinations,
                "can_delete": selection is not None and not has_children(db, kind, selection.id),
                "can_raise": bool(books and selection and kind == Kind.notebook and books[0].id != selection.id),
                "can_lower": bool(books and selection and kind == Kind.notebook and books[-1].id != selection.id),
            },
            headers={"Cache-Control": "no-store"},
        )


def group_by(rows: list[TopicList] | list[Topic], key: str) -> dict[int, list[TopicList | Topic]]:
    # 將同一父層底下的項目聚在一起，供樹狀導覽使用。
    grouped: dict[int, list[TopicList | Topic]] = {}
    for row in rows:
        grouped.setdefault(getattr(row, key), []).append(row)
    return grouped


def save_fields(item: Item, name: str, content: str, content_format: NoteFormat = "plain") -> None:
    # 純文字原樣儲存，富文字先經過 HTML 允許清單淨化。
    name = name.strip()
    if not 1 <= len(name) <= NAME_LIMIT:
        raise HTTPException(422, "名稱須為 1 至 200 個字元。")
    if isinstance(item, Note):
        if len(content) > CONTENT_LIMIT:
            raise HTTPException(422, "筆記超過 100,000 個字元。")
        item.title = name
        item.content = clean_html(content) if content_format == "html" else content
        item.content_format = content_format
    else:
        item.name = name


def move_notebook(db: Session, item_id: int, direction: int) -> None:
    # 筆記本位於最上層，移動操作調整清單順序。
    ordered = [book.id for book in notebook_order(db)]
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
    if child_kind is not None:
        child = LEVELS[child_kind]
        model = child.model
        existing = db.scalar(select(model.id).where(getattr(model, child.parent_key) == item_id).limit(1))
    else:
        existing = db.scalar(select(Attachment.id).where(Attachment.note_id == item_id).limit(1))
    return existing is not None


def remove_item(db: Session, kind: Kind, item: Item) -> None:
    # 重新檢查資料庫，避免使用者送出過期表單而刪除父層。
    if has_children(db, kind, item.id):
        raise HTTPException(409, "此項目仍有下層資料。請先移動或清空後再刪除。")
    db.delete(item)


def workspace_actions(engine: Engine, settings: Settings) -> APIRouter:
    # 所有資料變更共用登入驗證與同源檢查。
    router = APIRouter(prefix="/workspace")

    def authorized(request: Request) -> None:
        # 未登入或從其他站台送出的表單都不得修改資料。
        if not session_active(engine, settings, request.cookies.get(COOKIE)):
            raise HTTPException(401, "請先登入 PeipeiNote。")
        require_same_origin(request, settings)

    @router.post("/{kind}")
    def create(
        request: Request,
        kind: Kind,
        name: Annotated[str, Form()],
        parent_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        template_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
    ) -> RedirectResponse:
        # 建立項目前確認父層存在，並在同一交易中寫入。
        authorized(request)
        level = LEVELS[kind]
        item = level.model()
        save_fields(item, name, "")
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if level.parent is not None:
                if parent_id is None:
                    raise HTTPException(422, "請先選擇上層項目。")
                get_item(db, level.parent, parent_id)
                setattr(item, level.parent_key, parent_id)
            elif parent_id is not None:
                raise HTTPException(422, "筆記本位於最上層。")
            if template_id is not None:
                if not isinstance(item, Note):
                    raise HTTPException(422, "範本只能套用至筆記。")
                template = db.get(Template, template_id)
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
        target_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        direction: Annotated[int, Form(ge=-1, le=1)] = 0,
        confirmed: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        # 將讀取、父層驗證和修改放在同一筆 SQLite 交易中。
        authorized(request)
        level = LEVELS[kind]
        next_kind, next_id = kind, item_id
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            item = get_item(db, kind, item_id)
            if action == Action.save:
                save_fields(item, name, content, content_format)
            elif action == Action.move:
                if level.parent is None:
                    move_notebook(db, item_id, direction)
                else:
                    if target_id is None:
                        raise HTTPException(422, "請選擇移動目的地。")
                    get_item(db, level.parent, target_id)
                    setattr(item, level.parent_key, target_id)
            else:
                if not confirmed:
                    raise HTTPException(422, "請勾選確認後再刪除。")
                next_kind = level.parent
                next_id = getattr(item, level.parent_key) if next_kind else None
                remove_item(db, kind, item)
        return RedirectResponse(item_url(next_kind, next_id), status_code=303)

    return router
