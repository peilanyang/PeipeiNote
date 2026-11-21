import io
import logging
import re
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.access import notebook_items
from app.auth import Settings, require_user
from app.database import Attachment, Note

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
IMAGE_TYPES = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain", ".zip": "application/zip",
}
FILE_ID = re.compile(r"[0-9a-f]{32}\.(?:png|jpe?g|gif|webp|pdf|docx|txt|zip)")
logger = logging.getLogger(__name__)


def valid_payload(data: bytearray, suffix: str) -> bool:
    # 依允許的副檔名檢查實際格式，ZIP 與 DOCX 不解壓內容。
    try:
        if suffix in IMAGE_TYPES:
            with Image.open(io.BytesIO(data), formats=[IMAGE_TYPES[suffix]]) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return False
                image.verify()
            with Image.open(io.BytesIO(data), formats=[IMAGE_TYPES[suffix]]) as image:
                image.load()
            return True
        if suffix == ".pdf":
            if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)(?:\r\n|\r|\n)", data):
                return False
            tail = data.rstrip()[-4096:]
            pointer = re.search(rb"(?:^|[\r\n])startxref\s+([0-9]{1,10})\s+%%EOF$", tail)
            if pointer is None:
                return False
            offset = int(pointer[1])
            return bool(re.match(rb"(?:xref\s|[0-9]+\s+[0-9]+\s+obj\b)", data[offset:offset + 48]))
        if suffix == ".txt":
            decoded = data.decode("utf-8-sig")
            return not any(ord(char) < 32 and char not in "\r\n\t\f" for char in decoded)
        if suffix not in {".zip", ".docx"} or data[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
            return False
        with ZipFile(io.BytesIO(data)) as archive:
            if suffix == ".zip":
                return True
            return {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}.issubset(archive.namelist())
    except (OSError, ValueError, SyntaxError, BadZipFile, Image.DecompressionBombError):
        return False


def store_attachment(
    engine: Engine, uploads: Path, user_id: int, note_id: int, filename: str, data: bytearray,
) -> dict[str, str]:
    # 附件檔案使用 UUID，交易失敗時清理已寫入的檔案。
    name = re.split(r"[/\\]", filename)[-1]
    suffix = Path(name).suffix.lower()
    if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise HTTPException(422, "檔名不合法或過長。")
    if suffix not in MEDIA_TYPES or not valid_payload(data, suffix):
        raise HTTPException(415, "檔案格式不支援，或內容與副檔名不符。")
    file_id = uuid4().hex + suffix
    path = uploads / file_id
    created = False
    try:
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if db.scalar(notebook_items(Note, user_id).where(Note.id == note_id)) is None:
                raise HTTPException(404, "找不到這則筆記。")
            uploads.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                created = True
                output.write(data)
            db.add(Attachment(
                note_id=note_id, original_name=name, storage_name=file_id,
                media_type=MEDIA_TYPES[suffix], size_bytes=len(data),
            ))
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return {"id": file_id, "name": name, "url": f"/api/files/{file_id}", "media_type": MEDIA_TYPES[suffix]}


def lookup_attachment(db: Session, file_id: str, user_id: int) -> Attachment:
    # 只接受系統產生的檔案編號，拒絕路徑及其他字串。
    if FILE_ID.fullmatch(file_id):
        file = db.scalar(notebook_items(Attachment, user_id).where(Attachment.storage_name == file_id))
        if file is not None:
            return file
    raise HTTPException(404, "找不到這個附件。")


def attachment_routes(engine: Engine, settings: Settings, uploads: Path) -> APIRouter:
    # 附件端點共用應用程式既有的登入和同源驗證。
    router = APIRouter()

    @router.post("/api/notes/{note_id}/files", status_code=201)
    async def upload(request: Request, note_id: int) -> dict[str, str]:
        # 驗證後才以串流讀取，超過單檔上限即拒絕。
        user_id = await run_in_threadpool(require_user, engine, settings, request, write=True)
        if not 0 < note_id < 2**63:
            raise HTTPException(404, "找不到這則筆記。")
        # 先查權限再接收資料，真正寫檔前在交易中重查。
        def check_note() -> None:
            with Session(engine) as db:
                if db.scalar(notebook_items(Note, user_id).where(Note.id == note_id)) is None:
                    raise HTTPException(404, "找不到這則筆記。")

        await run_in_threadpool(check_note)
        encoded_name = request.headers.get("x-filename", "")
        if not encoded_name or len(encoded_name) > 3200:
            raise HTTPException(422, "缺少有效檔名。")
        try:
            filename = unquote(encoded_name, errors="strict")
        except UnicodeError:
            raise HTTPException(422, "檔名編碼不合法。") from None
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_FILE_BYTES:
                raise HTTPException(413, "單檔限 20 MB。")
            data.extend(chunk)
        if not data:
            raise HTTPException(422, "檔案是空的。")
        return await run_in_threadpool(store_attachment, engine, uploads, user_id, note_id, filename, data)

    @router.get("/api/files/{file_id}")
    def download(request: Request, file_id: str, download: bool = False) -> FileResponse:
        # 圖片可內嵌，其餘格式以附件下載，保留安全快取及內容類型標頭。
        user_id = require_user(engine, settings, request)
        with Session(engine) as db:
            file = lookup_attachment(db, file_id, user_id)
            path = uploads / file.storage_name
            if not path.is_file():
                raise HTTPException(404, "附件檔案已遺失。")
            media_type = MEDIA_TYPES[Path(file_id).suffix]
            return FileResponse(
                path, media_type=media_type, filename=file.original_name,
                content_disposition_type="inline" if media_type.startswith("image/") and not download else "attachment",
                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
            )

    @router.post("/api/files/{file_id}/delete")
    def delete_attachment(request: Request, file_id: str) -> RedirectResponse:
        # 內嵌圖片要先從正文移除並儲存，避免留下失效的插圖。
        user_id = require_user(engine, settings, request, write=True)
        with Session(engine) as db, db.begin():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            file = lookup_attachment(db, file_id, user_id)
            note = db.scalar(notebook_items(Note, user_id).where(Note.id == file.note_id))
            note_id = file.note_id
            if note and note.content_format == "html" and f'src="/api/files/{file_id}"' in note.content:
                raise HTTPException(409, "請先從正文移除這張圖片並儲存筆記。")
            db.delete(file)
        try:
            (uploads / file_id).unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove detached upload %s", file_id)
        return RedirectResponse(f"/?kind=note&item_id={note_id}", status_code=303)

    return router
