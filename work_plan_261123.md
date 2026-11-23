# PeipeiNote 修改調整計畫 (work_plan_261123.md)

> 依據 2026-11-23 稽核報告所定優先級分層，每一層級完成後需通過 `pytest` 全測試與 `bandit -r app/` 掃描零高風險，始能勾選完成。

---

## 📋 進度總覽

| 層級 | 項目數 | 狀態 |
|------|--------|------|
| **P0 立即修復（資安/嚴重邏輯）** | 1 | ✅ 完成 |
| **P1 核心效能/資料完整性** | 3 | ✅ 3 完成 |
| **P2 使用體驗明顯改善** | 2 | ✅ 2 完成 |
| **P3 細節打磨** | 2 | ✅ 2 完成 |
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

### P1-1 `render_workspace` 合併查詢（解決 N+1） ✅
- **現況**：單請求 8-9 個獨立 SQL 查詢（筆記本、主題清單、主題、記事、標籤、範本、附件、搜尋片段）
- **目標**：降至 2-3 個查詢（CTE 或 `joinedload` 組裝樹狀結構）
- **修正檔案**：`app/workspace.py` (`render_workspace`)、`app/access.py`（提供合併查詢輔助函式 `workspace_tree`）
- **實作策略**：
  1. 單一 CTE 遞迴查詢 `notebooks → topic_lists → topics`，一次取回目前使用者可見的完整樹（不含 notes，notes 僅在選取主題時查詢）
  2. Python 端用 `dict` 組裝 `section_map`、`topics`、`topics_by_book`
  3. 標籤、範本、附件維持獨立查詢（資料量小、變動頻率低）
- **驗收**：
  1. 啟用 SQLAlchemy `echo=True` 確認樹狀查詢從多次降至 2 個（CTE + notebook_order）✅
  2. 首頁載入時間 < 200ms（本機 SQLite）✅
  3. `pytest tests/test_main.py::test_workspace_creates_edits_moves_and_deletes_all_levels` 通過 ✅
  4. 功能測試：建立/移動/刪除四層項目皆正常 ✅

### P1-2 `Notebook` 新增 `UniqueConstraint(owner_id, name)`（防並發建立重複收件匣） ✅
- **現況**：`inbox_topic()` 先查後建，極短窗口可能並發建立同名筆記本
- **修正位置**：
  - `app/database.py:32` `Notebook` 類別已有 `__table_args__ = (UniqueConstraint("owner_id", "name", name="uq_notebook_owner_name"),)`
  - `app/database.py:177-188` `initialize_household()` 遷移邏輯：重建表並加入 unique constraint，去重保留每用戶首筆同名記錄
- **驗收**：
  1. 同一使用者無法建立兩本同名筆記本（資料庫層面 IntegrityError → API 回 409）✅
  2. 不同使用者可同名（互不干擾）✅
  3. 遷移正確處理既有重複資料（去重）✅
  4. `pytest tests/test_household.py` 全通過 ✅
  5. `pytest tests/` 全測試 45 通過 ✅
  6. `bandit -r app/` 零高風險 ✅

### P1-3 標籤可見性查詢快取（請求級） ✅
- **現況**：`search_label(user_id)` 在單請求中被呼叫 3+ 次（工作區、標籤管理、標籤增刪）
- **修正位置**：
  - `app/access.py:76-88` 新增 `get_visible_tag_ids(db, user_id) -> set[int]` 單次查詢取得可見標籤 ID 集合
  - `app/workspace.py:179` `render_workspace` 改用快取函式
  - `app/library.py:51, 155` `search_page`、`update_note_tag` 改用快取函式
- **驗收**：
  1. `get_visible_tag_ids` 單次查詢取代多處 `search_label` 子查詢 ✅
  2. `pytest tests/` 全測試 45 通過 ✅
  3. `bandit -r app/` 零高風險 ✅
  4. 標籤搜尋、增刪改查功能正常 ✅

---

## 🟡 P2 使用體驗明顯改善

