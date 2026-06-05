// Node harness exercising the canonical agent→color logic in board.js.
//
// board.js is a flat browser script (no module exports); it is loaded
// here into a Node `vm` context against a hand-rolled minimal DOM/XHR
// stub so `agentColor()` / `AGENT_COLORS` can be invoked and asserted
// on without a browser, a JS test runner, or any third-party dependency.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Run with `node board_agent_colors_harness.mjs`; exits
// non-zero on the first failing assertion-group, 0 when every scenario
// passes.

import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// Resolve the real board.js relative to this harness — never duplicate it.
const BOARD_JS = path.resolve(
  __dirname,
  "../../src/robotsix_mill/runtime/static/board.js",
);
const source = fs.readFileSync(BOARD_JS, "utf8");

// --- minimal DOM/XHR/timer/window stubs --------------------------------

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

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const documentStub = {
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
  setInterval: noopTimer,
  clearInterval: () => {},
  setTimeout: noopTimer,
  clearTimeout: () => {},
  URL,
  URLSearchParams,
  alert: () => {},
  console,
  ST: [],
  marked: { parse: (s) => s },
};

const context = vm.createContext(ctx);
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

// `agentColor` is a function declaration (exposed as a context global);
// `AGENT_COLORS` is a top-level `const`, which vm does NOT attach to the
// context object — read it through the context instead.
const { agentColor } = ctx;
const AGENT_COLORS = vm.runInContext("AGENT_COLORS", context);

// ----------------------------------------------------------------------

test("hyphen and underscore spellings resolve to the same color", () => {
  // The Runs view passes RunEntry.kind which mixes hyphens/underscores;
  // the menu uses underscores. Both must agree per agent.
  const pairs = [
    ["trace-health", "trace_health"],
    ["test-gap", "test_gap"],
    ["bc-check", "bc_check"],
    ["completeness-check", "completeness_check"],
    ["config-sync", "config_sync"],
    ["cost-reconciliation", "cost_reconciliation"],
    ["roadmap-sync", "roadmap_sync"],
    ["trace-review", "trace_review"],
    ["langfuse-cleanup", "langfuse_cleanup"],
  ];
  for (const [hyphen, underscore] of pairs) {
    assert.equal(
      agentColor(hyphen),
      agentColor(underscore),
      `${hyphen} must match ${underscore}`,
    );
    // …and equal the canonical map entry (the menu side).
    assert.equal(agentColor(hyphen), AGENT_COLORS[underscore], `${hyphen} → map color`);
    assert.notEqual(agentColor(hyphen), "#6b7280", `${hyphen} must not be grey`);
  }
});

test("previously grey-only Runs kinds now render their menu color", () => {
  assert.equal(agentColor("test-gap"), "#7c3aed");
  assert.equal(agentColor("bc-check"), "#84cc16");
  assert.equal(agentColor("completeness-check"), "#84cc16");
  assert.equal(agentColor("config-sync"), "#6366f1");
  assert.equal(agentColor("cost-reconciliation"), "#6366f1");
  assert.equal(agentColor("roadmap-sync"), "#9333ea");
  assert.equal(agentColor("trace-review"), "#0ea5e9");
  assert.equal(agentColor("langfuse-cleanup"), "#14b8a6");
  assert.equal(agentColor("module_curator"), "#f97316");
  assert.equal(agentColor("meta"), "#a855f7");
});

test("the original five Runs kinds keep their colors", () => {
  assert.equal(agentColor("audit"), "#059669");
  assert.equal(agentColor("trace-health"), "#0ea5e9");
  assert.equal(agentColor("health"), "#0d9488");
  assert.equal(agentColor("agent_check"), "#db2777");
  assert.equal(agentColor("survey"), "#f59e0b");
});

test("unknown / unmapped kinds fall back to grey without throwing", () => {
  for (const k of ["copy-paste", "epic-breakdown", "data_dir_audit", "proposed_action", "some-yaml-stem", "", null, undefined]) {
    assert.equal(agentColor(k), "#6b7280", `unknown kind ${String(k)} → grey`);
  }
});

// ----------------------------------------------------------------------

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll agent-color scenarios passed.");
process.exit(0);
