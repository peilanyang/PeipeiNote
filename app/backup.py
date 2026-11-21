from argparse import ArgumentParser
from contextlib import closing
import logging
from os import link
from pathlib import Path
from shutil import copyfile, copyfileobj
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.attachments import FILE_ID
from app.auth import ROOT, Settings, require_user
from app.database import Attachment, User, make_engine

logger = logging.getLogger(__name__)


class BackupResponse(FileResponse):
    def __init__(self, directory: TemporaryDirectory) -> None:
        # 備份在私有暫存空間生成，完成後從磁碟串流。
        self.directory = directory
        super().__init__(
            Path(directory.name) / "backup.zip",
            media_type="application/zip", filename="PeipeiNote-backup.zip",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 關閉下載或傳送失敗也要清理私有暫存檔。
        # 禁止伺服器在 ASGI 回應結束後才延遲開啟暫存 ZIP。
        scope = {**scope, "extensions": {}}
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self.directory.cleanup)


def write_backup(engine: Engine, uploads: Path, directory: Path, *, from_web: bool = False) -> None:
    # 用短暫寫入保留鎖固定附件清單。檔案上傳與刪除原本也使用此鎖。
    snapshot = directory / "peipeinote.db"
    uploads.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".backup-", dir=uploads) as pinned:
        with engine.begin() as guard:
            guard.exec_driver_sql("BEGIN IMMEDIATE")
            # 第二帳號曾啟用時，即使後來停用也保留硬碟上的私人資料。
            if from_web and guard.scalar(select(User.id).where(User.id == 2)) is not None:
                raise HTTPException(403, "家庭資料的完整備份請使用本機管理指令。")
            names = list(guard.scalars(select(Attachment.storage_name).order_by(Attachment.id)))
            with closing(engine.raw_connection()) as source, closing(sqlite3.connect(snapshot)) as target:
                source.driver_connection.backup(target)
            for name in names:
                if not FILE_ID.fullmatch(name):
                    raise OSError("無效的附件記錄。")
                path = uploads / name
                if path.is_symlink() or not path.is_file():
                    raise OSError("備份中的必要附件遺失。")
                destination = Path(pinned) / name
                try:
                    link(path, destination)
                except OSError:
                    # 不支援硬連結的磁碟仍能備份，但複製時需保留寫入鎖。
                    copyfile(path, destination)

        # 只打包資料庫與已登記的附件。釋放寫入鎖後才壓縮或傳輸。
        with ZipFile(directory / "backup.zip", "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
            archive.write(snapshot, "peipeinote.db")
            archive.writestr("uploads/", b"")
            for name in names:
                archive.write(Path(pinned) / name, f"uploads/{name}")


def backup_routes(engine: Engine, settings: Settings, uploads: Path) -> APIRouter:
    # 以已登入、同源的 POST 啟動完整備份下載。
    router = APIRouter()

    @router.post("/backup")
    def download_backup(request: Request) -> FileResponse:
        # 先驗證請求，再使用私有暫存目錄建立下載。
        user_id = require_user(engine, settings, request, write=True)
        if user_id != 1 or settings.partner_enabled:
            raise HTTPException(403, "家庭資料的完整備份請使用本機管理指令。")
        directory = TemporaryDirectory(prefix="peipeinote-backup-")
        try:
            write_backup(engine, uploads, Path(directory.name), from_web=True)
            return BackupResponse(directory)
        except HTTPException:
            directory.cleanup()
            raise
        except (OSError, sqlite3.Error, SQLAlchemyError):
            directory.cleanup()
            logger.exception("PeipeiNote 完整備份失敗")
            raise HTTPException(503, "無法完整備份。請檢查可用空間與附件是否齊全。") from None

    return router


def main() -> None:
    # 完整備份由能直接存取資料庫的本機操作員執行。
    parser = ArgumentParser(description="將整個 SQLite 資料庫與附件備份到指定 ZIP。")
    parser.add_argument("destination", type=Path, help="新的備份 ZIP 檔案路徑")
    destination = parser.parse_args().destination
    database = ROOT / "peipeinote.db"
    if not database.is_file():
        parser.exit(1, "找不到本機資料庫。\n")
    engine = make_engine(database)
    created = False
    try:
        with TemporaryDirectory(prefix="peipeinote-backup-") as temporary:
            write_backup(engine, ROOT / "uploads", Path(temporary))
            with (Path(temporary) / "backup.zip").open("rb") as source, destination.open("xb") as output:
                created = True
                copyfileobj(source, output)
    except (OSError, sqlite3.Error, SQLAlchemyError):
        if created:
            destination.unlink(missing_ok=True)
        parser.exit(1, "備份失敗。請確認目的地不存在，且資料庫與附件均可讀取。\n")
    finally:
        engine.dispose()
    print(f"備份完成：{destination}")


if __name__ == "__main__":
    main()
