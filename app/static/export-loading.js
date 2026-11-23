// 匯出 Word 載入狀態：按下後等到伺服器真的回應，才恢復按鈕可點擊。
const EXPORT_BUSY_TEXT = '產生中…';
const EXPORT_ERROR_TEXT = '匯出失敗，請重試';
const EXPORT_PRINT_BUSY_TEXT = '開啟列印…';
const DEFAULT_FILE_NAME = 'PeipeiNote.docx';
const MAX_FILE_NAME_LENGTH = 120;
// 釋放 blob 網址的延遲（毫秒）。必須留足時間讓瀏覽器把大檔案寫完，
// 否則大型筆記本在下載開始前就失效，只會得到一個下載失敗。
const REVOKE_DELAY_MS = 60000;
// 檔名不得出現的路徑分隔符、保留字元與控制字元。
const UNSAFE_FILE_NAME = /[\\/:*?"<>|\u0000-\u001f\u007f]/g;
// 目前仍被瀏覽器持有的 blob 網址，下一次匯出前先釋放，避免堆積記憶體。
let activeObjectUrl = null;

function sanitizeFileName(value) {
  // 檔名只保留一般字元，避免以標題衍生路徑穿越或惡意副檔名。
  const cleaned = (value || '')
    .replace(UNSAFE_FILE_NAME, '_')
    .replace(/^\.+/, '')
    .trim();
  if (!cleaned) return DEFAULT_FILE_NAME;
  const dot = cleaned.lastIndexOf('.');
  const extension = dot > 0 ? cleaned.slice(dot) : '';
  const base = dot > 0 ? cleaned.slice(0, dot) : cleaned;
  const room = Math.max(1, MAX_FILE_NAME_LENGTH - extension.length);
  return `${base.slice(0, room)}${extension}`;
}

function decodeFileName(value) {
  try {
    return decodeURIComponent(value);
  } catch (error) {
    console.warn('[export] 檔名編碼損毀，改用一般檔名。', error);
    return null;
  }
}

function fileNameFrom(response) {
  // 優先採用 RFC 5987 的 UTF-8 檔名，失敗才退回一般 filename 或預設名稱。
  const header = response.headers.get('Content-Disposition') || '';
  const utf8 = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (utf8) {
    const decoded = decodeFileName(utf8[1].trim().replace(/^"|"$/g, ''));
    if (decoded) return sanitizeFileName(decoded);
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain ? sanitizeFileName(plain[1]) : DEFAULT_FILE_NAME;
}

function triggerDownload(blob, fileName) {
  // 以暫存的物件網址觸發瀏覽器下載，並帶上伺服器提供的乾淨檔名。
  if (activeObjectUrl) {
    URL.revokeObjectURL(activeObjectUrl);
    activeObjectUrl = null;
  }
  const objectUrl = URL.createObjectURL(blob);
  activeObjectUrl = objectUrl;
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = fileName;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 只在延遲一段時間後釋放，讓瀏覽器有足夠時間開始寫入檔案。
  setTimeout(() => {
    URL.revokeObjectURL(objectUrl);
    if (activeObjectUrl === objectUrl) activeObjectUrl = null;
  }, REVOKE_DELAY_MS);
}

function setBusy(button, busy) {
  // 載入中停用按鈕並標示忙碌狀態，完成後還原原本的文字。
  if (busy) {
    button.dataset.exportLabel = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.classList.add('loading');
    button.textContent = EXPORT_BUSY_TEXT;
    return;
  }
  button.disabled = false;
  button.removeAttribute('aria-busy');
  button.classList.remove('loading');
  if (button.dataset.exportLabel) button.textContent = button.dataset.exportLabel;
}

function showStatus(button, message) {
  // 失敗訊息放在具名角色的區域，避免匯出失敗時毫無聲息。
  let status = button.nextElementSibling;
  if (!status || !status.classList.contains('export-status')) {
    status = document.createElement('span');
    status.className = 'export-status';
    status.setAttribute('role', 'status');
    button.after(status);
  }
  status.textContent = message || '';
  return status;
}

async function exportDocument(button) {
  // 取回完整檔案後才觸發下載，載入狀態因此對應到真正的下載完成。
  const url = button.dataset.exportUrl;
  setBusy(button, true);
  showStatus(button, '');
  try {
    const response = await fetch(url, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`匯出失敗（HTTP ${response.status}）`);
    const blob = await response.blob();
    triggerDownload(blob, fileNameFrom(response));
  } catch (error) {
    console.error('[export] 匯出失敗。', error);
    showStatus(button, EXPORT_ERROR_TEXT);
  } finally {
    setBusy(button, false);
  }
}

function markPrintBusy(link) {
  // 列印頁是整頁導向，只提供短暫回饋，恢復由 pageshow 事件負責。
  link.dataset.printLabel = link.textContent;
  link.setAttribute('aria-busy', 'true');
  link.textContent = EXPORT_PRINT_BUSY_TEXT;
}

function restorePrintLabels() {
  // 從列印頁返回時還原列印連結文字。
  document.querySelectorAll('[data-export-format="pdf"]').forEach((link) => {
    link.removeAttribute('aria-busy');
    if (link.dataset.printLabel) link.textContent = link.dataset.printLabel;
  });
}

function setupExportControls() {
  // 漸進式增強：無 JavaScript 時顯示 href 降級連結並隱藏按鈕，
  // 有 JavaScript 時才改用具 loading 狀態的按鈕；即使程式檔載入失敗，
  // 降級連結仍然可以直接下載，不會讓匯出功能整個失效。
  document.querySelectorAll('[data-export-fallback]').forEach((link) => {
    link.hidden = true;
  });
  document.querySelectorAll('[data-export-url]').forEach((button) => {
    button.hidden = false;
    button.addEventListener('click', (event) => {
      event.preventDefault();
      if (button.disabled) return;
      exportDocument(button);
    });
  });
  document.querySelectorAll('[data-export-format="pdf"]').forEach((link) => {
    link.addEventListener('click', () => markPrintBusy(link));
  });
}

setupExportControls();
window.addEventListener('pageshow', restorePrintLabels);
