import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_home_serves_html() -> None:
    # 確認首頁可以正常顯示中文與啟動訊息。
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert "本機伺服器已啟動。" in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/register"),
        ("POST", "/signup"),
        ("GET", "/.env"),
        ("GET", "/uploads/private.pdf"),
        ("GET", "/openapi.json"),
    ],
)
def test_private_paths_are_not_exposed(method: str, path: str) -> None:
    # 確認首頁以外的私有路徑與公開註冊入口均未開放。
    assert client.request(method, path).status_code == 404
