# PeipeiNote 修改調整計畫 (work_plan_261123.md)

> 依據 2026-11-23 稽核報告所定優先級分層，每一層級完成後需通過 `pytest` 全測試與 `bandit -r app/` 掃描零高風險，始能勾選完成。

---

## 📋 進度總覽

| 層級 | 項目數 | 狀態 |
|------|--------|------|
| **P0 立即修復（資安/嚴重邏輯）** | 1 | ✅ 完成 |
| **P1 核心效能/資料完整性** | 3 | ☐ 待辦 / ☐ 進行中 / ☐ 完成 |
| **P2 使用體驗明顯改善** | 2 | ☐ 待辦 / ☐ 進行中 / ☐ 完成 |
| **P3 細節打磨** | 2 | ☐ 待辦 / ☐ 進行中 / ☐ 完成 |
| **P4 架構評估/重構** | 1 | ☐ 待辦 / ☐ 進行中 / ☐ 完成 |

---

## 🔴 P0 立即修復（資安/嚴重邏輯）

### P0-1 修正搜尋結果摘要 XSS 風險 ✅
- **風險**：`search.py` 產生的 `summary` 直接拼入模板未轉義，若內文含 `<script>` 可能被瀏覽器執行
- **修正位置**：
  - `app/templates/search_results.html:11` → 將 `{{ result.summary }}` 改為 `{{ result.summary | e }}` ✅
  - `app/search.py:119` 回傳前 `html.escape(summary)`（雙重防護：後端 escape + 模板 `|e`） ✅
- **驗收**：
  1. 建立記事內容含 `<script>alert(1)</script>` ✅
  2. 搜尋關鍵字 `script` → 摘要顯示為純文字 `<script>alert(1)</script>` ✅
  3. `pytest tests/test_search.py` 全通過 ✅
  4. `bandit -r app/` 無新增高風險 ✅

---

## 🟠 P1 核心效能/資料完整性

### P1-1 `render_workspace` 合併查詢（解決 N+1）
- **現況**：單請求 8-9 個獨立 SQL 查詢（筆記本、主題清單、主題、記事、標籤、範本、附件、搜尋片段）
- **目標**：降至 2-3 個查詢（CTE 或 `joinedload` 組裝樹狀結構）
- **修正檔案**：`app/workspace.py` (`render_workspace`)、`app/access.py`（提供合併查詢輔助函式）
- **實作策略**：
  1. 單一 CTE 遞迴查詢 `notebooks → topic_lists → topics → notes`，一次取回目前使用者可見的完整樹
  2. Python 端用 `dict` 組裝 `topics_by_book`、`notes` 列表
  3. 標籤、範本、附件維持獨立查詢（資料量小、變動頻率低）
- **驗收**：
  1. 啟用 SQLAlchemy `echo=True` 確認查詢數從 9 降至 ≤3
  2. 首頁載入時間 < 200ms（本機 SQLite）
  3. `pytest tests/test_main.py::test_workspace_creates_edits_moves_and_deletes_all_levels` 通過
  4. 功能測試：建立/移動/刪除四層項目皆正常

### P1-2 `Notebook` 新增 `UniqueConstraint(owner_id, name)`（防並發建立重複收件匣）
- **現況**：`inbox_topic()` 先查後建，極短窗口可能並發建立同名筆記本
- **修正位置**：`app/database.py` `Notebook` 類別加入 `__table_args__ = (UniqueConstraint("owner_id", "name", name="uq_notebook_owner_name"),)`
- **遷移**：`initialize_household()` 中檢查並補上 constraint（SQLite 需重建表，步驟 2 已有遷移邏輯可複用）
- **驗收**：
  1. 同一使用者無法建立兩本同名筆記本（API 回 409）
  2. 不同使用者可同名（互不干擾）
  3. `pytest tests/test_household.py` 全通過

### P1-3 標籤可見性查詢快取（請求級）
- **現況**：`search_label(user_id)` 在單請求中被呼叫 3+ 次（工作區、標籤管理、標籤增刪）
- **修正位置**：`app/access.py` 新增 `get_visible_tag_ids(db, user_id) -> set[int]`，`render_workspace`、`library_routes` 共用
- **驗收**：
  1. SQLAlchemy `echo=True` 確認 `visible_to(Tag, user_id)` 子查詢每請求只執行 1 次
  2. 現有標籤增刪改查、搜尋篩選功能測試全通過

---

## 🟡 P2 使用體驗明顯改善

### P2-1 標籤管理頁內嵌記事列表（減少跳轉）
- **現況**：點「查看記事」跳轉 `/library/search?tag=...`，脫離管理上下文
- **目標**：`library.html` 模式 `tags` 時，右側面板直接顯示該標籤下的記事列表（分頁、可點擊跳轉編輯）
- **修正檔案**：
  - `app/library.py`：`manage()` 傳入 `tag_notes`（分頁查詢）
  - `app/templates/library.html`：`tag-panel` 右側新增 `tag-notes` 區塊
- **驗收**：
  1. 標籤列表點選某標籤 → 右側即時顯示該標籤文章（無頁面跳轉）
  2. 點文章標題 → 新分頁開啟編輯頁
  3. 分頁切換正常
  4. `pytest tests/test_main.py::test_search_tags_and_template_workflow` 通過

