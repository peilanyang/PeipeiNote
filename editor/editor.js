import { Editor } from '@tiptap/core';
import Image from '@tiptap/extension-image';
import StarterKit from '@tiptap/starter-kit';
import { TaskItem, TaskList } from '@tiptap/extension-list';
import { TableKit } from '@tiptap/extension-table';
import { setupNoteSaving } from './note-save.js';

const PrivateImage = Image.extend({
  parseHTML() {
    return [{
      tag: 'img[src]',
      getAttrs: element => /^\/api\/files\/[0-9a-f]{32}\.(?:png|jpe?g|gif|webp)$/.test(element.getAttribute('src'))
        ? null : false,
    }];
  },
});

function allowedLink(value) {
  if (!/^(https?:|mailto:)/i.test(value)) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'mailto:' ? Boolean(url.pathname) : Boolean(url.hostname);
  } catch {
    return false;
  }
}

const source = document.querySelector('#note-content');
const editorHost = document.querySelector('#rich-editor');
const toolbar = document.querySelector('#rich-toolbar');
const format = document.querySelector('#content-format');

if (source && editorHost && toolbar && format) {
  // 將舊記事以純文字帶入編輯器，原本的尖括號仍是文字。
  const content = format.value === 'html' ? source.value : {
    type: 'doc',
    content: source.value.replace(/\r\n?/g, '\n').split('\n').map(line => ({
      type: 'paragraph',
      ...(line ? { content: [{ type: 'text', text: line }] } : {}),
    })),
  };
  const editor = new Editor({
    element: editorHost,
    content,
    injectCSS: false,
    parseOptions: { preserveWhitespace: "full" },
    editorProps: {
      handlePaste(_view, event) {
        const files = Array.from(event.clipboardData?.files || []);
        if (!files.length || !editorHost.dataset.uploadUrl) return false;
        void uploadFiles(files);
        return true;
      },
      handleDrop(view, event) {
        const files = Array.from(event.dataTransfer?.files || []);
        if (!files.length || !editorHost.dataset.uploadUrl) return false;
        const at = view.posAtCoords({ left: event.clientX, top: event.clientY });
        if (at) editor.commands.setTextSelection(at.pos);
        void uploadFiles(files);
        return true;
      },
      attributes: { id: 'note-rich-content', role: 'textbox', 'aria-label': '內容', 'aria-multiline': 'true' },
    },
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
        link: { openOnClick: true, autolink: false, isAllowedUri: allowedLink },
      }),
      TableKit,
      PrivateImage.configure({
        resize: {
          enabled: true,
          directions: ['bottom-right'],
          minWidth: 16,
          minHeight: 16,
          alwaysPreserveAspectRatio: true,
        },
      }),
      TaskList,
      TaskItem.configure({ nested: true, HTMLAttributes: { "data-type": "taskItem" }, a11y: { checkboxLabel: (_, checked) => checked ? '已完成' : '未完成' } }),
    ],
  });

  const picker = document.querySelector('#attachment-picker');
  const choose = document.querySelector('#choose-attachments');
  const fileInput = document.querySelector('#attachment-input');
  const fileList = document.querySelector('#attachment-list');
  const status = document.querySelector('#attachment-status');
  const save = source.form.querySelector('button[type="submit"]');
  let uploading = false;

  function showAttachment(file) {
    const item = document.createElement('li');
    const link = document.createElement('a');
    link.href = file.url + '?download=true';
    link.textContent = file.name;
    const form = document.createElement('form');
    form.method = 'post';
    form.action = file.url + '/delete';
    const remove = document.createElement('button');
    remove.type = 'submit';
    remove.textContent = '移除';
    remove.setAttribute('aria-label', '移除附件 ' + file.name);
    form.append(remove);
    item.append(link, form);
    fileList.append(item);
  }

  async function uploadFiles(files) {
    if (!files.length || uploading) return;
    uploading = true;
    choose.disabled = true;
    save.disabled = true;
    status.textContent = '正在上傳…';
    const errors = [];
    let inserted = false;
    try {
      for (const file of files) {
        try {
          if (file.size > 20 * 1024 * 1024) throw new Error('單檔限 20 MB。');
          const result = await fetch(editorHost.dataset.uploadUrl, {
            method: 'POST', headers: { 'X-Filename': encodeURIComponent(file.name) }, body: file,
          });
          if (!result.ok) {
            const details = await result.json().catch(() => null);
            throw new Error(typeof details?.detail === 'string' ? details.detail : '上傳失敗。');
          }
          const entry = await result.json();
          showAttachment(entry);
          if (entry.media_type.startsWith('image/')) {
            inserted = editor.chain().focus().setImage({ src: entry.url, alt: entry.name }).run() || inserted;
          }
        } catch (error) {
          errors.push(file.name + '：' + error.message);
        }
      }
      status.textContent = errors.length ? errors.join('　') + (inserted ? '　其餘圖片已插入。' : '')
        : inserted ? '已插入圖片。請確認右上角的儲存狀態。' : '附件已上傳。';
    } finally {
      uploading = false;
      choose.disabled = false;
      save.disabled = false;
    }
  }

  if (picker) {
    choose.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      const files = Array.from(fileInput.files);
      fileInput.value = '';
      void uploadFiles(files);
    });
    picker.hidden = false;
  }

  const styles = toolbar.querySelector('select');
  const bold = toolbar.querySelector('[data-command="toggleBold"]');
  const checklist = toolbar.querySelector('[data-command="toggleTaskList"]');
  const tableButtons = toolbar.querySelectorAll('[data-table]');
  function updateToolbar() {
    styles.value = String([1, 2, 3].find(level => editor.isActive('heading', { level })) || 0);
    bold.setAttribute('aria-pressed', String(editor.isActive('bold')));
    checklist.setAttribute('aria-pressed', String(editor.isActive('taskList')));
    for (const button of tableButtons) {
      button.disabled = !editor.can()[button.dataset.command]();
    }
  }

  const commands = {
    toggleBold: () => editor.chain().focus().toggleBold().run(),
    setHorizontalRule: () => editor.chain().focus().setHorizontalRule().run(),
    toggleTaskList: () => editor.chain().focus().toggleTaskList().run(),
    insertTable: () => editor.chain().focus().insertTable({ rows: 2, cols: 2, withHeaderRow: true }).run(),
    addRowAfter: () => editor.chain().focus().addRowAfter().run(),
    addColumnAfter: () => editor.chain().focus().addColumnAfter().run(),
    deleteRow: () => editor.chain().focus().deleteRow().run(),
    deleteColumn: () => editor.chain().focus().deleteColumn().run(),
    deleteTable: () => editor.chain().focus().deleteTable().run(),
    link: () => {
      const entered = window.prompt('輸入完整網址，例如 https://example.com。留空可移除連結。', editor.getAttributes('link').href || '');
      if (entered === null) return;
      const url = entered.trim();
      if (!url) {
        editor.chain().focus().extendMarkRange('link').unsetLink().run();
        return;
      }
      if (!allowedLink(url)) {
        window.alert('請輸入 http://、https:// 或 mailto: 開頭的完整網址。');
        return;
      }
      if (editor.state.selection.empty && !editor.isActive('link')) {
        editor.chain().focus().insertContent({
          type: 'text', text: url, marks: [{ type: 'link', attrs: { href: url } }],
        }).run();
        return;
      }
      editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run();
    },
  };

  toolbar.addEventListener('mousedown', event => {
    if (event.target.closest('button')) event.preventDefault();
  });
  toolbar.addEventListener('click', event => {
    const button = event.target.closest('button[data-command]');
    if (button) commands[button.dataset.command]();
  });
  styles.addEventListener('change', () => {
    const level = Number(styles.value);
    const chain = editor.chain().focus();
    if (level) chain.setHeading({ level }).run();
    else chain.setParagraph().run();
  });
  editor.on('transaction', updateToolbar);
  updateToolbar();
  source.form.addEventListener('submit', () => {
    source.value = editor.getHTML();
    format.value = 'html';
  });
  setupNoteSaving(editor, source.form);
  source.hidden = true;
  editorHost.hidden = false;
  toolbar.hidden = false;
  document.querySelector('label[for="note-content"]').htmlFor = 'note-rich-content';
}
