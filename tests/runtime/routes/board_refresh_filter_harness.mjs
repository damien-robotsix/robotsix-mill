// Node harness exercising board-mill.js's repo-filter bootstrap and the
// status-bar (#meta) updater.
//
// board-mill.js is a flat browser IIFE (no module exports); it is loaded
// here into a Node `vm` context against a hand-rolled minimal DOM/window
// stub so its load-time bootstrap and the updateMeta() helper can be
// driven and asserted on without a browser, a JS test runner, or any
// third-party dependency.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Run with `node board_refresh_filter_harness.mjs`; exits
// non-zero on the first failing assertion-group, 0 when every scenario
// passes.
//
// Defect-A scenario: with document.readyState already "complete" BEFORE
// the script evaluates (the broken cached/bfcache F5 path), the bootstrap
// must still run and call robotsixBoardSetRefreshUrl with the filtered
// "/board/cards?repo_id=foo" URL. Before the readyState-safe fix the
// bootstrap only ran on a DOMContentLoaded that had already fired, so the
// spy was never called.
//
// Defect-B scenario: updateMeta() must replace the permanent "loading…"
// placeholder in #meta with a real status string (card count + time).

import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Resolve the real board-mill.js relative to this harness — never duplicate it.
const BOARD_JS = path.resolve(
  __dirname,
  "../../../src/robotsix_mill/runtime/static/board-mill.js",
);
const source = fs.readFileSync(BOARD_JS, "utf8");

// --- minimal DOM/window stubs ------------------------------------------

// Number of "#board .board-card" elements the document reports.
let cardCount = 0;
const setRefreshUrlCalls = [];

function makeEl(id) {
  const set = new Set();
  const el = {
    id,
    _innerHTML: "",
    _text: "",
    style: { setProperty() {} },
    dataset: {},
    classList: {
      _set: set,
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c) => (set.has(c) ? set.delete(c) : set.add(c)),
    },
    appendChild() {},
    removeChild() {},
    addEventListener() {},
    setAttribute() {},
    getAttribute() {
      return null;
    },
    hasAttribute() {
      return false;
    },
    querySelector() {
      return makeEl("_q");
    },
    querySelectorAll() {
      return [];
    },
    closest() {
      return null;
    },
    focus() {},
    remove() {},
    getContext() {
      return null;
    },
    getBoundingClientRect() {
      return { width: 0, height: 0 };
    },
  };
  Object.defineProperty(el, "innerHTML", {
    get: () => el._innerHTML,
    set: (v) => {
      el._innerHTML = v;
    },
  });
  Object.defineProperty(el, "textContent", {
    get: () => el._text,
    set: (v) => {
      el._text = v;
    },
  });
  return el;
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

function docQuerySelectorAll(sel) {
  if (sel === "#board .board-card") {
    return Array.from({ length: cardCount }, () => makeEl("_card"));
  }
  return [];
}

const documentStub = {
  readyState: "complete",
  getElementById: getEl,
  createElement: (tag) => makeEl("_created_" + tag),
  querySelector: (sel) => getEl("_qs_" + sel),
  querySelectorAll: docQuerySelectorAll,
  addEventListener() {},
  body: makeEl("body"),
  location: { search: "?repo=foo" },
};

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
  location: {
    search: "?repo=foo",
    protocol: "http:",
    host: "localhost",
    href: "http://localhost/?repo=foo",
  },
  localStorage: localStorageStub,
  history: { replaceState() {} },
  addEventListener() {},
  devicePixelRatio: 1,
  // Shared robotsix-board hooks the bootstrap touches.
  robotsixBoardSetGateEndpoint() {},
  robotsixBoardSetRefreshUrl(url) {
    setRefreshUrlCalls.push(url);
  },
  robotsixBoardRefresh() {},
};

class XMLHttpRequestStub {
  open(method, url) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader() {}
  send() {
    this.status = 200;
    this.responseText = "null";
    if (this.onload) this.onload();
  }
}

class WebSocketStub {
  constructor(url) {
    this.url = url;
  }
  close() {}
  send() {}
}

const noopTimer = () => 1;

const ctx = {
  window: windowStub,
  document: documentStub,
  localStorage: localStorageStub,
  XMLHttpRequest: XMLHttpRequestStub,
  WebSocket: WebSocketStub,
  fetch: () => Promise.resolve({ ok: true, text: () => Promise.resolve("") }),
  setInterval: noopTimer,
  clearInterval: () => {},
  setTimeout: noopTimer,
  clearTimeout: () => {},
  URL,
  URLSearchParams,
  alert: () => {},
  console,
  marked: { parse: (s) => s },
};

const context = vm.createContext(ctx);
// Evaluate the script with readyState already "complete" (the broken
// cached/bfcache F5 path) — the readyState-safe bootstrap must still run.
vm.runInContext(source, context);

// --- tiny test runner --------------------------------------------------

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

// ----------------------------------------------------------------------
// Defect A — filter bootstrap runs even when readyState is "complete"
// ----------------------------------------------------------------------

test("bootstrap sets the filtered refresh URL when readyState is already complete", () => {
  assert.ok(
    setRefreshUrlCalls.length >= 1,
    "robotsixBoardSetRefreshUrl must be called by the bootstrap even after DOMContentLoaded fired",
  );
  assert.ok(
    setRefreshUrlCalls.includes("/board/cards?repo_id=foo"),
    "the filtered URL must carry the ?repo selection — got " +
      JSON.stringify(setRefreshUrlCalls),
  );
});

// ----------------------------------------------------------------------
// Defect B — updateMeta replaces the permanent "loading…" placeholder
// ----------------------------------------------------------------------

test("updateMeta leaves loading… and reports the card count", () => {
  const meta = getEl("meta");
  meta._text = "loading…";
  cardCount = 3;
  windowStub.updateMeta();
  assert.notEqual(meta.textContent, "loading…", "#meta must no longer be the placeholder");
  assert.ok(meta.textContent.includes("3"), "#meta must report the card count — got " + meta.textContent);
  assert.ok(meta.textContent.includes("tickets"), "#meta should read '<n> tickets · <time>'");
});

test("updateMeta is a no-op safe guard when #meta is absent", () => {
  // Drop the #meta element; updateMeta must not throw.
  elements.delete("meta");
  const origGet = documentStub.getElementById;
  documentStub.getElementById = (id) => (id === "meta" ? null : origGet(id));
  try {
    windowStub.updateMeta();
  } finally {
    documentStub.getElementById = origGet;
  }
});

// ----------------------------------------------------------------------

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll board refresh-filter / meta scenarios passed.");
process.exit(0);
