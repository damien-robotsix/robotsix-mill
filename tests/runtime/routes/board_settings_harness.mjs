// Node harness exercising board-mill.js's Settings-panel dismissal paths.
//
// The Settings panel lives inside #drawer (#settings-wrap) and hides #d
// while open. Before the fix it had no × control, no Escape handling and
// no outside-click dismissal, so the operator was stuck in the overlay.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Exits non-zero on the first failing scenario.

import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BOARD_JS = path.resolve(
  __dirname,
  "../../../src/robotsix_mill/runtime/static/board-mill.js",
);
const source = fs.readFileSync(BOARD_JS, "utf8");

function makeEl(id) {
  const set = new Set();
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    style: { display: "", setProperty() {} },
    dataset: {},
    classList: {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c) => (set.has(c) ? set.delete(c) : set.add(c)),
    },
    appendChild() {},
    removeChild() {},
    addEventListener() {},
    setAttribute() {},
    getAttribute: () => null,
    hasAttribute: () => false,
    querySelector: () => null,
    querySelectorAll: () => [],
    // Ancestors the target is "inside", e.g. ["#drawer"].
    _inside: [],
    closest(sel) {
      return el._inside.includes(sel) ? el : null;
    },
    contains: () => false,
    focus() {},
    remove() {},
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
  };
  return el;
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const docListeners = {};
let modalOpen = false;
const documentStub = {
  readyState: "complete",
  getElementById: getEl,
  createElement: (tag) => makeEl("_created_" + tag),
  querySelector: (sel) =>
    sel === ".modal-backdrop" ? (modalOpen ? makeEl("_modal") : null) : null,
  querySelectorAll: () => [],
  addEventListener(type, fn) {
    (docListeners[type] = docListeners[type] || []).push(fn);
  },
  body: { ...makeEl("body"), contains: () => true },
  location: { search: "" },
};

function dispatch(type, ev) {
  for (const fn of docListeners[type] || []) fn(ev);
}

const localStorageStub = {
  _m: new Map(),
  getItem(k) {
    return this._m.has(k) ? this._m.get(k) : null;
  },
  setItem(k, v) {
    this._m.set(k, v);
  },
  removeItem(k) {
    this._m.delete(k);
  },
};

const windowStub = {
  location: { search: "", protocol: "http:", host: "localhost", href: "http://localhost/" },
  localStorage: localStorageStub,
  history: { replaceState() {} },
  addEventListener() {},
  devicePixelRatio: 1,
  robotsixBoardSetGateEndpoint() {},
  robotsixBoardSetRefreshUrl() {},
  robotsixBoardRefresh() {},
};

class XMLHttpRequestStub {
  open() {}
  setRequestHeader() {}
  send() {
    this.status = 200;
    this.responseText = "null";
    if (this.onload) this.onload();
  }
}

const ctx = {
  window: windowStub,
  document: documentStub,
  localStorage: localStorageStub,
  XMLHttpRequest: XMLHttpRequestStub,
  WebSocket: class { close() {} send() {} },
  fetch: () => Promise.resolve({ ok: true, text: () => Promise.resolve("") }),
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout: () => 1,
  clearTimeout: () => {},
  URL,
  URLSearchParams,
  alert: () => {},
  console,
  marked: { parse: (s) => s },
};
vm.runInContext(source, vm.createContext(ctx));

let failures = 0;
function test(name, fn) {
  try {
    fn();
    console.log("ok   - " + name);
  } catch (e) {
    failures++;
    console.error("FAIL - " + name + "\n    " + ((e && e.stack) || e));
  }
}

const drawer = getEl("drawer");
const wrap = getEl("settings-wrap");
const d = getEl("d");

function assertOpen() {
  assert.ok(drawer.classList.contains("open"), "drawer must be open");
  assert.equal(wrap.style.display, "", "settings must be visible");
  assert.equal(d.style.display, "none", "#d must be hidden while settings show");
}
function assertClosed() {
  assert.ok(!drawer.classList.contains("open"), "drawer must be closed");
  assert.equal(wrap.style.display, "none", "settings must be hidden");
  assert.equal(d.style.display, "", "#d must be restored");
}

function target(inside) {
  const t = makeEl("_target");
  t._inside = inside;
  return t;
}

test("× control (closeSettings) dismisses the panel", () => {
  windowStub.openSettings();
  assertOpen();
  windowStub.closeSettings();
  assertClosed();
});

test("Escape dismisses the panel", () => {
  windowStub.openSettings();
  dispatch("keydown", { key: "Escape" });
  assertClosed();
});

test("Escape defers to an open modal", () => {
  windowStub.openSettings();
  modalOpen = true;
  dispatch("keydown", { key: "Escape" });
  assertOpen();
  modalOpen = false;
  windowStub.closeSettings();
});

test("click outside the drawer dismisses the panel", () => {
  windowStub.openSettings();
  dispatch("click", { target: target([]) });
  assertClosed();
});

test("click inside the drawer keeps the panel open", () => {
  windowStub.openSettings();
  dispatch("click", { target: target(["#drawer"]) });
  assertOpen();
  windowStub.closeSettings();
});

test("generic close_() also resets settings state", () => {
  windowStub.openSettings();
  windowStub.close_();
  assertClosed();
  // Re-opening works (state was not left stuck as "open").
  windowStub.openSettings();
  assertOpen();
  windowStub.closeSettings();
});

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll board settings scenarios passed.");
process.exit(0);
