from collections.abc import Iterator
from pathlib import Path
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app

ORIGIN = "https://peipeinote.test"


@pytest.fixture
def credentials() -> dict[str, str]:
    # 每次產生測試用密碼，避免在版本庫留下固定憑證。
    return {"username": "owner", "password": secrets.token_urlsafe(24)}


@pytest.fixture
def client(tmp_path: Path, credentials: dict[str, str]) -> Iterator[TestClient]:
    # 以臨時 SQLite 與測試站台啟動完整應用程式。
    settings = auth.Settings(
        username=credentials["username"], password_hash=auth.hasher.hash(credentials["password"]),
        secret=secrets.token_urlsafe(48), origin=ORIGIN,
    )
    with TestClient(create_app(settings, tmp_path / "test.db"), base_url=ORIGIN) as browser:
        yield browser


def test_login_logout_and_cookie(client: TestClient, credentials: dict[str, str]) -> None:
    # 驗證登入表單、Cookie 安全屬性、登入後首頁與伺服器端登出。
    blocked = client.get("/", follow_redirects=False)
    assert (blocked.status_code, blocked.headers["location"]) == (303, "/login")
    assert "已登入" not in blocked.text
    assert 'name="password"' in client.get("/login").text
    response = client.post("/login", data=credentials, headers={"Origin": ORIGIN}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", f"max-age={auth.COOKIE_SECONDS}"))
    token = client.cookies[auth.COOKIE]
    assert "已登入" in client.get("/").text
    assert client.post("/logout", headers={"Origin": ORIGIN}, follow_redirects=False).status_code == 303
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_lockout(client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch,
                 caplog: pytest.LogCaptureFixture) -> None:
    # 同一 IP 第五次失敗立即鎖定，15 分鐘後才接受正確密碼。
    clock = SimpleNamespace(now=10_000)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock.now))
    headers = {"Origin": ORIGIN}
    invalid = {**credentials, "password": secrets.token_urlsafe(24)}
    for _ in range(4):
        assert client.post("/login", data=invalid, headers=headers).status_code == 401
    locked = client.post("/login", data=invalid, headers=headers)
    assert locked.status_code == 429
    assert locked.headers["retry-after"] == "900"
    assert "已鎖定 IP" in caplog.text
    clock.now += 899
    assert client.post("/login", data=credentials, headers=headers).headers["retry-after"] == "1"
    clock.now += 1
    assert client.post("/login", data=credentials, headers=headers, follow_redirects=False).status_code == 303


def test_csrf_and_invalid_session(client: TestClient, credentials: dict[str, str]) -> None:
    # 跨站或缺少來源時拒絕變更登入狀態，無效 Cookie 無法讀取首頁。
    assert client.post("/login", data=credentials, headers={"Origin": "https://elsewhere.test"}).status_code == 403
    assert client.post("/login", data=credentials).status_code == 403
    assert client.post("/logout", headers={"Origin": "null"}).status_code == 403
    client.cookies.set(auth.COOKIE, secrets.token_urlsafe(32))
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_survives_past_original_timeout(client: TestClient, credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # 模擬超過原本的 8 小時，確認登入仍有效。
    clock = SimpleNamespace(now=10_000)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: clock.now))
    client.post("/login", data=credentials, headers={"Origin": ORIGIN}).raise_for_status()
    assert "已登入" in client.get("/").text
    clock.now += 30 * 24 * 60 * 60
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "已登入" in response.text
    assert f"Max-Age={auth.COOKIE_SECONDS}" in response.headers["set-cookie"]


def test_unconfigured_app(tmp_path: Path) -> None:
    # 貼上範本或遺失設定時，服務顯示初始化提示並拒絕登入。
    url = auth.Settings.origin
    with TestClient(create_app(auth.Settings(), tmp_path / "test.db"), base_url=url) as browser:
        assert "帳號設定指令" in browser.get("/login").text
        assert browser.post("/login", headers={"Origin": url}).status_code == 503


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/register"), ("POST", "/signup"), ("GET", "/.env"),
     ("GET", "/uploads/private.pdf"), ("GET", "/openapi.json")],
)
def test_private_paths_are_not_exposed(client: TestClient, method: str, path: str) -> None:
    # 確認公開註冊、機密設定、上傳路徑及 API 文件均未開放。
    assert client.request(method, path).status_code == 404
