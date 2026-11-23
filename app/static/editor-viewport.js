// 手機版編輯器工具列的虛擬鍵盤適配。
// iOS Safari 的虛擬鍵盤不會帶動 position: sticky 的元素，工具列會被鍵盤整個蓋住；
// 因此改由這支程式量測 visualViewport，把鍵盤佔用的高度寫成 CSS 變數，
// 讓工具列以 position: fixed 停在鍵盤上方，輸入中也不會消失。
// 安全設計：只讀取尺寸，不改動 DOM 結構、不送出任何網路請求。
'use strict';

// CSS 變數名稱，與 workspace.css 的手機版規則對應。
const KEYBOARD_INSET_VARIABLE = '--keyboard-inset';
const TOOLBAR_HEIGHT_VARIABLE = '--toolbar-height';
// 需要量測的工具列，工作區與範本／標籤頁共用同一組編輯器元件。
const TOOLBAR_SELECTOR = '.note-form .rich-toolbar';
const FALLBACK_TOOLBAR_SELECTOR = '.rich-toolbar';
// 目前已量測的工具列，重算時可重複使用。
let observedToolbar = null;

// 只接受非負的有限數字；NaN、Infinity 與其他型別都視為無效值。
function isValidSize(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

// 將尺寸夾在 0 以上，確保寫進樣式的永遠是合法數值。
function toSize(value) {
  return isValidSize(value) ? value : 0;
}

// 取得根元素的樣式物件；環境異常時回傳 null，避免拋出例外。
function rootStyle() {
  try {
    const root = document.documentElement;
    if (!root || !root.style) return null;
    return root.style;
  } catch (error) {
    console.warn('[editor-viewport] 無法取得根元素樣式。', error);
    return null;
  }
}

// 寫入 CSS 變數；數值一律夾在 0 以上，不會出現 NaNpx 或負值。
function setVariable(name, value) {
  const style = rootStyle();
  if (!style || typeof style.setProperty !== 'function') return;
  style.setProperty(name, `${toSize(value)}px`);
}

// 移除 CSS 變數，讓樣式表改用預設值。
function removeVariable(name) {
  const style = rootStyle();
  if (!style || typeof style.removeProperty !== 'function') return;
  style.removeProperty(name);
}

// 計算虛擬鍵盤佔用的高度：視窗高度扣掉可視區域高度與位移。
function readKeyboardInset() {
  try {
    const viewport = window.visualViewport;
    // 舊版瀏覽器沒有 visualViewport，工具列退回貼齊底部。
    if (!viewport) return 0;
    const windowHeight = window.innerHeight;
    const viewportHeight = viewport.height;
    const offsetTop = viewport.offsetTop;
    // 任一尺寸不合法時無法推算鍵盤高度，一律視為沒有鍵盤。
    if (!isValidSize(windowHeight) || !isValidSize(viewportHeight) || !isValidSize(offsetTop)) {
      return 0;
    }
    return Math.max(0, windowHeight - viewportHeight - offsetTop);
  } catch (error) {
    console.warn('[editor-viewport] 無法計算鍵盤高度。', error);
    return 0;
  }
}

// 把鍵盤高度寫入 --keyboard-inset，工具列據此停在鍵盤上方。
function updateKeyboardInset() {
  setVariable(KEYBOARD_INSET_VARIABLE, readKeyboardInset());
}

// 從 ResizeObserver 的通知取得工具列高度，優先採用含內距與邊線的邊框盒尺寸。
function readEntryHeight(entry) {
  if (!entry) return 0;
  const box = entry.borderBoxSize;
  const size = Array.isArray(box) ? box[0] : box;
  if (size) {
    if (typeof size.blockSize === 'number') return toSize(size.blockSize);
    if (typeof size.height === 'number') return toSize(size.height);
  }
  const rect = entry.contentRect;
  if (rect && typeof rect.height === 'number') return toSize(rect.height);
  return 0;
}

// 量測元素的可見高度；量不到時回傳 0。
function measureElementHeight(element) {
  if (!element) return 0;
  try {
    if (typeof element.getBoundingClientRect === 'function') {
      const rect = element.getBoundingClientRect();
      if (rect && typeof rect.height === 'number') return toSize(rect.height);
    }
    if (typeof element.offsetHeight === 'number') return toSize(element.offsetHeight);
  } catch (error) {
    console.warn('[editor-viewport] 無法量測工具列高度。', error);
  }
  return 0;
}

// 工具列高度寫入 --toolbar-height；量不到時移除變數，讓 CSS 退回預設值。
function updateToolbarHeight(height) {
  if (!isValidSize(height) || height <= 0) {
    removeVariable(TOOLBAR_HEIGHT_VARIABLE);
    return;
  }
  setVariable(TOOLBAR_HEIGHT_VARIABLE, height);
}

// ResizeObserver 的通知：工具列尺寸改變時更新高度變數。
function handleResizeEntries(entries) {
  entries.forEach((entry) => {
    const height = readEntryHeight(entry) || measureElementHeight(entry && entry.target);
    updateToolbarHeight(height);
  });
}

// 尋找頁面上的工具列，找不到就放棄（頁面可能沒有編輯器）。
function findToolbar() {
  if (typeof document.querySelector !== 'function') return null;
  try {
    return document.querySelector(TOOLBAR_SELECTOR) || document.querySelector(FALLBACK_TOOLBAR_SELECTOR);
  } catch (error) {
    console.warn('[editor-viewport] 找不到工具列。', error);
    return null;
  }
}

// 接上工具列的量測；沒有 ResizeObserver 時改以視窗事件觸發量測。
function ensureToolbarObserved() {
  if (observedToolbar) return;
  const toolbar = findToolbar();
  if (!toolbar) return;
  observedToolbar = toolbar;
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(handleResizeEntries);
    observer.observe(toolbar);
  }
  updateToolbarHeight(measureElementHeight(toolbar));
}

// 檢查工具列高度是否需要重算（旋轉或視窗大小改變後使用）。
function remeasureToolbar() {
  if (!observedToolbar) return;
  updateToolbarHeight(measureElementHeight(observedToolbar));
}

// 任何會改變可視區域的情況都要重新計算鍵盤高度。
function handleViewportChange() {
  updateKeyboardInset();
  // 工具列可能比程式晚一步出現在畫面上，因此每次都再確認一次。
  ensureToolbarObserved();
  remeasureToolbar();
}

// 綁定鍵盤彈出／收起、可視區域捲動與旋轉等事件。
function listenViewportEvents() {
  try {
    const viewport = window.visualViewport;
    if (viewport && typeof viewport.addEventListener === 'function') {
      viewport.addEventListener('resize', handleViewportChange);
      viewport.addEventListener('scroll', handleViewportChange);
    }
    if (typeof window.addEventListener === 'function') {
      window.addEventListener('resize', handleViewportChange);
      window.addEventListener('orientationchange', handleViewportChange);
    }
  } catch (error) {
    console.warn('[editor-viewport] 事件綁定失敗。', error);
  }
}

// 程式啟動流程：綁定事件、量測工具列並寫出目前的鍵盤高度。
function init() {
  listenViewportEvents();
  ensureToolbarObserved();
  updateKeyboardInset();
}

if (typeof document !== 'undefined') {
  init();
}
