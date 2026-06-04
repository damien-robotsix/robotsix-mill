// Node harness exercising the Proposals-panel client logic in board.js.
//
// board.js is a flat browser script (no module exports); it is loaded
// here into a Node `vm` context against a hand-rolled minimal DOM/XHR
// stub so the four Proposals functions (toggleProposals, renderProposals,
// approveProposal, rejectProposal) can be invoked and asserted on without
// a browser, a JS test runner, or any third-party dependency.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Run with `node board_proposals_harness.mjs`; exits non-zero
// on the first failing assertion-group, 0 when every scenario passes.

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
const stateOf = (name) => vm.runInContext(name, context);
const drawerEl = getEl("drawer");
const dEl = getEl("d");

function reset(repo) {
  requests.length = 0;
  alerts.length = 0;
  responder = () => ({ status: 200, responseText: "null" });
  for (const el of elements.values()) {
    el._innerHTML = "";
    if (el.classList && el.classList._set) el.classList._set.clear();
  }
  evalIn("proposalsOpen=false;runsOpen=false;costDashboardOpen=false;candidatesOpen=false;sel=null;");
  if (repo !== undefined) evalIn("currentRepoId=" + JSON.stringify(repo));
}

function listRequests() {
  return requests.filter((r) => r.url.includes("/proposed-actions"));
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
// toggleProposals
// ----------------------------------------------------------------------

await test("toggleProposals opens panel when closed", async () => {
  reset("repo1");
  responder = () => ({ status: 200, responseText: "[]" });
  await ctx.toggleProposals();
  assert.equal(stateOf("proposalsOpen"), true, "proposalsOpen should be true");
  assert.ok(drawerEl.classList.contains("open"), "drawer should have 'open' class");
  // renderProposals ran: either a list GET was recorded or the panel
  // rendered its empty-state message.
  assert.ok(listRequests().length >= 1, "a /proposed-actions GET should have been issued");
  assert.ok(dEl.innerHTML.includes("No pending proposed actions."), "empty-state should render");
});

await test("toggleProposals closes panel when already open", async () => {
  reset("repo1");
  evalIn("proposalsOpen=true;");
  drawerEl.classList.add("open");
  await ctx.toggleProposals();
  assert.equal(stateOf("proposalsOpen"), false, "proposalsOpen should reset to false");
  assert.ok(!drawerEl.classList.contains("open"), "drawer 'open' class should be removed");
  assert.equal(listRequests().length, 0, "no list GET — renderProposals must not run on close");
});

await test("toggleProposals is mutually exclusive with other panels", async () => {
  for (const flag of ["runsOpen", "costDashboardOpen", "candidatesOpen"]) {
    reset("repo1");
    evalIn(flag + "=true;");
    responder = () => ({ status: 200, responseText: "[]" });
    await ctx.toggleProposals();
    assert.equal(stateOf(flag), false, flag + " should be reset by close_()");
    assert.equal(stateOf("proposalsOpen"), true, "proposalsOpen should be true after opening");
    assert.ok(drawerEl.classList.contains("open"), "drawer should be open");
  }
  // Also when a ticket detail is open (sel set).
  reset("repo1");
  evalIn("sel='T-1';");
  responder = () => ({ status: 200, responseText: "[]" });
  await ctx.toggleProposals();
  assert.equal(stateOf("sel"), null, "sel should be cleared by close_()");
  assert.equal(stateOf("proposalsOpen"), true, "proposalsOpen should be true");
});

// ----------------------------------------------------------------------
// renderProposals
// ----------------------------------------------------------------------

await test("renderProposals guards on all-repos / empty repo", async () => {
  for (const repo of ["all", ""]) {
    reset(repo);
    await ctx.renderProposals();
    assert.equal(listRequests().length, 0, "no /proposed-actions request for repo=" + JSON.stringify(repo));
    assert.ok(dEl.innerHTML.includes("Select a single repo"), "should render the per-board hint");
    assert.ok(dEl.innerHTML.includes("Proposed actions"), "should render the panel title");
  }
});

await test("renderProposals renders error on non-array / failed fetch", async () => {
  // null payload
  reset("repo1");
  responder = () => ({ status: 200, responseText: "null" });
  await ctx.renderProposals();
  assert.ok(dEl.innerHTML.includes("failed to load proposed actions."), "null payload → error message");

  // non-array object payload
  reset("repo1");
  responder = () => ({ status: 200, responseText: '{"foo":1}' });
  await ctx.renderProposals();
  assert.ok(dEl.innerHTML.includes("failed to load proposed actions."), "object payload → error message");

  // non-2xx status (jget resolves null)
  reset("repo1");
  responder = () => ({ status: 500, responseText: "boom" });
  await ctx.renderProposals();
  assert.ok(dEl.innerHTML.includes("failed to load proposed actions."), "500 → error message");
});

await test("renderProposals renders empty-state for []", async () => {
  reset("repo1");
  responder = () => ({ status: 200, responseText: "[]" });
  await ctx.renderProposals();
  assert.ok(dEl.innerHTML.includes("No pending proposed actions."), "empty array → empty-state");
});

await test("renderProposals renders a populated pending item with escaping + buttons", async () => {
  reset("repo1");
  const item = {
    id: 7,
    source: "health",
    target_ticket_id: "T-1",
    action_type: "CLOSE",
    rationale: "close <b> & co",
    status: "pending",
    created_at: "2026-06-01T10:00:00",
  };
  responder = () => ({ status: 200, responseText: JSON.stringify([item]) });
  await ctx.renderProposals();
  const html = dEl.innerHTML;
  // source badge
  assert.ok(html.includes("pa-source src-health"), "source badge class");
  assert.ok(html.includes(">health</span>"), "source badge text");
  // action_type badge (lower-cased class token, original-cased text)
  assert.ok(html.includes("pa-action pa-action-close"), "action badge class");
  assert.ok(html.includes(">CLOSE</span>"), "action badge text");
  // clickable target ticket id
  assert.ok(html.includes("pa-target"), "target badge class");
  assert.ok(html.includes(">T-1</span>"), "target ticket id text");
  assert.ok(html.includes("open_(&quot;T-1&quot;)"), "target onclick opens the ticket");
  // rationale, HTML-escaped via esc()
  assert.ok(html.includes("close &lt;b&gt; &amp; co"), "rationale must be HTML-escaped");
  assert.ok(!html.includes("close <b>"), "raw unescaped rationale must NOT appear");
  // created_at + status
  assert.ok(html.includes("2026-06-01T10:00:00"), "created_at rendered");
  assert.ok(html.includes("pa-status-pending"), "status class rendered");
  assert.ok(html.includes(">pending</span>"), "status text rendered");
  // action buttons (pending → Approve/Reject present)
  assert.ok(
    html.includes('class="approve-btn" onclick="approveProposal(&quot;7&quot;)"'),
    "Approve button wired to approveProposal",
  );
  assert.ok(
    html.includes('class="reject-btn" onclick="rejectProposal(&quot;7&quot;)"'),
    "Reject button wired to rejectProposal",
  );
});

await test("renderProposals omits action buttons for non-pending items", async () => {
  reset("repo1");
  const item = {
    id: 9,
    source: "survey",
    target_ticket_id: "T-2",
    action_type: "COMMENT",
    rationale: "already done",
    status: "executed",
    created_at: "2026-06-02T11:00:00",
  };
  responder = () => ({ status: 200, responseText: JSON.stringify([item]) });
  await ctx.renderProposals();
  const html = dEl.innerHTML;
  // content still rendered
  assert.ok(html.includes(">survey</span>"), "source badge text rendered");
  assert.ok(html.includes(">T-2</span>"), "target ticket id rendered");
  assert.ok(html.includes("already done"), "rationale rendered");
  assert.ok(html.includes("pa-status-executed"), "executed status rendered");
  // but NO action buttons
  assert.ok(!html.includes("approve-btn"), "no Approve button for non-pending");
  assert.ok(!html.includes("reject-btn"), "no Reject button for non-pending");
  assert.ok(!html.includes("pa-buttons"), "no button row for non-pending");
});

await test("renderProposals requests the exact list endpoint (status + encoded repo)", async () => {
  reset("org/repo");
  responder = () => ({ status: 200, responseText: "[]" });
  await ctx.renderProposals();
  const gets = requests.filter((r) => r.method === "GET" && r.url.includes("/proposed-actions"));
  assert.equal(gets.length, 1, "exactly one list GET");
  assert.equal(
    gets[0].url,
    "/proposed-actions?status=pending&repo_id=org%2Frepo",
    "list URL must carry status=pending and the URL-encoded repo_id",
  );
});

// ----------------------------------------------------------------------
// approveProposal
// ----------------------------------------------------------------------

await test("approveProposal POSTs the approve endpoint and re-renders on ok", async () => {
  reset("org/repo");
  responder = (method, url) => {
    if (url.includes("/approve")) return { status: 200, responseText: "{}" };
    if (url.includes("/proposed-actions?status=pending")) return { status: 200, responseText: "[]" };
    return null;
  };
  await ctx.approveProposal("42");
  const post = requests.find((r) => r.method === "POST");
  assert.ok(post, "an approve POST should be issued");
  assert.equal(
    post.url,
    "/proposed-actions/42/approve?repo_id=org%2Frepo",
    "approve URL must be encoded id + repo_id",
  );
  // ok response → renderProposals() re-fetches the list
  const listGet = requests.find((r) => r.method === "GET" && r.url.includes("/proposed-actions?status=pending"));
  assert.ok(listGet, "ok approve should trigger a re-render list GET");
  assert.equal(alerts.length, 0, "no alert on ok approve");
});

await test("approveProposal alerts and does not re-render on non-ok", async () => {
  reset("repo1");
  responder = (method, url) => {
    if (url.includes("/approve")) return { status: 500, responseText: "nope" };
    return null;
  };
  await ctx.approveProposal("42");
  assert.equal(alerts.length, 1, "one alert on failure");
  assert.ok(alerts[0].includes("Approve failed"), "alert mentions approve failure");
  const listGet = requests.find((r) => r.method === "GET" && r.url.includes("/proposed-actions?status=pending"));
  assert.ok(!listGet, "failed approve must NOT re-render (no list GET)");
});

// ----------------------------------------------------------------------
// rejectProposal
// ----------------------------------------------------------------------

await test("rejectProposal POSTs the reject endpoint and re-renders on ok", async () => {
  reset("org/repo");
  responder = (method, url) => {
    if (url.includes("/reject")) return { status: 200, responseText: "{}" };
    if (url.includes("/proposed-actions?status=pending")) return { status: 200, responseText: "[]" };
    return null;
  };
  await ctx.rejectProposal("42");
  const post = requests.find((r) => r.method === "POST");
  assert.ok(post, "a reject POST should be issued");
  assert.equal(
    post.url,
    "/proposed-actions/42/reject?repo_id=org%2Frepo",
    "reject URL must be encoded id + repo_id",
  );
  const listGet = requests.find((r) => r.method === "GET" && r.url.includes("/proposed-actions?status=pending"));
  assert.ok(listGet, "ok reject should trigger a re-render list GET");
  assert.equal(alerts.length, 0, "no alert on ok reject");
});

await test("rejectProposal alerts and does not re-render on non-ok", async () => {
  reset("repo1");
  responder = (method, url) => {
    if (url.includes("/reject")) return { status: 400, responseText: "bad" };
    return null;
  };
  await ctx.rejectProposal("42");
  assert.equal(alerts.length, 1, "one alert on failure");
  assert.ok(alerts[0].includes("Reject failed"), "alert mentions reject failure");
  const listGet = requests.find((r) => r.method === "GET" && r.url.includes("/proposed-actions?status=pending"));
  assert.ok(!listGet, "failed reject must NOT re-render (no list GET)");
});

// ----------------------------------------------------------------------

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll proposals-panel scenarios passed.");
process.exit(0);
