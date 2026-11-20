from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.auth import COOKIE, Settings, require_same_origin, session_active
from app.database import Note, Notebook, Tag, Template, note_tags
from app.richtext import NoteFormat, editor_source, template_html
from app.search import note_text, search_rows
from app.workspace import CONTENT_LIMIT, MAX_ID, NAME_LIMIT, templates

LibraryKind = Literal["tags", "templates"]
MAX_QUERY = 160


def library_location(kind: str, **params: str | int | None) -> str:
    # 建立內部連結，所有查詢值依網址規則編碼。
    query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
    return f"/library/{kind}" + (f"?{query}" if query else "")


def library_routes(engine: Engine, settings: Settings) -> APIRouter:
    # 標籤、範本和搜尋入口皆要求登入。
    router = APIRouter(prefix="/library")

    def authorize(request: Request, write: bool = False) -> None:
        # 變更資料時，一併驗證請求來源。
        if not session_active(engine, settings, request.cookies.get(COOKIE)):
            raise HTTPException(401, "請先登入 PeipeiNote。")
        if write:
            require_same_origin(request, settings)

    @router.get("/search")
    def search_page(
        request: Request,
        q: Annotated[str, Query(max_length=MAX_QUERY)] = "",
        book: Annotated[int, Query(ge=0, le=MAX_ID)] = 0,
        tag: Annotated[int | None, Query(gt=0, le=MAX_ID)] = None,
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        partial: bool = False,
    ) -> Response:
        # 查詢標題及可見內文，並提供可開啟原筆記的分頁清單。
        authorize(request)
        query = note_text(q, "plain")
        with Session(engine) as db:
            label = db.get(Tag, tag) if tag is not None else None
            if tag is not None and label is None:
                raise HTTPException(404, "找不到這個標籤。")
            active = bool(query or label)
            rows, more = search_rows(db, query, book or None, tag, page) if active else ([], False)
            return templates.TemplateResponse(
                request=request, name="search_results.html" if partial else "library.html",
                context={
                    "mode": "search", "q": q, "book": book, "tag": label, "active": active, "rows": rows,
                    "books": list(db.scalars(select(Notebook).order_by(Notebook.name))),
                    "previous": library_location("search", q=q, book=book, tag=tag, page=page - 1) if page > 1 else "",
                    "next": library_location("search", q=q, book=book, tag=tag, page=page + 1) if more else "",
                }, headers={"Cache-Control": "no-store"},
            )

    @router.get("/{kind}")
    def manage(
        request: Request, kind: LibraryKind, selected: Annotated[int | None, Query(gt=0, le=MAX_ID)] = None,
    ) -> Response:
        # 清單與編輯表單共用頁面，富文字範本以淨化後的內容載入。
        authorize(request)
        model = Tag if kind == "tags" else Template
        with Session(engine) as db:
            item = db.get(model, selected) if selected is not None else None
            if selected is not None and item is None:
                raise HTTPException(404, "找不到選取的項目。")
            return templates.TemplateResponse(
                request=request, name="library.html",
                context={
                    "mode": kind, "items": list(db.scalars(select(model).order_by(model.name, model.id))),
                    "item": item, "editor_content_format": item.content_format if isinstance(item, Template) else "plain",
                    "editor_content": editor_source(item.content, item.content_format) if isinstance(item, Template) else "",
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
    ) -> RedirectResponse:
        # 用交易處理名稱檢查、刪除及筆記快照，維持一致的資料狀態。
        authorize(request, write=True)
        model = Tag if kind == "tags" else Template
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            item = db.get(model, record_id) if record_id is not None else model()
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
                existing = db.scalar(select(Tag.id).where(Tag.name == name))
                if existing is not None and existing != record_id:
                    raise HTTPException(409, "這個標籤名稱已存在。")
            item.name = name
            if isinstance(item, Template):
                if note_id is not None:
                    note = db.get(Note, note_id)
                    if note is None:
                        raise HTTPException(404, "找不到這則筆記。")
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
        authorize(request, write=True)
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if db.get(Note, note_id) is None or db.get(Tag, tag_id) is None:
                raise HTTPException(404, "找不到筆記或標籤。")
            if action == "add":
                db.execute(insert(note_tags).values(note_id=note_id, tag_id=tag_id).on_conflict_do_nothing())
            else:
                db.execute(delete(note_tags).where(note_tags.c.note_id == note_id, note_tags.c.tag_id == tag_id))
        return RedirectResponse(f"/?kind=note&item_id={note_id}", status_code=303)

    return router
