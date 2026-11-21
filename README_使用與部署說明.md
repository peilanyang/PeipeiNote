# PeipeiNote 使用與部署說明

PeipeiNote 是最多兩組白名單帳號的家庭筆記系統。每人有自己的私人筆記本，也可以選擇分享特定筆記本。結構是「筆記本 → 主題清單 → 主題 → 筆記」。以下說明如何在 Windows 開啟、如何備份並還原，以及如何在自己的 Linux 主機透過 HTTPS 使用。

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

- **建立筆記**：按首頁上方的「新增筆記」，填入名稱並選擇主題。還沒有主題時，第一則會放在「收件匣 › 一般 › 隨手記」。進入編輯器後，寫完按「儲存筆記」。
- **圖片與附件**：圖片可貼上或拖進正文，拉右下角可縮放，完成後記得儲存。附件在編輯區下方上傳，單檔上限 20 MB。PDF 須有可解析的頁面，加密 PDF 請先另存為可開啟的版本。DOCX 的必要 XML 部件每個上限為 8 MiB。
- **登入期限**：從登入起最多保留一年，到期後須重新登入。這次升級會讓舊版未設定期限的工作階段失效。
- **標籤、範本與搜尋**：左欄開啟標籤或範本管理。中欄可即時搜尋筆記標題和內文，切換全部或單本範圍。列表以瀏覽器時區顯示更新時間，升級前的舊筆記若沒有時間紀錄，會等到下次儲存或移動才顯示。
- **另存範本**：儲存文字與排版，內嵌圖片及附件不會複製。
- **刪除**：刪除含下層項目或附件的資料時，畫面會要求另外確認一併永久刪除。只有筆記本擁有者可刪除整本。
- **Word 與列印**：從筆記匯出 Word 或列印／另存 PDF。選擇筆記本可匯出整本及階層目錄。輸出取用已儲存的內容。

### 網頁打不開

- 確認網址是 `http://127.0.0.1:8000/`，並檢查啟動視窗有沒有錯誤。
- 若顯示 `address already in use`，通常已有服務在使用 8000。先確認是否有另一個 PeipeiNote 啟動視窗，按 **Ctrl+C** 結束舊程序，再執行 `start.bat`。若舊服務在背景執行，先辨識是 PeipeiNote 再停止。
- 設定自訂網址時，請讓瀏覽器網址的協定、網域和連接埠與 `.env` 內的 `APP_ORIGIN` 一致。

## 設定第二組帳號與驗收家庭共用

1. 先備份資料並儲存正在編輯的筆記。在伺服器視窗按 **Ctrl+C**。在專案資料夾的 PowerShell 執行下列指令。

   ```powershell
   .\.venv\Scripts\python.exe -m app.setup_account --partner
   ```

2. 輸入不同的帳號名稱、至少 8 字元且與主帳號不同的密碼。指令會把第二組帳號及其 Argon2id 雜湊加到既有的 `.env`。Linux 對應指令是 `.venv/bin/python -m app.setup_account --partner`。手動管理設定時，可使用 `PARTNER_USERNAME` 與 `PARTNER_PASSWORD_HASH`。
3. 重新執行 `start.bat`。第一次啟動新版會自動遷移舊資料，由第一組帳號私人持有。兩人分別用一般與無痕視窗登入，或使用不同的瀏覽器設定檔。
4. 用其中一位帳號建立測試筆記本及筆記，附上一張圖片或 PDF。另一位重新整理工作區，應看不到這本筆記本。直接開啟該筆記或附件網址應顯示 404。
5. 回到擁有者的筆記本頁面，點「設為共用」。另一位重新整理後應看到帶有 👥 圖示的筆記本，並能新增筆記、編輯、開啟附件以及匯出 Word。在兩個視窗開啟同一筆記並同時修改，讓第一個視窗先儲存，第二個視窗再儲存。後者會收到版本衝突提示，草稿會保留。
6. 擁有者點「設為私人」。另一位重新整理後，選單、筆記、附件與匯出連結都會失去存取權。先前下載到個人裝置的副本仍在。

第二帳號建立後，請保留原使用者名稱。替同一人換密碼時只更新密碼雜湊。若改由不同的人使用，請先由本機管理員處理原帳號的私人資料。系統會拒絕用新名稱啟用原有槽位。舊資料庫若曾停用第二帳號且升級時沒有原帳號設定，也需要本機管理員核對資料歸屬後處理。

## 完整備份與還原

| 項目 | 內容 |
| --- | --- |
| `peipeinote.db` | 筆記本、筆記、標籤、範本、附件索引與搜尋索引 |
| `uploads/` | 圖片與上傳附件的原始檔 |
| `.env` | 白名單帳號、密碼雜湊、登入金鑰和站台網址 |

Word 匯出適合保存可讀文件。要把系統還原到原本可以登入、搜尋及下載附件的狀態，請備份上表三項。

### 備份

家庭資料包含兩位使用者的私人內容，完整備份由本機操作員執行。在專案資料夾執行下列 PowerShell 指令，可取得含 SQLite 快照及附件的 ZIP。另請在受保護的位置保存同一批 `.env`。

```powershell
.\.venv\Scripts\python.exe -m app.backup "$env:USERPROFILE\PeipeiNote-backup.zip"
```

指令需要新的目的檔名，請選版本庫外的安全目錄。Linux 可用 `.venv/bin/python -m app.backup /安全目錄/PeipeiNote-backup.zip`。家庭帳號曾啟用的資料庫會停用網頁完整備份。你也可以依下列步驟停機備份。

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

上列部署由 Caddy 直接接收外部 HTTPS 流量，Uvicorn 只監聽回環位址，並只接受來自本機 Caddy 的轉送標頭。請在部署驗收時確認 8000 埠無法從外部連入，以及不同網路的登入失敗紀錄依各自的來源 IP 計數。若另外使用 CDN 或負載平衡器，請按實際代理鏈設定可信來源範圍。

部署設定參考 [Caddy 自動 HTTPS](https://caddyserver.com/docs/automatic-https) 與 [Caddy 反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。

## 開發時重建前端

調整編輯器或樣式時，在 `editor/` 執行 `npm ci`，再執行 `npm run build`。只調整 Tailwind 樣式時可執行 `npm run build:css`。平常啟動會直接使用版本庫中的編譯檔。
