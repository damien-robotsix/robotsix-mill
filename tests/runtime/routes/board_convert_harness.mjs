// Node harness exercising the Convert-to-Ticket button + flow in
// board-mill.js.
//
// board-mill.js is a browser script wrapped in an IIFE; its wrapper is
// stripped (below) and the body loaded into a Node `vm` context against
// a hand-rolled minimal DOM/XHR stub so that `_actionButtonsHtml()` and
// `convertToTicket()` can be invoked and asserted on without a browser,
// a JS test runner, or any third-party dependency.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Run with `node board_convert_harness.mjs`; exits non-zero
// on the first failing assertion-group, 0 when every scenario passes.

import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Resolve the real served board-mill.js relative to this harness —
// never duplicate it.
const BOARD_JS = path.resolve(
  __dirname,
  "../../../src/robotsix_mill/runtime/static/board-mill.js",
);
// board-mill.js wraps everything in an IIFE so it leaks no browser
// globals. Strip that wrapper so the top-level function declarations
// surface as vm context globals (the way the flat board.js used to),
// letting the harness invoke _actionButtonsHtml()/convertToTicket().
const source = fs
  .readFileSync(BOARD_JS, "utf8")
  .replace(/^\(function\(\)\s*\{\s*"use strict";/, "")
  .replace(/\}\)\(\);\s*$/, "");

// --- minimal DOM/XHR/timer/window stubs --------------------------------
// These mirror the stubs used in board_proposals_harness.mjs so the
// same board.js surface loads without errors.

// Recorded XHR requests + a per-scenario programmable responder.
const requests = [];
let responder = () => ({ status: 200, responseText: "null" });
const alerts = [];

