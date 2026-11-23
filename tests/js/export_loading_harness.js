// P3-1 匯出載入狀態的執行期測試環境。
// 以最小的 DOM 模擬載入 app/static/export-loading.js，實際跑一次點擊流程，
// 驗證按鈕的 disabled／aria-busy／文字變化，以及下載檔名的淨化結果。
// 由 tests/test_export_loading.py 以 node 執行，node 不存在時該測試會略過。
'use strict';

const fs = require('fs');
const vm = require('vm');

// 程式觸發下載時會立刻移除暫存連結，因此在被移除前就記錄下載內容。
const downloads = [];

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.listeners = new Map();
    this._text = '';
    this._hidden = false;
    this.disabled = false;
    this.classList = {
      add: (...names) => this.setAttribute('class', [...this.classNames(), ...names].join(' ')),
      remove: (...names) => this.setAttribute(
        'class', this.classNames().filter((name) => !names.includes(name)).join(' '),
      ),
      contains: (name) => this.classNames().includes(name),
    };
    this.dataset = new Proxy({}, {
      get: (_, key) => this.getAttribute(dataAttribute(key)),
      set: (_, key, value) => { this.setAttribute(dataAttribute(key), value); return true; },
      has: (_, key) => this.getAttribute(dataAttribute(key)) !== null,
    });
  }

  classNames() {
    return (this.getAttribute('class') || '').split(/\s+/).filter(Boolean);
  }

  get hidden() { return this._hidden; }
  set hidden(value) {
    this._hidden = Boolean(value);
    if (this._hidden) this.attributes.set('hidden', '');
    else this.attributes.delete('hidden');
  }

  // 程式以屬性方式指派 href／download，模擬環境需與屬性同步。
  get href() { return this.getAttribute('href'); }
  set href(value) { this.setAttribute('href', value); }
  get download() { return this.getAttribute('download'); }
  set download(value) { this.setAttribute('download', value); }

  get textContent() {
    if (!this.childNodes.length) return this._text;
    return this.childNodes.map((child) => child.textContent).join('');
  }
  set textContent(value) { this._text = String(value); this.childNodes = []; }

  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatchEvent(event) {
    const handler = (this.listeners.get(event.type) || []).slice();
    handler.forEach((fn) => fn(event));
    return true;
  }

  click() {
    if (this.attributes.has('download')) {
      downloads.push({ href: this.getAttribute('href'), name: this.getAttribute('download') });
    }
    this.dispatchEvent({ type: 'click', preventDefault() { this.defaultPrevented = true; } });
  }

  appendChild(child) {
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes = this.parentNode.childNodes.filter((child) => child !== this);
    this.parentNode = null;
  }

  after(node) {
    if (!this.parentNode) throw new Error('after() 需要有父節點');
    const index = this.parentNode.childNodes.indexOf(this);
    node.parentNode = this.parentNode;
    this.parentNode.childNodes.splice(index + 1, 0, node);
    return node;
  }

  get nextElementSibling() {
    if (!this.parentNode) return null;
    const index = this.parentNode.childNodes.indexOf(this);
    return this.parentNode.childNodes.slice(index + 1).find((node) => node instanceof FakeElement) || null;
  }
}

function dataAttribute(key) {
  return 'data-' + String(key).replace(/[A-Z]/g, (char) => '-' + char.toLowerCase());
}

function makeDocument(root) {
  const all = [];
  const walk = (node) => {
    node.childNodes.forEach((child) => {
      if (child instanceof FakeElement) { all.push(child); walk(child); }
    });
  };
  walk(root);
  return {
    body: root,
    createElement: (tag) => new FakeElement(tag),
    querySelectorAll: (selector) => {
      const matched = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(selector);
      if (!matched) throw new Error(`不支援的選擇器：${selector}`);
      const [, name, value] = matched;
      return all.filter((node) => node.hasAttribute(name)
        && (value === undefined || node.getAttribute(name) === value));
    },
  };
}

function buildScene() {
  const fallback = new FakeElement('a');
  fallback.setAttribute('href', '/export/notes/1.docx');
  fallback.setAttribute('class', 'export-btn');
  fallback.setAttribute('data-export-fallback', '');
  fallback.textContent = '匯出 Word';

  const button = new FakeElement('button');
  button.setAttribute('type', 'button');
  button.setAttribute('class', 'export-btn');
  button.setAttribute('data-export-url', '/export/notes/1.docx');
  button.setAttribute('hidden', '');
  button.hidden = true;
  button.textContent = '匯出 Word';

  const printLink = new FakeElement('a');
  printLink.setAttribute('href', '/print/notes/1');
  printLink.setAttribute('class', 'export-btn');
  printLink.setAttribute('data-export-format', 'pdf');
  printLink.textContent = '列印／另存 PDF';

  const nav = new FakeElement('nav');
  nav.appendChild(fallback);
  nav.appendChild(button);
  nav.appendChild(printLink);

  const body = new FakeElement('body');
  body.appendChild(nav);
  return { body, fallback, button, printLink };
}