### P2-1 標籤管理頁內嵌記事列表（減少跳轉） ✅
- **現況**：點「查看記事」跳轉 `/library/search?tag=...`，脫離管理上下文
- **目標**：`library.html` 模式 `tags` 時，右側面板直接顯示該標籤下的記事列表（分頁、可點擊跳轉編輯）
- **修正檔案**：
  - `app/library.py`：`manage()` 新增 `tag_page` 參數，新增 `fetch_tag_notes()` 查詢指定標籤下的記事（分頁、授權檢查）
  - `app/templates/library.html`：`tag-panel` 右側直接渲染 `tag_notes` 列表（伺服端渲染，移除 AJAX），空狀態顯示 CTA
  - `app/templates/library.html`：`.tag-layout` 改為三欄響應式網格（≥1100px 表單/列表/文章三欄並排、760–1099px 兩欄、<700px 單欄）
- **驗收**：
  1. 標籤列表點選某標籤 → 右側即時顯示該標籤文章（無頁面跳轉）✅
  2. 點文章標題 → 新分頁開啟編輯頁（`target="_blank" rel="noopener"`）✅
  3. 分頁切換正常（`tag_page` 參數）✅
  4. `pytest tests/test_main.py::test_search_tags_and_template_workflow` 通過 ✅
  5. `pytest tests/` 全測試 50 通過 ✅
  6. `bandit -r app/` 零風險 ✅
- **新增測試**：
  - `test_library_tags_manage_with_selected_tag`
  - `test_library_tags_manage_tag_notes_page_links`（以 `monkeypatch` 縮小 `PAGE_SIZE` 驗證分頁連結）
  - `test_library_tags_manage_tag_notes_pagination`
  - `test_library_tags_manage_nonexistent_tag_404`
  - `test_library_tags_manage_tag_notes_empty_state`

### P2-2 空狀態加入 CTA 引導 ✅
- **目標位置與文案**：
  | 頁面 | 空狀態現況 | 新增 CTA |
  |------|------------|----------|
  | 主題無記事（`workspace.html:92`） | `這個主題還沒有記事。` | `+ <a href="#new-note">新增第一則記事</a>` |
  | 搜尋無結果（`search_results.html:19`） | `找不到符合條件的記事。` | `調整關鍵字或 <a href="/?new_note=true">新增記事</a>` |
  | 標籤無記事（`library.html` 新增區塊） | — | `此標籤下尚無記事，去 <a href="/?new_note=true">新增</a>？` |
- **實作內容**：
  - `app/templates/workspace.html`：主題沒有記事時，於記事清單下方新增主按鈕 `<a class="button-link primary" href="#new-note">新增第一則記事</a>`；同一頁已有 `id="new-note"` 的新增表單，且 `.new-note:target` 會自動加上綠框提示。
  - `app/templates/search_results.html`：搜尋無結果的「新增記事」連結由失效的 `href="#new-note"` 改為 `href="/?new_note=true"`（搜尋頁本身沒有 `#new-note` 錨點），標籤篩選分支一併補上「新增記事」。
  - `app/templates/library.html`：標籤無記事的 CTA 已於 P2-1 完成，本次僅納入回歸驗收。
- **驗收**：
  1. 視覺檢查：空狀態區塊有明確行動按鈕/連結 ✅
  2. 操作流程：點擊 CTA → 正確導向新增記事表單/搜尋頁 ✅
  3. 主題有記事時不再顯示空狀態引導，避免重複提示 ✅
  4. 搜尋頁與工作區局部搜尋（`partial=true`）都使用可用的 `/?new_note=true` ✅
  5. 惡意關鍵字不會寫入空狀態連結，空狀態區塊不產生可執行標籤 ✅
  6. `pytest tests/` 全測試 54 通過 ✅
  7. `bandit -r app/` 零風險 ✅
- **新增測試**：
  - `test_empty_topic_offers_create_first_note_call_to_action`
  - `test_empty_search_results_offer_search_and_create_actions`
  - `test_partial_search_results_keep_create_action_target`
  - `test_empty_search_results_keep_malicious_keyword_escaped`

---

## 🟢 P3 細節打磨

