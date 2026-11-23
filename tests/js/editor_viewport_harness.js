// P3-2 虛擬鍵盤適配的執行期測試環境。
// 以最小的 DOM 模擬載入 app/static/editor-viewport.js，實際觸發鍵盤彈出／收起、
// 可視區域捲動、旋轉以及工具列尺寸變化，驗證 CSS 變數的數值是否正確，
// 同時確認異常尺寸（負值、NaN、非數字）不會寫出無效的樣式值。
// 由 tests/test_editor_viewport.py 以 node 執行，node 不存在時該測試會略過。
'use strict';

const fs = require('fs');
const vm = require('vm');

const KEYBOARD_INSET = '--keyboard-inset';
const TOOLBAR_HEIGHT = '--toolbar-height';

// 模擬 documentElement.style，記錄所有被寫入的 CSS 變數。
function createStyle() {
  const values = new Map();
  return {
    values,
    setProperty(name, value) { values.set(name, String(value)); },
    removeProperty(name) { values.delete(name); },
    getPropertyValue(name) { return values.has(name) ? values.get(name) : ''; },
  };
}

// 模擬工具列元素，高度由測試腳本控制。
class FakeToolbar {
  constructor(height) {
    this.height = height;
  }

  getBoundingClientRect() {
    return { width: 320, height: this.height, top: 0, left: 0, right: 320, bottom: this.height };
  }

  get offsetHeight() { return this.height; }
}

// 只支援本專案會使用的兩個選擇器，其餘一律視為查無此元素。
function createDocument(toolbar, style) {
  const state = { toolbar };
  return {
    documentElement: { style },
    // 測試用：模擬工具列比程式晚一步出現在畫面上。
    setToolbar(element) { state.toolbar = element; },
    querySelector(selector) {
      if (!state.toolbar) return null;
      if (selector === '.note-form .rich-toolbar' || selector === '.rich-toolbar') return state.toolbar;
      return null;
    },
  };
}

// 可派發事件的目標，取代瀏覽器的 addEventListener。
function createEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
      const handlers = listeners.get(type) || [];
      const index = handlers.indexOf(handler);
      if (index >= 0) handlers.splice(index, 1);
    },
    dispatch(type) {
      (listeners.get(type) || []).slice().forEach((handler) => handler({ type }));
    },
  };
}

// 模擬 window 與 visualViewport；沒有 viewport 時代表舊版瀏覽器。
function createWindow(options) {
  const windowTarget = createEventTarget();
  const viewportTarget = options.viewport ? createEventTarget() : null;
  const win = {
    innerHeight: options.innerHeight,
    visualViewport: viewportTarget ? {
      height: options.viewport.height,
      offsetTop: options.viewport.offsetTop || 0,
      addEventListener: viewportTarget.addEventListener,
      removeEventListener: viewportTarget.removeEventListener,
    } : undefined,
    addEventListener: windowTarget.addEventListener,
    removeEventListener: windowTarget.removeEventListener,
  };
  win.__window = windowTarget;
  win.__viewport = viewportTarget;
  return win;
}

// 模擬 ResizeObserver，測試可手動回報新的工具列高度。
function createResizeObserverClass(instances) {
  return class FakeResizeObserver {
    constructor(callback) {
      this.callback = callback;
      this.targets = [];
      instances.push(this);
    }

    observe(target) { this.targets.push(target); }

    unobserve(target) { this.targets = this.targets.filter((item) => item !== target); }

    disconnect() { this.targets = []; }

    report(height) {
      this.callback(this.targets.map((target) => ({
        target,
        contentRect: { width: 320, height },
        borderBoxSize: [{ inlineSize: 320, blockSize: height }],
      })), this);
    }
  };
}

// 執行一次程式載入，回傳可操作的場景。
function runScenario(source, options) {
  const style = createStyle();
  const toolbar = options.withToolbar === false ? null : new FakeToolbar(options.toolbarHeight || 0);
  const document = createDocument(toolbar, style);
  const win = createWindow(options);
  const observers = [];
  const sandbox = {
    window: win,
    document,
    console: { log() {}, warn() {}, error() {} },
  };
  if (options.withResizeObserver !== false) {
    sandbox.ResizeObserver = createResizeObserverClass(observers);
  }
  vm.runInNewContext(source, sandbox, { filename: 'editor-viewport.js' });
  return {
    scene: { style, win, document, toolbar, observers },
    variable(name) {
      return style.values.has(name) ? style.values.get(name) : null;
    },
    variables() {
      const result = {};
      style.values.forEach((value, name) => { result[name] = value; });
      return result;
    },
  };
}

// 鍵盤彈出：可視區域只剩上半部，鍵盤佔用的高度必須寫入 CSS 變數。
function scenarioKeyboardOpen(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 400, offsetTop: 0 } });
  return { inset: run.variable(KEYBOARD_INSET), variables: run.variables() };
}