// 執行一次點擊流程，回傳可斷言的狀態快照。
async function runScenario(source, disposition) {
  const scene = buildScene();
  const document = makeDocument(scene.body);
  downloads.length = 0;
  const revoked = [];
  const timers = [];
  const fetchCalls = [];
  let pending = null;
  let objectCounter = 0;

  const realSetTimeout = setTimeout;
  const fakeURL = {
    createObjectURL: () => { objectCounter += 1; return `blob:fake/${objectCounter}`; },
    revokeObjectURL: (url) => revoked.push(url),
  };

  const sandbox = {
    document,
    window: { addEventListener: () => {} },
    console: { log() {}, warn() {}, error() {} },
    URL: fakeURL,
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
    clearTimeout: () => {},
    fetch: (url, options) => {
      fetchCalls.push({ url, options });
      return new Promise((resolve, reject) => { pending = { resolve, reject }; });
    },
  };
  vm.runInNewContext(source, sandbox, { filename: 'export-loading.js' });

  const afterSetup = {
    fallbackHidden: scene.fallback.hidden,
    buttonRevealed: !scene.button.hidden,
    printBusy: scene.printLink.getAttribute('aria-busy'),
  };

  scene.button.click();

  const busyNow = {
    disabled: scene.button.disabled === true,
    ariaBusy: scene.button.getAttribute('aria-busy'),
    text: scene.button.textContent,
  };

  await new Promise((resolve) => realSetTimeout(resolve, 0));
  const busyWhileWaiting = scene.button.disabled === true
    && scene.button.getAttribute('aria-busy') === 'true';

  pending.resolve({
    ok: true,
    status: 200,
    headers: { get: (name) => (name.toLowerCase() === 'content-disposition' ? disposition : null) },
    blob: async () => ({ size: 12, type: 'docx' }),
  });
  await new Promise((resolve) => realSetTimeout(resolve, 0));
  await new Promise((resolve) => realSetTimeout(resolve, 0));

  scene.body.childNodes.forEach((node) => {
    (node.childNodes || []).forEach((child) => {
      if (child instanceof FakeElement && child.attributes.has('download')) {
        downloads.push({ href: child.getAttribute('href'), name: child.getAttribute('download') });
      }
    });
  });
  return {
    afterSetup,
    busyNow,
    busyWhileWaiting,
    fetchCalls,
    downloads: downloads.slice(),
    revoked: revoked.length,
    revokeDelay: timers.length ? timers[0].delay : null,
    done: {
      disabled: scene.button.disabled === true,
      ariaBusy: scene.button.getAttribute('aria-busy'),
      text: scene.button.textContent,
    },
    statusText: (scene.button.nextElementSibling || { textContent: '' }).textContent,
  };
}

async function runFailureScenario(source) {
  const scene = buildScene();
  const document = makeDocument(scene.body);
  let pending = null;
  const sandbox = {
    document,
    window: { addEventListener: () => {} },
    console: { log() {}, warn() {}, error() {} },
    URL: { createObjectURL: () => 'blob:fake/1', revokeObjectURL: () => {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    fetch: () => new Promise((resolve, reject) => { pending = { resolve, reject }; }),
  };
  vm.runInNewContext(source, sandbox, { filename: 'export-loading.js' });
  scene.button.click();
  pending.resolve({ ok: false, status: 500, headers: { get: () => null }, blob: async () => ({}) });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
  return {
    statusText: (scene.button.nextElementSibling || { textContent: '' }).textContent,
    disabled: scene.button.disabled === true,
    text: scene.button.textContent,
  };
}

(async () => {
  const source = fs.readFileSync(process.argv[2], 'utf8');
  const normal = await runScenario(
    source,
    "attachment; filename=PeipeiNote.docx; filename*=UTF-8''%E9%80%B1%E4%BA%94%E6%9C%83%E8%AD%B0.docx",
  );
  const malicious = await runScenario(
    source,
    "attachment; filename=PeipeiNote.docx; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd%22.docx",
  );
  const failure = await runFailureScenario(source);
  process.stdout.write(JSON.stringify({ normal, malicious, failure }));
})().catch((error) => {
  process.stderr.write(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
