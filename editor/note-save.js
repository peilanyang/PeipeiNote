export function setupNoteSaving(editor, form) {
  const version = form.elements.namedItem('version');
  if (!version) return;

  const title = form.elements.namedItem('name');
  const status = document.querySelector('#note-save-status');
  const latest = document.querySelector('#note-save-latest');
  const heading = document.querySelector('#editor-heading');
  let timer;
  let changed = 0;
  let persisted = 0;
  let saving = false;
  let queued = false;
  let conflict = false;

  function warnBeforeLeaving(event) {
    event.preventDefault();
    event.returnValue = '';
  }

  function markChanged() {
    changed += 1;
    window.addEventListener('beforeunload', warnBeforeLeaving);
    if (conflict) return;
    if (!saving) status.textContent = '尚未儲存';
    clearTimeout(timer);
    queued = false;
    timer = setTimeout(save, 2000);
  }

  async function save() {
    clearTimeout(timer);
    if (conflict || changed === persisted) return;
    if (!form.reportValidity()) {
      status.textContent = '儲存失敗：請輸入記事標題。';
      return;
    }
    if (saving) {
      queued = true;
      return;
    }
    saving = true;
    queued = false;
    const revision = changed;
    const fields = new FormData(form);
    fields.set('content', editor.getHTML());
    fields.set('content_format', 'html');
    status.textContent = '儲存中…';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        headers: { Accept: 'application/json' },
        body: fields,
      });
      if (response.status === 409) {
        conflict = true;
        latest.hidden = false;
        status.textContent = '儲存失敗：另一個分頁已儲存新版，草稿仍保留在這裡。';
        return;
      }
      if (!response.ok) throw new Error('save failed');

      const result = await response.json();
      version.value = result.version;
      persisted = revision;
      heading.textContent = String(fields.get('name')).trim();
      status.textContent = changed === persisted ? '已儲存' : '尚未儲存';
      if (changed === persisted) window.removeEventListener('beforeunload', warnBeforeLeaving);
    } catch {
      status.textContent = '儲存失敗：草稿仍保留，請稍後再試。';
    } finally {
      saving = false;
      if (queued && !conflict) void save();
    }
  }

  editor.on('update', markChanged);
  title.addEventListener('input', markChanged);
  form.addEventListener('submit', event => {
    event.preventDefault();
    void save();
  });
}