function makeEl(id) {
  const set = new Set();
  const el = {
    id,
    _innerHTML: "",
    _text: "",
    style: {},
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

// Stable per-id elements (so "drawer"/"d" and any other id return the
// same stub across lookups within a scenario).
const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const documentStub = {
  // Keep readyState "loading" so board-mill.js's bootstrap guard defers
  // millBootstrap() to the (no-op) DOMContentLoaded listener instead of
  // invoking it synchronously at eval time — this harness only exercises
  // _actionButtonsHtml()/convertToTicket(), not the bootstrap path.
  readyState: "loading",
  getElementById: getEl,
  createElement: (tag) => makeEl("_created_" + tag),
  querySelector: (sel) => getEl("_qs_" + sel),
  querySelectorAll: () => [],
  addEventListener() {},
  body: makeEl("body"),
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
  location: { search: "", protocol: "http:", host: "localhost", href: "http://localhost/" },
  localStorage: localStorageStub,
  history: { replaceState() {} },
  addEventListener() {},
  devicePixelRatio: 1,
};

class XMLHttpRequestStub {
  open(method, url) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader() {}
  send(body) {
    requests.push({ method: this.method, url: this.url, body });
    let resp;
    try {
      resp = responder(this.method, this.url, body);
    } catch (_e) {
      resp = null;
    }
    if (resp == null) {
      this.status = 0;
      this.responseText = "";
      if (this.onerror) this.onerror();
      return;
    }
    this.status = resp.status;
    this.responseText = resp.responseText;
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

// Programmable prompt stub — each scenario sets promptReturn before
// calling convertToTicket().
let promptReturn = null;

const ctx = {
  window: windowStub,
  document: documentStub,
  localStorage: localStorageStub,
  XMLHttpRequest: XMLHttpRequestStub,
  WebSocket: WebSocketStub,
  setInterval: noopTimer,
  clearInterval: () => {},
  setTimeout: noopTimer,
  clearTimeout: () => {},
  URL,
  URLSearchParams,
  alert: (m) => alerts.push(String(m)),
  prompt: () => promptReturn,
  console,
  // Globals board.js references in code paths we don't exercise, but
  // which must exist so unrelated load-time side effects don't throw.
  ST: [],
  marked: { parse: (s) => s },
};

const context = vm.createContext(ctx);
vm.runInContext(source, context);

// --- helpers to drive the loaded script --------------------------------

const flush = () => new Promise((r) => setImmediate(r));
const evalIn = (expr) => vm.runInContext(expr, context);

function reset(repo) {
  requests.length = 0;
  alerts.length = 0;
  promptReturn = null;
  responder = () => ({ status: 200, responseText: "null" });
  for (const el of elements.values()) {
    el._innerHTML = "";
    if (el.classList && el.classList._set) el.classList._set.clear();
  }
  evalIn("proposalsOpen=false;runsOpen=false;costDashboardOpen=false;candidatesOpen=false;sel=null;");
  if (repo !== undefined) evalIn("currentRepoId=" + JSON.stringify(repo));
  // Replace fire-and-forget side effects (refresh, open_) with no-ops
  // so the conversion-flow tests don't trip on unhandled rejections
  // from endpoints we haven't stubbed fully.
  evalIn("refresh = async function(){}; open_ = async function(){};");
}

// --- tiny test runner --------------------------------------------------

let failures = 0;
async function test(name, fn) {
  try {
    await fn();
    console.log("ok   - " + name);
  } catch (e) {
    failures++;
    console.error("FAIL - " + name + "\n    " + ((e && e.stack) || e));
  }
}

// Let the load-time refresh()/connectWebSocket() side effects settle,
// then clear anything they recorded before the scenarios run.
await flush();
await flush();

// ----------------------------------------------------------------------
// Button gating — _actionButtonsHtml(t)
// ----------------------------------------------------------------------

await test("_actionButtonsHtml renders Convert button for answered inquiry", async () => {
  reset("repo1");
  const html = await ctx._actionButtonsHtml({
    kind: "inquiry",
    state: "answered",
    id: "T-1",
  });
  assert.ok(
    html.includes("convertToTicket("),
    "should contain convertToTicket onclick",
  );
  assert.ok(
    html.includes("Convert to ticket"),
    "should contain 'Convert to ticket' label",
  );
});

await test("_actionButtonsHtml omits Convert button for asked (not answered) inquiry", async () => {
  reset("repo1");
  const html = await ctx._actionButtonsHtml({
    kind: "inquiry",
    state: "asked",
    id: "T-2",
  });
  assert.ok(
    !html.includes("convertToTicket"),
    "must NOT contain convertToTicket for asked inquiry",
  );
  assert.ok(
    !html.includes("Convert to ticket"),
    "must NOT contain 'Convert to ticket' label for asked inquiry",
  );
});

await test("_actionButtonsHtml omits Convert button for non-inquiry ticket", async () => {
  reset("repo1");
  const html = await ctx._actionButtonsHtml({
    kind: "task",
    state: "answered",
    id: "T-3",
  });
  assert.ok(
    !html.includes("convertToTicket"),
    "must NOT contain convertToTicket for non-inquiry",
  );
  assert.ok(
    !html.includes("Convert to ticket"),
    "must NOT contain 'Convert to ticket' label for non-inquiry",
  );
});

// ----------------------------------------------------------------------
// Conversion flow — convertToTicket(id)
// ----------------------------------------------------------------------

await test("convertToTicket cancel: prompt returns null → no POST, no alert", async () => {
  reset("repo1");
  promptReturn = null;
  await ctx.convertToTicket("T-1");
  const convertPosts = requests.filter(
    (r) => r.method === "POST" && r.url.includes("/convert-to-task"),
  );
  assert.equal(convertPosts.length, 0, "no POST when user cancels prompt");
  assert.equal(alerts.length, 0, "no alert when user cancels prompt");
});

await test("convertToTicket success: POSTs trimmed comment, no alert", async () => {
  reset("repo1");
  promptReturn = "  please do X  ";
  responder = (method, url, body) => {
    if (method === "POST" && url.includes("/convert-to-task")) {
      return { status: 200, responseText: JSON.stringify({ id: "T-9" }) };
    }
    return { status: 200, responseText: "[]" };
  };
  await ctx.convertToTicket("T-1");
  const convertPosts = requests.filter(
    (r) => r.method === "POST" && r.url.includes("/convert-to-task"),
  );
  assert.equal(convertPosts.length, 1, "exactly one convert POST");
  assert.equal(
    convertPosts[0].url,
    "/tickets/T-1/convert-to-task",
    "POST URL must include ticket id",
  );
  const sentBody = JSON.parse(convertPosts[0].body);
  assert.equal(
    sentBody.comment,
    "please do X",
    "comment must be the trimmed prompt value",
  );
  assert.equal(alerts.length, 0, "no alert on success");
});

await test("convertToTicket error: non-2xx → alert", async () => {
  reset("repo1");
  promptReturn = "x";
  responder = (method, url) => {
    if (method === "POST" && url.includes("/convert-to-task")) {
      return { status: 500, responseText: "boom" };
    }
    return { status: 200, responseText: "[]" };
  };
  await ctx.convertToTicket("T-1");
  assert.equal(alerts.length, 1, "exactly one alert on error");
  assert.ok(
    alerts[0].includes("convert to ticket failed"),
    "alert must mention 'convert to ticket failed'",
  );
});

// ----------------------------------------------------------------------

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll convert-to-ticket scenarios passed.");
process.exit(0);
