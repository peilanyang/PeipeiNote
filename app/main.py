from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import (
    COOKIE, ROOT, SESSION_SECONDS, Settings, authenticate, read_settings,
    require_same_origin, revoke_session, session_active,
)
from app.database import Base, make_engine


def page(title: str, content: str, status: int = 200) -> HTMLResponse:
    # 只接收程式內的固定文字，避免把輸入內容帶入 HTML。
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | PeipeiNote</title>
</head>
<body style="max-width: 32rem; margin: 3rem auto; padding: 0 1rem; font: 1rem system-ui">
  <main><h1>PeipeiNote</h1>{content}</main>
</body>
</html>""",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def create_app(settings: Settings, db_path: Path) -> FastAPI:
    # 將站台設定與 SQLite 路徑集中注入，測試可使用獨立資料庫。
    engine = make_engine(db_path)
    site = urlsplit(settings.origin)
    if (
        site.scheme not in {"http", "https"} or not site.hostname
        or site.username or site.password
        or settings.origin != f"{site.scheme}://{site.netloc}"
        or "*" in site.hostname
        or (site.scheme == "http" and site.hostname not in {"127.0.0.1", "localhost"})
    ):
        raise ValueError("APP_ORIGIN 必須是 HTTPS 站台或本機 HTTP 網址，網址尾端不要加斜線。")
    secure_cookie = site.scheme == "https"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 啟動時建立資料表，關閉時釋放資料庫連線。
        Base.metadata.create_all(engine)
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[site.hostname])

    @app.get("/")
    def home(request: Request) -> Response:
        # 首頁只顯示給已通過登入驗證的使用者。
        if not session_active(engine, settings, request.cookies.get(COOKIE)):
            return RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
        return page("首頁", """<p>已登入。你的筆記工作區已就緒。</p>
<form method="post" action="/logout"><button type="submit">登出</button></form>""")

    @app.get("/login")
    def login_page() -> HTMLResponse:
        # 只有完成本機設定才開放登入表單。
        if not settings.configured:
            return page("初始化帳號", "<p>請先執行帳號設定指令，再重新啟動 PeipeiNote。</p><p><code>.venv/Scripts/python.exe -m app.setup_account</code></p>", 503)
        return page("登入", """<h2>登入</h2>
<form method="post" action="/login">
  <p><label>帳號 <input name="username" autocomplete="username" maxlength="128" required></label></p>
  <p><label>密碼 <input type="password" name="password" autocomplete="current-password" maxlength="1024" required></label></p>
  <button type="submit">登入</button>
</form>""")

    @app.post("/login")
    def login(
        request: Request,
        username: Annotated[str, Form()] = "",
        password: Annotated[str | None, Form()] = None,
    ) -> Response:
        # 先驗證請求來源，再以目前連線 IP 進行登入檢查。
        require_same_origin(request, settings)
        if not settings.configured:
            return login_page()
        ip = request.client.host if request.client else "unknown"
        token, retry_after = authenticate(engine, settings, ip, username, password or "")
        if retry_after:
            response = page("暫停登入", "<p>此連線暫時鎖定，請稍後再試。</p><a href='/login'>回登入頁</a>", 429)
            response.headers["Retry-After"] = str(retry_after)
            return response
        if token is None:
            return page("登入失敗", "<p>帳號或密碼錯誤。</p><a href='/login'>回登入頁</a>", 401)
        revoke_session(engine, settings, request.cookies.get(COOKIE))
        response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
        response.set_cookie(
            COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=secure_cookie, samesite="lax"
        )
        return response

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        # 使用同源 POST 撤銷憑證並清除瀏覽器 Cookie。
        require_same_origin(request, settings)
        revoke_session(engine, settings, request.cookies.get(COOKIE))
        response = RedirectResponse("/login", status_code=303, headers={"Cache-Control": "no-store"})
        response.delete_cookie(COOKIE, secure=secure_cookie, httponly=True, samesite="lax")
        return response

    return app


app = create_app(read_settings(), ROOT / "peipeinote.db")
