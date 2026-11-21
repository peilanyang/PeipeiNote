from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.access import notebook_items, search_label, visible_to
from app.auth import Settings, require_user
from app.database import Note, Notebook, Tag, Template, note_tags
from app.richtext import NoteFormat, template_html
from app.search import note_text, search_rows
from app.workspace import CONTENT_LIMIT, MAX_ID, NAME_LIMIT, Kind, get_item, templates

LibraryKind = Literal["tags", "templates"]
MAX_QUERY = 160


def library_location(kind: str, **params: str | int | None) -> str:
    # 建立內部連結，所有查詢值依網址規則編碼。
    query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
    return f"/library/{kind}" + (f"?{query}" if query else "")


def library_routes(engine: Engine, settings: Settings) -> APIRouter:
    # 標籤、範本和搜尋入口皆要求登入。
    router = APIRouter(prefix="/library")

    @router.get("/search")
    def search_page(
        request: Request,
        q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
        book: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
        tag: Annotated[int | None, Query(gt=0, le=MAX_ID)] = None,
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        partial: bool = False,
    ) -> Response:
        # 查詢標題及可見內文，並提供可開啟原記事的分頁清單。
        user_id = require_user(engine, settings, request)
        query = note_text(q, "plain")
        with Session(engine) as db:
            db.connection().exec_driver_sql("BEGIN")
            if book:
                get_item(db, Kind.notebook, book, user_id)
            label = db.scalar(select(Tag).where(Tag.id == tag, search_label(user_id))) if tag is not None else None
            if tag is not None and label is None:
                raise HTTPException(404, "找不到這個標籤。")
            active = bool(query or label)
            rows, more = search_rows(db, query, book or None, tag, page, user_id) if active else ([], False)
            return templates.TemplateResponse(
                request=request, name="search_results.html" if partial else "library.html",
                context={
                    "mode": "search", "q": q, "book": book, "tag": label, "active": active, "rows": rows,
                    "books": list(db.scalars(notebook_items(Notebook, user_id).order_by(Notebook.name))),
                    "previous": library_location("search", q=q, book=book, tag=tag, page=page - 1) if page > 1 else "",
                    "next": library_location("search", q=q, book=book, tag=tag, page=page + 1) if more else "",
                }, headers={"Cache-Control": "no-store"},
            )

    @router.get("/{kind}")
    def manage(
        request: Request, kind: LibraryKind, selected: Annotated[int | None, Query(gt=0, le=MAX_ID)] = None,
    ) -> Response:
        # 清單與編輯表單共用頁面，富文字範本以淨化後的內容載入。
        user_id = require_user(engine, settings, request)
        model = Tag if kind == "tags" else Template
        with Session(engine) as db:
            db.connection().exec_driver_sql("BEGIN")
            visible = select(model).where(visible_to(model, user_id))
            item = db.scalar(visible.where(model.id == selected)) if selected is not None else None
            if selected is not None and item is None:
                raise HTTPException(404, "找不到選取的項目。")
            return templates.TemplateResponse(
                request=request, name="library.html",
                context={
                    "mode": kind, "items": list(db.scalars(visible.order_by(model.name, model.id))), "user_id": user_id,
                    "item": item, "editor_content_format": item.content_format if isinstance(item, Template) else "plain",
                    "editor_content": (
                        template_html(item.content) if item.content_format == "html" else item.content
                    ) if isinstance(item, Template) else "",
                }, headers={"Cache-Control": "no-store"},
            )

    @router.post("/{kind}")
    def change(
        request: Request, kind: LibraryKind,
        name: Annotated[str, Form(max_length=NAME_LIMIT)] = "",
        content: Annotated[str, Form(max_length=CONTENT_LIMIT)] = "",
        content_format: Annotated[NoteFormat, Form()] = "plain",
        record_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        action: Annotated[Literal["save", "delete"], Form()] = "save",
        note_id: Annotated[int | None, Form(gt=0, le=MAX_ID)] = None,
        is_shared: Annotated[bool | None, Form()] = None,
    ) -> RedirectResponse:
        # 用交易處理名稱檢查、刪除及記事快照，維持一致的資料狀態。
        user_id = require_user(engine, settings, request, write=True)
        model = Tag if kind == "tags" else Template
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            item = db.scalar(select(model).where(
                model.id == record_id, model.owner_id == user_id
            )) if record_id is not None else model(owner_id=user_id)
            if item is None:
                raise HTTPException(404, "找不到選取的項目。")
            if action == "delete":
                if record_id is None:
                    raise HTTPException(422, "請指定要刪除的項目。")
                db.delete(item)
                return RedirectResponse(library_location(kind), status_code=303)
            name = name.strip()
            if not name:
                raise HTTPException(422, "請輸入名稱。")
            if kind == "tags":
                existing = db.scalar(select(Tag.id).where(Tag.owner_id == user_id, Tag.name == name))
                if existing is not None and existing != record_id:
                    raise HTTPException(409, "這個標籤名稱已存在。")
            item.name = name
            if is_shared is not None:
                item.is_shared = is_shared
            if isinstance(item, Template):
                if note_id is not None:
                    note = get_item(db, Kind.note, note_id, user_id)
                    content, content_format = note.content, note.content_format
                item.content_format = content_format
                item.content = template_html(content) if content_format == "html" else content
            db.add(item)
            db.flush()
            return RedirectResponse(library_location(kind, selected=item.id), status_code=303)

    @router.post("/notes/{note_id}/tags")
    def update_note_tag(
        request: Request, note_id: Annotated[int, Path(gt=0, le=MAX_ID)],
        tag_id: Annotated[int, Form(gt=0, le=MAX_ID)],
        action: Annotated[Literal["add", "remove"], Form()],
    ) -> RedirectResponse:
        # 每個連結獨立寫入，無數量上限，重複加入也不會產生重複列。
        user_id = require_user(engine, settings, request, write=True)
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            get_item(db, Kind.note, note_id, user_id)
            tag_access = visible_to(Tag, user_id)
            if action == "remove":
                # 共用記事的編輯者可以移除已附上的標籤，但不能附上別人的私人標籤。
                linked = select(note_tags.c.tag_id).where(note_tags.c.note_id == note_id)
                tag_access = or_(tag_access, Tag.id.in_(linked))
            if db.scalar(select(Tag.id).where(Tag.id == tag_id, tag_access)) is None:
                raise HTTPException(404, "找不到這個標籤。")
            if action == "add":
                db.execute(insert(note_tags).values(note_id=note_id, tag_id=tag_id).on_conflict_do_nothing())
            else:
                db.execute(delete(note_tags).where(note_tags.c.note_id == note_id, note_tags.c.tag_id == tag_id))
        return RedirectResponse(f"/?kind=note&item_id={note_id}", status_code=303)

    return router
