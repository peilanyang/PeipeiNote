# PeipeiNote 使用與部署說明

PeipeiNote 是單一帳號的私人筆記系統。結構是「筆記本 → 主題清單 → 主題 → 筆記」。以下說明如何在 Windows 開啟、如何備份並還原，以及如何在自己的 Linux 主機透過 HTTPS 使用。

## 在 Windows 開啟

1. 第一次使用請先安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在 PowerShell 執行 `uv --version` 確認可用。
2. 到專案資料夾雙擊 `start.bat`。它會建立 Python 環境、安裝套件並啟動伺服器。首次安裝需要網路。
3. 用瀏覽器開啟 [PeipeiNote 本機頁面](http://127.0.0.1:8000/)。如果你已設定帳號，直接登入。
4. 若畫面提示要初始化帳號，在伺服器視窗按 **Ctrl+C**，再於專案資料夾的 PowerShell 執行下列指令。密碼最少 8 字元，可混用英文、數字和符號。兩次輸入都會以星號遮蔽。

```powershell
.\.venv\Scripts\python.exe -m app.setup_account
```

看到「帳號已建立」後，再雙擊 `start.bat`。日常使用可保留啟動視窗，關閉瀏覽器後也可以再打開網址。

### 日常操作

- **建立筆記**：從左欄選筆記本、主題清單和主題，再按「新增筆記」。填入內容後按「儲存筆記」。
- **圖片與附件**：圖片可貼上或拖進正文，拉右下角可縮放，完成後記得儲存。附件在編輯區下方上傳，單檔上限 20 MB。
- **標籤、範本與搜尋**：左欄開啟標籤或範本管理，上方可搜尋筆記標題和內文。
- **Word 與列印**：從筆記匯出 Word 或列印／另存 PDF。選擇筆記本可匯出整本及階層目錄。輸出取用已儲存的內容。

### 網頁打不開

- 確認網址是 `http://127.0.0.1:8000/`，並檢查啟動視窗有沒有錯誤。
- 若顯示 `address already in use`，通常已有服務在使用 8000。先確認是否有另一個 PeipeiNote 啟動視窗，按 **Ctrl+C** 結束舊程序，再執行 `start.bat`。若舊服務在背景執行，先辨識是 PeipeiNote 再停止。
- 設定自訂網址時，請讓瀏覽器網址的協定、網域和連接埠與 `.env` 內的 `APP_ORIGIN` 一致。

## 完整備份與還原

| 項目 | 內容 |
| --- | --- |
| `peipeinote.db` | 筆記本、筆記、標籤、範本、附件索引與搜尋索引 |
| `uploads/` | 圖片與上傳附件的原始檔 |
| `.env` | 管理者帳號、密碼雜湊、登入金鑰和站台網址 |

Word 匯出適合保存可讀文件。要把系統還原到原本可以登入、搜尋及下載附件的狀態，請備份上表三項。

### 備份

1. 儲存所有正在編輯的筆記，停止 PeipeiNote。在 Windows 的啟動視窗按 **Ctrl+C**；若以系統服務執行，則停止該服務。
2. 在專案外建立一個有日期的備份資料夾，將三項一起複製過去。同一時間若仍有 `peipeinote.db-wal`、`peipeinote.db-shm` 或 `peipeinote.db-journal`，請保留同一批檔案一起複製。
3. 在有權限限制的位置保存備份。若移到雲端硬碟或可攜式硬碟，使用加密備份，並將密碼另外保管。`.env`、資料庫和附件都含有私人資料。
4. 重新啟動 PeipeiNote。

### 還原或搬到另一台電腦

1. 先停止目標位置的 PeipeiNote。把目標位置現有的三項及資料庫附屬檔移到另一個有日期的資料夾，以便需要時可以回復。
2. 從**同一批備份**把 `peipeinote.db`、`uploads/`、`.env` 及備份中存在的資料庫附屬檔放回專案根目錄。附件用整個資料夾還原。
3. 若換了網址，在 `.env` 更新 `APP_ORIGIN`。例如 Windows 本機使用 `http://127.0.0.1:8000`。若希望讓所有舊登入憑證失效，可替換 `SECRET_KEY` 為新的長隨機金鑰。
4. 啟動並用原帳密登入。打開一則舊筆記、搜尋其中的文字，再確認圖片正常顯示且附件可下載。

資料庫及 `uploads/` 由一台伺服器管理。多裝置請連到同一個網址，避免兩台電腦同時寫入同步資料夾裡的資料庫。[SQLite 附屬檔說明](https://sqlite.org/wal.html#the_wal_file)

## 部署到自己的 Linux 主機

以下範例使用單台 Linux 主機、Uvicorn 和 Caddy。需要一個指向主機的網域，並先完成一份備份。範例使用 `notes.example.com`，部署時換成你的實際網域。

1. 用專用非 root 帳號放置專案並執行應用程式。專案與私人資料放在該帳號可讀寫的目錄，例如 `/srv/peipeinote`。把資料庫與附件放在主機的持久儲存空間。
2. 在專案根目錄建立 Python 3.11 以上的環境、安裝套件，確認 SQLite 支援 FTS5 的 trigram 索引。

```bash
umask 077
chmod 700 .
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE test USING fts5(body, tokenize=trigram)")'
```

3. 若搬移既有資料，先照還原步驟放回整套備份。全新部署則執行 `.venv/bin/python -m app.setup_account`。把 `.env` 裡的網址改成實際 HTTPS 位址，尾端省略斜線。

```dotenv
APP_ORIGIN=https://notes.example.com
```

4. 限制私人資料的權限。由執行 PeipeiNote 的使用者在專案根目錄執行：

```bash
mkdir -p uploads
chmod 600 .env
chmod 700 uploads
if test -f peipeinote.db; then chmod 600 peipeinote.db; fi
```

5. 在同一台主機啟動 Uvicorn，讓它只接受本機反向代理的連線。確認可用後，可用系統服務把同一條指令設為開機啟動，並將工作目錄設為專案根目錄。系統服務也應使用專用帳號，並設定 `UMask=0077`。

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log
```

6. 在 Caddy 加入以下站點設定，僅讓外部連入網站使用的 80、443 埠，將 8000 留在主機內部。確認網域已正確指向主機，再重新載入 Caddy。

```caddyfile
notes.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

7. 用另一台裝置開啟 HTTPS 網址，確認憑證有效、能登入及儲存測試筆記。再開無痕視窗確認受保護的筆記、下載、匯出入口會要求登入。定期更新 Python 與系統套件，更新前先備份。

部署設定參考 [Caddy 自動 HTTPS](https://caddyserver.com/docs/automatic-https) 與 [Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。
