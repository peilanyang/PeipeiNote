from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.access import notebook_items, search_label, visible_to, get_visible_tag_ids
from app.auth import Settings, require_user
from app.database import Note, Notebook, Tag, Template, note_tags
from app.richtext import NoteFormat, template_html
from app.search import note_text, search_rows, PAGE_SIZE
from app.workspace import CONTENT_LIMIT, MAX_ID, NAME_LIMIT, Kind, get_item, templates

LibraryKind = Literal["tags", "templates"]
MAX_QUERY = 160


def library_location(kind: str, **params: str | int | None) -> str:
    # 建立內部連結，所有查詢值依網址規則編碼。
    query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
    return f"/library/{kind}" + (f"?{query}" if query else "")


def fetch_tag_notes(db: Session, tag_id: int, user_id: int, page: int) -> tuple[list[dict], bool]:
    """
    取得指定標籤下的記事列表（分頁），只回傳使用者可見的記事。
    回傳: (notes_list, has_more)
    """
    offset = (page - 1) * PAGE_SIZE
    limit = PAGE_SIZE + 1  # 多取一筆判斷是否還有下一頁
    # 使用與 search_rows 相同的查詢邏輯，但不做全文搜尋，只按標籤過濾；
    # 列表只需要標題與位置欄位，不搬運記事全文。
    stmt = text("""
        SELECT n.id, n.updated_at, n.title,
               b.name AS notebook, t.name AS topic
        FROM notes AS n
        JOIN note_tags AS nt ON nt.note_id = n.id
        JOIN topics AS t ON n.topic_id = t.id
        JOIN topic_lists AS l ON t.topic_list_id = l.id
        JOIN notebooks AS b ON l.notebook_id = b.id
        WHERE nt.tag_id = :tag_id
          AND (b.owner_id = :user_id OR b.is_shared = 1)
        ORDER BY n.id DESC
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(stmt, {"tag_id": tag_id, "user_id": user_id, "limit": limit, "offset": offset}).mappings().all()
    notes = []
    for row in rows[:PAGE_SIZE]:
        # SQLite 回傳 updated_at 為字串，需轉為 datetime
        updated_at = row["updated_at"]
        if isinstance(updated_at, str):
            try:
                updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            except ValueError:
                updated_at = None
        notes.append({
            "id": row["id"],
            "title": row["title"],
            "notebook": row["notebook"],
            "topic": row["topic"],
            "updated_at": updated_at,
        })
    return notes, len(rows) > PAGE_SIZE


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
            # 使用快取的可見標籤 ID，避免重複執行 search_label 子查詢
            visible_tag_ids = get_visible_tag_ids(db, user_id)
            label = (
                db.scalar(select(Tag).where(Tag.id == tag, Tag.id.in_(visible_tag_ids)))
                if tag is not None else None
            )
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
        request: Request,
        kind: LibraryKind,
        selected: Annotated[int | None, Query(gt=0, le=MAX_ID)] = None,
        tag_page: Annotated[int, Query(ge=1, le=100000)] = 1,
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

            # 標籤模式且選取了標籤：查詢該標籤下的記事列表
            tag_notes = []
            tag_notes_prev = ""
            tag_notes_next = ""
            if kind == "tags" and selected is not None:
                # 再次確認標籤對當前用戶可見
                visible_tag_ids = get_visible_tag_ids(db, user_id)
                if selected in visible_tag_ids:
                    tag_notes, has_more = fetch_tag_notes(db, selected, user_id, tag_page)
                    if tag_page > 1:
                        tag_notes_prev = library_location("tags", selected=selected, tag_page=tag_page - 1)
                    if has_more:
                        tag_notes_next = library_location("tags", selected=selected, tag_page=tag_page + 1)

            return templates.TemplateResponse(
                request=request, name="library.html",
                context={
                    "mode": kind,
                    "items": list(db.scalars(visible.order_by(model.name, model.id))),
                    "user_id": user_id,
                    "item": item,
                    "editor_content_format": item.content_format if isinstance(item, Template) else "plain",
                    "editor_content": (
                        template_html(item.content) if item.content_format == "html" else item.content
                    ) if isinstance(item, Template) else "",
                    "tag_notes": tag_notes,
                    "tag_notes_prev": tag_notes_prev,
                    "tag_notes_next": tag_notes_next,
                    "tag_page": tag_page,
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
            # 使用快取的可見標籤 ID
            visible_tag_ids = get_visible_tag_ids(db, user_id)
            if action == "remove":
                # 移除時：允許移除已連結在該記事上的標籤（即使非可見），以及可見標籤
                linked_tag_ids = set(db.scalars(select(note_tags.c.tag_id).where(note_tags.c.note_id == note_id)).all())
                allowed_tag_ids = visible_tag_ids | linked_tag_ids
            else:
                # 新增時：只允許加上可見標籤
                allowed_tag_ids = visible_tag_ids
            if tag_id not in allowed_tag_ids:
                raise HTTPException(404, "找不到這個標籤。")
            if action == "add":
                db.execute(insert(note_tags).values(note_id=note_id, tag_id=tag_id).on_conflict_do_nothing())
            else:
                db.execute(delete(note_tags).where(note_tags.c.note_id == note_id, note_tags.c.tag_id == tag_id))
        return RedirectResponse(f"/?kind=note&item_id={note_id}", status_code=303)

    return router
