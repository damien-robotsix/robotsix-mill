// Node harness exercising board-mill.js's "Remove repo from mill" flow:
// the button visibility toggle, the confirmation modal, and the
// DELETE /repos/{id} call (confirm / cancel / error paths).
//
// board-mill.js is a flat browser IIFE; it is loaded into a Node `vm`
// context against a minimal DOM/window stub. Uses ONLY Node built-ins.
// Exits non-zero when any scenario fails.

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

// --- minimal DOM/window stubs ------------------------------------------

function makeEl(id) {
  const listeners = {};
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    disabled: false,
    className: "",
    parentNode: null,
    _listeners: listeners,
    style: { setProperty() {} },
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    appendChild(child) {
      child.parentNode = el;
    },
    removeChild(child) {
      child.parentNode = null;
    },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    setAttribute() {},
    getAttribute: () => null,
    hasAttribute: () => false,
    querySelector: () => makeEl("_q"),
    querySelectorAll: () => [],
    closest: () => null,
    focus() {},
    remove() {},
    getContext: () => null,
    getBoundingClientRect: () => ({ width: 0, height: 0 }),
  };
  return el;
}

let elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const created = [];
const documentStub = {
  readyState: "complete",
  getElementById: getEl,
  createElement: (tag) => {
    const el = makeEl("_created_" + tag);
    created.push(el);
    return el;
  },
  querySelector: (sel) => getEl("_qs_" + sel),
  querySelectorAll: () => [],
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
    toString() {
      return this.href;
    },
  },
  localStorage: localStorageStub,
  history: { replaceState() {} },
  addEventListener() {},
  devicePixelRatio: 1,
  robotsixBoardSetGateEndpoint() {},
  robotsixBoardSetRefreshUrl() {},
  robotsixBoardRefresh() {},
};

// Every request is recorded; DELETE responses are scripted per test.
const requests = [];
let deleteResponse = { status: 204, body: "" };
const REPOS = [
  { repo_id: "foo", board_id: "foo" },
  { repo_id: "bar", board_id: "bar" },
];

class XMLHttpRequestStub {
  open(method, url) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader() {}
  send() {
    requests.push({ method: this.method, url: this.url });
    if (this.method === "DELETE") {
      this.status = deleteResponse.status;
      this.responseText = deleteResponse.body;
    } else {
      this.status = 200;
      this.responseText = this.url === "/repos" ? JSON.stringify(REPOS) : "null";
    }
    if (this.onload) this.onload();
  }
}

class WebSocketStub {
  close() {}
  send() {}
}

const ctx = {
  window: windowStub,
  document: documentStub,
  localStorage: localStorageStub,
  XMLHttpRequest: XMLHttpRequestStub,
  WebSocket: WebSocketStub,
  fetch: () => Promise.resolve({ ok: true, text: () => Promise.resolve("") }),
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout: () => 1,
  clearTimeout: () => {},
  URL,
  URLSearchParams,
  alert: () => {},
  confirm: () => true,
  console,
  marked: { parse: (s) => s },
};

vm.createContext(ctx);
vm.runInContext(source, ctx);

// --- helpers -------------------------------------------------------------

const flush = () => new Promise((r) => setImmediate(r));

async function settle() {
  for (let i = 0; i < 20; i++) await flush();
}

function click(id) {
  for (const fn of getEl(id)._listeners.click || []) fn({ target: getEl(id) });
}

function deletes() {
  return requests.filter((r) => r.method === "DELETE");
}

function openModal() {
  // Fresh element ids per modal so prior listeners do not leak.
  for (const id of ["modal-remove-cancel", "modal-remove-confirm", "modal-remove-err"]) {
    elements.delete(id);
  }
  created.length = 0;
  windowStub.removeRepo();
  return created.find((el) => el.className === "modal");
}

function toasts() {
  return created.filter((el) => String(el.className).includes("mill-toast"));
}

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

// --- scenarios -----------------------------------------------------------

await settle();

await test("button hidden on all-repos view, visible on a single-repo view", async () => {
  windowStub.onRepoChange("all");
  await settle();
  assert.equal(getEl("remove-repo-btn").style.display, "none");
  windowStub.onRepoChange("foo");
  await settle();
  assert.equal(getEl("remove-repo-btn").style.display, "");
});

await test("confirmation dialog names the repo; cancel does nothing", async () => {
  requests.length = 0;
  const modal = openModal();
  assert.ok(modal, "modal must be created");
  assert.ok(modal.innerHTML.includes("foo"), "dialog must name the repo");
  assert.ok(modal.innerHTML.includes("Remove repo from mill"));
  click("modal-remove-cancel");
  await settle();
  assert.equal(deletes().length, 0, "cancel must not call DELETE");
});

await test("error response is surfaced and repo stays selected", async () => {
  requests.length = 0;
  deleteResponse = {
    status: 409,
    body: JSON.stringify({ detail: "Repo 'foo' still has 2 open ticket(s)." }),
  };
  openModal();
  click("modal-remove-confirm");
  await settle();
  assert.deepEqual(deletes().map((r) => r.url), ["/repos/foo"]);
  const err = getEl("modal-remove-err").textContent;
  assert.ok(err.includes("409") && err.includes("open ticket"), "error shown — got " + err);
  assert.equal(localStorageStub.getItem("robotsix-mill:repo-id"), "foo");
});

await test("confirm calls DELETE, redirects to all repos and toasts", async () => {
  requests.length = 0;
  deleteResponse = { status: 204, body: "" };
  openModal();
  click("modal-remove-confirm");
  await settle();
  assert.deepEqual(deletes().map((r) => r.url), ["/repos/foo"]);
  assert.equal(localStorageStub.getItem("robotsix-mill:repo-id"), "all");
  const t = toasts();
  assert.ok(t.length === 1 && t[0].textContent.includes("foo"), "success toast expected");
  assert.equal(getEl("remove-repo-btn").style.display, "none", "button hidden on all-repos view");
});

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll remove-repo scenarios passed.");
process.exit(0);