### P3-1 匯出 Word 加入載入狀態 ✅
- **現況**：點擊「匯出 Word」/「匯出整本 Word」直接下載，無回饋
- **目標**：按鈕 `disabled` + `aria-busy="true"` + 文字改為「產生中…」，下載完成恢復
- **規格落差修正**：
  - `note_toolbar.html` 只有富文字工具列（粗體／連結／待辦／表格／附件），並沒有匯出按鈕；單則匯出實際位於 `workspace.html` 的 `note-exports` 區塊，本次未硬塞匯出按鈕進工具列。
  - 專案沒有 HTMX，改用原生 vanilla JS + `fetch` + blob 下載（風格比照 `library-search.js`）。
  - 原本以 8 秒 `setTimeout` 猜測完成時間的 inline script 已移除，改由外部檔案負責。
- **實作內容**：
  - `app/static/export-loading.js`（新增）：統一處理所有匯出控制項。觸發後 `disabled` + `aria-busy="true"` + 文字「產生中…」，以 `fetch(url, {credentials:'same-origin'})` 取回完整回應後才觸發 blob 下載，`finally` 才還原按鈕；失敗時顯示「匯出失敗，請重試」而非靜默。
  - `app/templates/workspace.html`：單則與整本匯出改為 `<button type="button" data-export-url="…" hidden>`，同一處保留 `<a href="…" data-export-fallback>` 作為無 JavaScript 降級；列印連結維持 `<a>` 導向行為。
  - `app/static/workspace.css`：`.export-btn` 改為按鈕外觀，`.export-btn[disabled]`／`.loading` 顯示等待游標，新增 `.export-status` 錯誤訊息樣式。
- **降級策略（修正 `<noscript>` 版本的兩個缺陷）**：
  - 降級連結「永遠存在於 DOM」，不再用 `<noscript>` 包住：沒有 JavaScript 時顯示連結、按鈕維持 `hidden`；有 JavaScript 時改顯示按鈕、隱藏連結。因此不會出現「沒有作用的可點按鈕」，而且即使 `/static/export-loading.js` 載入失敗，匯出仍然可以直接下載。
  - 釋放 blob 網址的延遲由 60 毫秒改為 60 秒：原本 60 毫秒會讓大型筆記本在瀏覽器開始下載前就失效，導致下載失敗。
- **驗收**：
  1. 點擊匯出 → 按鈕即時 disabled、顯示「產生中…」 ✅
  2. 大筆記本（>50 篇、含圖）測試：載入狀態持續到檔案下載彈出 ✅（改以伺服器真的回應為準，全程唯一計時器只用於釋放 blob 網址，不再猜測完成秒數）
  3. 無 JS 環境降級：連結仍可直接下載（`href` 保留） ✅
  4. 未登入匯出 401、無權限匯出 404 維持不變，新介面不削弱驗權 ✅
  5. 下載檔名經淨化，無路徑分隔符、無 CRLF 注入 ✅
  6. `pytest tests/` 全測試 73 通過 ✅
  7. `bandit -r app/` 零風險 ✅
- **新增測試**：
  - `test_note_export_button_uses_progressive_enhancement`
  - `test_notebook_export_button_uses_progressive_enhancement`
  - `test_export_links_survive_without_javascript`
  - `test_workspace_drops_the_timer_based_inline_export_script`
  - `test_print_link_keeps_navigation_behaviour`
  - `test_export_script_sets_loading_state_and_uses_safe_requests`
  - `test_export_script_sanitizes_download_filename`
  - `test_export_script_falls_back_when_utf8_file_name_is_broken`
  - `test_export_script_waits_for_the_real_response_instead_of_a_fixed_timer`
  - `test_export_script_keeps_the_blob_alive_long_enough_to_download`
  - `test_export_script_does_not_download_print_pages_as_blob`
  - `test_export_endpoints_require_login`
  - `test_export_endpoints_hide_other_users_notebooks`
  - `test_export_file_name_has_no_path_separator_or_crlf`
  - `test_large_notebook_export_still_returns_every_note`
  - `test_shared_notebook_export_keeps_working_for_partner`
  - `test_export_script_disables_the_button_until_the_file_arrives`
  - `test_export_script_sanitizes_the_download_name_at_runtime`
  - `test_export_script_reports_failures_instead_of_failing_silently`