### P2-2 空狀態加入 CTA 引導
- **目標位置與文案**：
  | 頁面 | 空狀態現況 | 新增 CTA |
  |------|------------|----------|
  | 主題無記事（`workspace.html:92`） | `這個主題還沒有記事。` | `+ <a href="#new-note">新增第一則記事</a>` |
  | 搜尋無結果（`search_results.html:19`） | `找不到符合條件的記事。` | `調整關鍵字或 <a href="/?new_note=true">新增記事</a>` |
  | 標籤無記事（`library.html` 新增區塊） | — | `此標籤下尚無記事，去 <a href="/?new_note=true">新增</a>？` |
- **驗收**：
  1. 視覺檢查：空狀態區塊有明確行動按鈕/連結
  2. 操作流程：點擊 CTA → 正確導向新增記事表單/搜尋頁

---

## 🟢 P3 細節打磨

### P3-1 匯出 Word 加入載入狀態
- **現況**：點擊「匯出 Word」/「匯出整本 Word」直接下載，無回饋
- **目標**：按鈕 `disabled` + `aria-busy="true"` + 文字改為「產生中…」，下載完成恢復
- **修正檔案**：
  - `workspace.html`：匯出連結改為 `<button>` + `hx-post`（或原生 JS `fetch` + blob 下載）
  - `note_toolbar.html`：同步處理單則匯出按鈕
  - 新增 `/static/export-loading.js` 統一處理
- **驗收**：
  1. 點擊匯出 → 按鈕即時 disabled、顯示「產生中…」
  2. 大筆記本（>50 篇、含圖）測試：載入狀態持續到檔案下載彈出
  3. 無 JS 環境降級：連結仍可直接下載（`href` 保留）

### P3-2 手機版編輯器工具列虛擬鍵盤適配
- **現況**：手機鍵盤彈出時，`sticky` 底部工具列可能被遮擋或浮在鍵盤上方
- **目標**：使用 `visualViewport` API 動態調整工具列 `bottom`，或改 `position: fixed; bottom: env(keyboard-inset-height, 0)`
- **修正檔案**：
  - `app/static/workspace.css`：新增 `@supports (bottom: env(keyboard-inset-height))` 規則
  - `editor.bundle.js`：監聽 `visualViewport.onresize` 更新工具列位置
- **驗收**：
  1. iOS Safari / Android Chrome 實測：輸入中工具列始終可見、不遮擋內容
  2. 橫向/直向切換正常
  3. 桌面版無副作用

---

## 🔵 P4 架構評估/重構（選擇性）

### P4-1 評估移除 `TopicList`（主題清單）層級
- **背景**：UI 已隱藏 `TopicList`（主題直接掛筆記本），但資料庫仍保留 `topic_lists` 表，移動主題時目的地選單顯示「筆記本名稱」實際寫入 `topic_list_id`，認知負荷高
- **評估項目**：
  1. 資料遷移腳本：將現有 `topic_list_id` 改為 `notebook_id` 直屬
  2. 模型變更：`Topic.notebook_id` 取代 `topic_list_id`，刪除 `TopicList` 表
  3. API 變更：`/workspace/topic` `parent_id` → `notebook_id`，移除 `topic_list_for_book()`
  4. 相容性：舊資料遷移腳本需在 `initialize_household()` 執行
- **決策門檻**：
  - 若遷移腳本 < 50 行且風險可控 → 排程實施
  - 若涉及大量既有關聯、外鍵重構 → 保留現狀，僅在文件說明設計意圖
- **�出物**：`docs/topic_list_removal_assessment.md`（評估報告）

---

## 🛠 通用驗收流程（每項任務完成必跑）

```bash
# 1. 執行相關測試
pytest tests/ -x -q

# 2. 全專案資安掃描
bandit -r app/ -f json -o bandit-report.json

# 3. 手動功能驗收（依據各項驗收標準）

# 4. Git 提交（確認 .env、*.db、uploads/ 被 .gitignore 排除）
git add -A
git commit -m "feat/fix: <任務代碼> <一行摘要>

<詳細說明：修正內容、影響範圍、驗收方式>

Co-Authored-By: Claude Code <noreply@anthropic.com>"
```

---

## 📅 排程建議

| 週次 | 重點 |
|------|------|
| 第 1 週 | P0-1、P1-1（最高風險與效能收益） |
| 第 2 週 | P1-2、P1-3、P2-1 |
| 第 3 週 | P2-2、P3-1、P3-2 |
| 第 4 週 | P4-1 評估（視風險決定是否實施） |

---

## 📝 變更記錄

| 日期 | 版本 | 變更說明 |
|------|------|----------|
| 2026-11-23 | 1.0 | 初版，依稽核報告建立 |
| 2026-11-23 | 1.1 | P0-1 完成：搜尋結果摘要 XSS 風險修正（雙重防護：後端 `html.escape()` + 模板 `|e`） |

---

> **備註**：本計畫嚴格遵循 `2_資安與規矩.md` §3「AI 施工與驗收工作流」——**一次只執行一個未完成項目**，通過測試與掃描後才勾選完成並提交版本。