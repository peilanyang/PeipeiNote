import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from dotenv import dotenv_values
from fastapi import HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.database import LoginAttempt, LoginSession

ROOT = Path(__file__).resolve().parents[1]
COOKIE = "peipeinote_session"
COOKIE_SECONDS = 365 * 24 * 60 * 60
LOCK_SECONDS = 15 * 60
FAILURE_LIMIT = 5
PASSWORD_CHECK_SLOTS = BoundedSemaphore(4)
hasher = PasswordHasher()
logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class Settings:
    username: str = ""
    password_hash: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    origin: str = "http://127.0.0.1:8000"

    partner_username: str = ""
    partner_password_hash: str = field(default="", repr=False)

    @property
    def partner_enabled(self) -> bool:
        # 伴侶帳號可在初次設定後加入。
        return bool(self.partner_username)

    @staticmethod
    def valid_hash(password_hash: str) -> bool:
        # 接受 Argon2id 雜湊。
        try:
            return extract_parameters(password_hash).type == Type.ID
        except (InvalidHashError, ValueError):
            return False

    @property
    def configured(self) -> bool:
        # 任一帳號設定不完整時關閉登入，直到本機修正。
        primary = bool(self.username and len(self.secret) >= 32 and self.valid_hash(self.password_hash))
        if not self.partner_username and not self.partner_password_hash:
            return primary
        return bool(
            primary and self.partner_username and self.partner_username != self.username
            and self.valid_hash(self.partner_password_hash)
        )


def read_settings() -> Settings:
    # 只讀專案根目錄的設定，不展開密碼雜湊中的美元符號。
    values = dotenv_values(ROOT / ".env", interpolate=False)
    return Settings(
        username=values.get("ADMIN_USERNAME") or "",
        password_hash=values.get("ADMIN_PASSWORD_HASH") or "",
        secret=values.get("SECRET_KEY") or "",
        origin=values.get("APP_ORIGIN") or Settings.origin,
        partner_username=values.get("PARTNER_USERNAME") or "",
        partner_password_hash=values.get("PARTNER_PASSWORD_HASH") or "",
    )


def require_same_origin(request: Request, settings: Settings) -> None:
    # 比對固定的站台來源，僅在缺少 Origin 時才採用 Referer。
    origin = request.headers.get("origin")
    if origin is None:
        try:
            referer = urlsplit(request.headers.get("referer", ""))
        except ValueError:
            raise HTTPException(403, "請從 PeipeiNote 頁面操作。") from None
        origin = f"{referer.scheme}://{referer.netloc}"
    if origin != settings.origin:
        raise HTTPException(403, "請從 PeipeiNote 頁面操作。")


def session_digest(settings: Settings, token: str) -> str:
    # 換帳號、密碼或金鑰時，舊的工作階段摘要立即失效。
    accounts = json.dumps([settings.username, settings.password_hash,
                           settings.partner_username, settings.partner_password_hash])
    key = hmac.digest(settings.secret.encode(), accounts.encode(), "sha256")
    return hmac.digest(key, token.encode(), "sha256").hex()


def authenticate(
    engine: Engine, settings: Settings, ip: str, username: str, password: str
) -> tuple[str | None, int]:
    # 先用短查詢拒絕已鎖定的 IP，避免未必要的密碼雜湊與寫入鎖。
    with Session(engine) as db:
        now = int(time.time())
        attempt = db.get(LoginAttempt, ip)
        if attempt and attempt.locked_until > now:
            return None, attempt.locked_until - now

    # 每個程序只允許少量並行密碼驗證，雜湊期間不占用 SQLite 寫入鎖。
    if not PASSWORD_CHECK_SLOTS.acquire(blocking=False):
        return None, 1
    try:
        partner = settings.partner_enabled and hmac.compare_digest(
            settings.partner_username.encode(), username.encode()
        )
        user_id = 2 if partner else 1
        password_hash = settings.partner_password_hash if partner else settings.password_hash
        try:
            valid_password = len(password) <= 1024 and hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            valid_password = False
    finally:
        PASSWORD_CHECK_SLOTS.release()
    valid_user = partner or hmac.compare_digest(settings.username.encode(), username.encode())

    # 寫入時重新讀取鎖定狀態，同時到達的請求也不能略過第五次失敗。
    with Session(engine) as db, db.begin():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        now = int(time.time())
        attempt = db.get(LoginAttempt, ip)
        if attempt and attempt.locked_until > now:
            return None, attempt.locked_until - now
        if attempt and attempt.locked_until:
            attempt.failures = 0
            attempt.locked_until = 0

        if valid_password and valid_user:
            if attempt:
                db.delete(attempt)
            db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
            token = secrets.token_urlsafe(32)
            db.add(LoginSession(
                digest=session_digest(settings, token),
                expires_at=now + COOKIE_SECONDS,
                user_id=user_id,
            ))
            return token, 0

        if attempt is None:
            attempt = LoginAttempt(ip=ip, failures=0, locked_until=0)
            db.add(attempt)
        attempt.failures += 1
        if attempt.failures >= FAILURE_LIMIT:
            attempt.locked_until = now + LOCK_SECONDS
            logger.warning("PeipeiNote 登入失敗達上限，已鎖定 IP %s 15 分鐘", ip)
            return None, LOCK_SECONDS
        return None, 0


def session_user(engine: Engine, settings: Settings, token: str | None) -> int | None:
    # 查出工作階段綁定的帳號。
    if not token or not settings.configured:
        return None
    with Session(engine) as db:
        record = db.get(LoginSession, session_digest(settings, token))
        if record is not None and record.expires_at > int(time.time()):
            if record.user_id == 1 or (record.user_id == 2 and settings.partner_enabled):
                return record.user_id
    return None


def require_user(engine: Engine, settings: Settings, request: Request, *, write: bool = False) -> int:
    # 將憑證解析為帳號。寫入另外驗證請求來源。
    user_id = session_user(engine, settings, request.cookies.get(COOKIE))
    if user_id is None:
        raise HTTPException(401, "請先登入 PeipeiNote。")
    if write:
        require_same_origin(request, settings)
    return user_id


def revoke_session(engine: Engine, settings: Settings, token: str | None) -> None:
    # 登出時撤銷目前憑證，連已複製的 Cookie 也不能再使用。
    if token:
        with Session(engine) as db, db.begin():
            db.execute(delete(LoginSession).where(LoginSession.digest == session_digest(settings, token)))