- **JavaScript 行為測試**：
  - `tests/js/export_loading_harness.js`（新增）：以最小 DOM 模擬實際載入 `export-loading.js` 並跑一次完整點擊流程，驗證停用、忙碌標記、等待真實回應、檔名淨化與失敗提示。此測試由 pytest 驅動，環境沒有 node 時會自動略過。

### P3-2 手機版編輯器工具列虛擬鍵盤適配 ✅
- **現況**：手機鍵盤彈出時，`sticky` 底部工具列可能被遮擋或浮在鍵盤上方
- **目標**：使用 `visualViewport` API 動態調整工具列 `bottom`，或改 `position: fixed; bottom: env(keyboard-inset-height, 0)`
- **規格落差修正**：
  - 修正檔案清單原定修改 `editor.bundle.js`，但該檔是第三方 TipTap 3.34.0 的壓縮 vendored 套件（專案沒有 package.json、沒有前端建置流程），直接改它會在升級時遺失且無法審閱；改以獨立檔 `app/static/editor-viewport.js` 實作，兩邊互不干擾。
  - 原定的 `visualViewport.onresize` 並非真實 API（`visualViewport` 只有 `addEventListener('resize'|'scroll')`），已改用真實事件。
  - `env(keyboard-inset-height)` 目前沒有任何正式瀏覽器實作，只能當未來-proof 的漸進增強，主力仍是 `visualViewport` 量測；兩者同時存在時取較大值。
- **實作內容**：
  - `app/static/editor-viewport.js`（新增）：監聽 `visualViewport` 的 `resize`/`scroll` 與 `window` 的 `resize`/`orientationchange`，以 `max(0, window.innerHeight − visualViewport.height − visualViewport.offsetTop)` 算出鍵盤佔用高度並寫入根元素的 `--keyboard-inset`；另以 `ResizeObserver` 量測工具列高度寫入 `--toolbar-height`（量不到就移除變數，讓樣式表使用預設值）。所有數值都經過 `Number.isFinite` 與 `Math.max(0, …)` 夾制，NaN／負值／非數字一律視為「沒有鍵盤」。
  - `app/static/workspace.css`：手機版（`max-width: 640px`）工具列由 `position: sticky` 改為 `position: fixed; bottom: var(--keyboard-inset, 0px)`；刪除 `focus-within { position: static }`（原本讓工具列在「打字時」整個消失，直接違反驗收條件 1）；補上 `padding-inline` 讓固定後的工具列不貼齊螢幕邊緣；新增 `.note-form { padding-bottom: calc(var(--toolbar-height, 3.25rem) + 1rem) }` 內容補償；安全區內距改為扣掉鍵盤高度避免重複計算；新增 `@supports (bottom: env(keyboard-inset-height))` 規則取兩者較大值。
  - `app/templates/note_editor.html`：加入 `<script defer src="/static/editor-viewport.js"></script>`（工作區與範本／標籤管理頁共用同一個 include，兩頁同時生效）。
- **降級策略**：
  - 沒有 JavaScript 時 `--keyboard-inset` 無值，工具列以 `bottom: 0` 貼齊畫面底部、內容補償退回預設 `3.25rem`，功能不會壞掉。
  - 沒有 `visualViewport`（舊版瀏覽器）時靜靜退回貼齊底部；沒有 `ResizeObserver` 時工具列高度交由樣式表預設值，兩者都不會拋出例外。
  - 桌面版（>640px）完全沿用原本的 `sticky` 定位，樣式改動全部包在 `@media (max-width: 640px)` 內。
- **驗收**：
  1. 手機輸入時工具列固定在鍵盤上緣、輸入中不會消失 ✅（刪除 focus-within hack ＋ `position: fixed` ＋ `--keyboard-inset`）
  2. 橫向／直向切換：`orientationchange` 與 `resize` 都會重算 ✅
  3. 桌面版無副作用：`position: fixed` 全檔只出現在手機版媒體區塊，桌面規則逐字未動 ✅
  4. 工具列不再壓住最後一段內容（等高空白補償）✅
  5. `pytest tests/` 全測試 97 通過 ✅
  6. `bandit -r app/` 零風險 ✅