// 鍵盤收起：可視區域等於視窗高度，鍵盤高度必須歸零。
function scenarioKeyboardClosed(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 800, offsetTop: 0 } });
  return { inset: run.variable(KEYBOARD_INSET), variables: run.variables() };
}

// iOS 捲動時可視區域的 offsetTop 會變動，值必須跟著更新。
function scenarioScroll(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 600, offsetTop: 50 } });
  const initial = run.variable(KEYBOARD_INSET);
  run.scene.win.visualViewport.offsetTop = 100;
  run.scene.win.__viewport.dispatch('scroll');
  return { initial, afterScroll: run.variable(KEYBOARD_INSET) };
}

// 工具列尺寸變化由 ResizeObserver 回報；量不到時必須移除變數。
function scenarioToolbar(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 800, offsetTop: 0 } });
  const initial = run.variable(TOOLBAR_HEIGHT);
  run.scene.observers[0].report(52);
  const afterReport = run.variable(TOOLBAR_HEIGHT);
  run.scene.observers[0].report(0);
  const afterZero = run.variable(TOOLBAR_HEIGHT);
  run.scene.observers[0].report(-30);
  const afterNegative = run.variable(TOOLBAR_HEIGHT);
  return { initial, afterReport, afterZero, afterNegative };
}

// 旋轉（橫向／直向切換）後視窗高度改變，必須重新計算。
function scenarioRotation(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 400, offsetTop: 0 } });
  const before = run.variable(KEYBOARD_INSET);
  run.scene.win.innerHeight = 400;
  run.scene.win.visualViewport.height = 300;
  run.scene.win.__window.dispatch('orientationchange');
  const after = run.variable(KEYBOARD_INSET);
  run.scene.win.__window.dispatch('resize');
  return { before, after, afterResize: run.variable(KEYBOARD_INSET) };
}

// 異常尺寸：負值、NaN、Infinity 與非數字都不得寫出無效的樣式值。
function scenarioMalformed(source) {
  const run = runScenario(source, { innerHeight: 800, viewport: { height: 400, offsetTop: 0 } });
  const viewport = run.scene.win.visualViewport;
  const results = {};
  viewport.height = NaN;
  run.scene.win.__viewport.dispatch('resize');
  results.nan = run.variable(KEYBOARD_INSET);
  viewport.height = -120;
  run.scene.win.__viewport.dispatch('resize');
  results.negative = run.variable(KEYBOARD_INSET);
  viewport.height = '600';
  run.scene.win.__viewport.dispatch('resize');
  results.text = run.variable(KEYBOARD_INSET);
  viewport.height = Infinity;
  run.scene.win.__viewport.dispatch('resize');
  results.infinity = run.variable(KEYBOARD_INSET);
  viewport.height = 400;
  run.scene.win.innerHeight = NaN;
  run.scene.win.__window.dispatch('resize');
  results.windowNaN = run.variable(KEYBOARD_INSET);
  results.offsetTopNegative = null;
  viewport.offsetTop = -30;
  run.scene.win.innerHeight = 800;
  run.scene.win.__window.dispatch('resize');
  results.offsetTopNegative = run.variable(KEYBOARD_INSET);
  results.variables = run.variables();
  return results;
}

// 工具列比程式晚一步出現在畫面上：之後仍要量得到高度。
function scenarioLateToolbar(source) {
  const run = runScenario(source, {
    innerHeight: 800, viewport: { height: 400, offsetTop: 0 }, withToolbar: false,
  });
  const before = run.variable(TOOLBAR_HEIGHT);
  run.scene.document.setToolbar(new FakeToolbar(48));
  run.scene.win.__window.dispatch('resize');
  return { before, after: run.variable(TOOLBAR_HEIGHT) };
}

// 舊版瀏覽器：沒有 visualViewport 也不得拋出例外，安靜退回底部貼齊。
function scenarioNoViewport(source) {
  const run = runScenario(source, { innerHeight: 800 });
  return { inset: run.variable(KEYBOARD_INSET), variables: run.variables() };
}

// 沒有 ResizeObserver 的環境：不得拋出例外，工具列高度交由樣式表預設值。
function scenarioNoObserver(source) {
  const run = runScenario(source, {
    innerHeight: 800, viewport: { height: 400, offsetTop: 0 }, withResizeObserver: false,
  });
  return {
    inset: run.variable(KEYBOARD_INSET),
    toolbar: run.variable(TOOLBAR_HEIGHT),
    variables: run.variables(),
  };
}

(function main() {
  const source = fs.readFileSync(process.argv[2], 'utf8');
  const report = {
    keyboardOpen: scenarioKeyboardOpen(source),
    keyboardClosed: scenarioKeyboardClosed(source),
    scroll: scenarioScroll(source),
    toolbar: scenarioToolbar(source),
    rotation: scenarioRotation(source),
    malformed: scenarioMalformed(source),
    lateToolbar: scenarioLateToolbar(source),
    noViewport: scenarioNoViewport(source),
    noObserver: scenarioNoObserver(source),
  };
  process.stdout.write(JSON.stringify(report));
})();
