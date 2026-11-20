from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
def home() -> HTMLResponse:
    # 顯示固定的首頁，方便確認本機伺服器已啟動。
    return HTMLResponse(
        """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>PeipeiNote</title>
</head>
<body>
  <main>
    <h1>PeipeiNote</h1>
    <p>本機伺服器已啟動。</p>
  </main>
</body>
</html>"""
    )