- **新增測試**：
  - `test_workspace_page_loads_the_viewport_script`
  - `test_library_page_loads_the_viewport_script`
  - `test_viewport_script_is_served_as_javascript`
  - `test_viewport_script_does_not_replace_the_editor_bundle`
  - `test_mobile_toolbar_is_fixed_and_uses_the_keyboard_inset`
  - `test_mobile_toolbar_no_longer_sticks_to_the_bottom`
  - `test_focus_within_static_position_removed`
  - `test_desktop_toolbar_rules_are_untouched`
  - `test_mobile_content_reserves_room_for_the_fixed_toolbar`
  - `test_safe_area_padding_does_not_double_count_the_keyboard`
  - `test_fixed_toolbar_keeps_its_content_off_the_screen_edge`
  - `test_fixed_position_is_only_used_on_small_screens`
  - `test_keyboard_inset_environment_fallback_is_applied`
  - `test_viewport_script_has_no_injection_or_network_channels`
  - `test_viewport_script_clamps_sizes_before_writing_css`
  - `test_keyboard_inset_is_written_when_the_keyboard_opens`
  - `test_keyboard_inset_returns_to_zero_when_the_keyboard_closes`
  - `test_visual_viewport_scroll_updates_the_keyboard_inset`
  - `test_toolbar_height_is_published_from_the_resize_observer`
  - `test_rotation_recalculates_the_keyboard_inset`
  - `test_malformed_viewport_sizes_never_produce_invalid_css_values`
  - `test_unmeasurable_toolbar_height_removes_the_css_variable`
  - `test_toolbar_mounted_after_the_script_is_still_measured`
  - `test_old_browsers_fall_back_without_raising_errors`
- **JavaScript 行為測試**：
  - `tests/js/editor_viewport_harness.js`（新增）：以最小 DOM 模擬實際載入 `editor-viewport.js`，觸發鍵盤彈出／收起、可視區域捲動、旋轉、工具列尺寸變化與惡意尺寸（負值／NaN／Infinity／非數字），驗證 CSS 變數數值正確且不會寫出無效樣式值。此測試由 pytest 驅動，環境沒有 node 時會自動略過。

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
| 2026-11-23 | 1.2 | P1-1 完成：`render_workspace` 合併查詢（CTE 遞迴查詢 `notebooks → topic_lists → topics`，查詢數從 9 降至 ≤3） |
| 2026-11-23 | 1.3 | P1-2 完成：`Notebook` 加入 `UniqueConstraint(owner_id, name)`，遷移重建表並去重 |
| 2026-11-23 | 1.4 | P1-3 完成：標籤可見性查詢請求級快取（`get_visible_tag_ids` 取代多處 `search_label` 子查詢） |
| 2026-11-23 | 1.5 | P2-1 完成：標籤管理頁內嵌記事列表（`manage()` 新增 `tag_notes` 分頁查詢、`library.html` 伺服端渲染並移除 AJAX、三欄響應式版面） |
| 2026-11-23 | 1.6 | P2-2 完成：空狀態加入 CTA 引導（主題無記事新增「新增第一則記事」按鈕、搜尋無結果的「新增記事」改用可用的 `/?new_note=true`、標籤分支一併補上） |
| 2026-11-23 | 1.7 | P3-1 完成：匯出 Word 加入載入狀態（新增 `/static/export-loading.js`，按下後 `disabled` + `aria-busy` + 「產生中…」，改以 `fetch` 取回完整回應後才觸發 blob 下載；降級連結常駐 DOM，無 JavaScript 或程式檔載入失敗時仍可直接下載） |
| 2026-11-23 | 1.8 | P3-2 完成：手機版編輯器工具列虛擬鍵盤適配（新增 `/static/editor-viewport.js`，以 `visualViewport` 量測鍵盤高度寫入 `--keyboard-inset`；手機版工具列改用 `position: fixed` 並刪除會讓工具列在打字時消失的 `focus-within` hack，補上內容與安全區補償；桌面版樣式不動） |

---

> **備註**：本計畫嚴格遵循 `2_資安與規矩.md` §3「AI 施工與驗收工作流」——**一次只執行一個未完成項目**，通過測試與掃描後才勾選完成並提交版本。