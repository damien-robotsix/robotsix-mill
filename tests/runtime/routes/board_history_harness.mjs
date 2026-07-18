// Node harness exercising the trace-breadcrumb merging in
// renderHistoryHtml() from board-mill.js.
//
// board-mill.js is a browser script wrapped in an IIFE; its wrapper is
// stripped (below) and the body loaded into a Node `vm` context against
// a hand-rolled minimal DOM/XHR stub so `renderHistoryHtml()` can be
// invoked and asserted on without a browser, a JS test runner, or any
// third-party dependency.
//
// Uses ONLY Node built-ins (node:fs, node:vm, node:path, node:assert,
// node:url). Run with `node board_history_harness.mjs`; exits non-zero
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
// surface as vm context globals, letting the harness call
// renderHistoryHtml() directly.
const source = fs
  .readFileSync(BOARD_JS, "utf8")
  .replace(/^\(function\(\)\s*\{\s*"use strict";/, "")
  .replace(/\}\)\(\);\s*$/, "")
    // After stripping the IIFE, bare `window.xxx = xxx;` assignments at
    // end of the file need `window` in scope — alias from the context.
    .replace(/^(?=\s*window\.)/m, "var window = globalThis.window;\n");

// --- minimal DOM/XHR/timer/window stubs --------------------------------
// (identical to the stubs in board_agent_colors_harness.mjs)

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
    getAttribute() { return null; },
    hasAttribute() { return false; },
    querySelector() { return makeEl("_q"); },
    querySelectorAll() { return []; },
    focus() {},
    remove() {},
    getContext() { return null; },
    getBoundingClientRect() { return { width: 0, height: 0 }; },
  };
  Object.defineProperty(el, "innerHTML", {
    get: () => el._innerHTML,
    set: (v) => { el._innerHTML = v; },
  });
  Object.defineProperty(el, "textContent", {
    get: () => el._text,
    set: (v) => { el._text = v; },
  });
  return el;
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) elements.set(id, makeEl(id));
  return elements.get(id);
}

const documentStub = {
  // Keep the DOM "loading" so board-mill.js defers its bootstrap to the
  // (noop) DOMContentLoaded listener instead of running it synchronously
  // at eval time.
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
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, v); },
  removeItem(k) { this._m.delete(k); },
};

const windowStub = {
  location: { search: "", protocol: "http:", host: "localhost", href: "http://localhost/" },
  localStorage: localStorageStub,
  history: { replaceState() {} },
  addEventListener() {},
  devicePixelRatio: 1,
};

class XMLHttpRequestStub {
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader() {}
  send() { this.status = 200; this.responseText = "null"; if (this.onload) this.onload(); }
}

class WebSocketStub {
  constructor(url) { this.url = url; }
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
  // marked.parse is an identity function so notes render as their raw
  // text and assertions can substring-match.
  marked: { parse: (s) => s },
};

const context = vm.createContext(ctx);
vm.runInContext(source, context);

// `renderHistoryHtml` is a function declaration, so it lands on the
// context object after vm.runInContext.
const { renderHistoryHtml } = ctx;

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

// ======================================================================
// Scenario 1 — trace breadcrumb + matching transition → single row
// ======================================================================
test("trace breadcrumb + matching transition renders as one row", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "ready", note: "🔍 [Trace: implement](http://lf/x)", at: "2025-01-01T00:01:00Z" },
    { state: "code_review", note: "implement: did the thing", at: "2025-01-01T00:02:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // The merged transition chip
  assert.ok(html.includes("implement → code_review"), "missing transition chip");
  // The recap text from the transition note
  assert.ok(html.includes("did the thing"), "missing recap text");
  // The trace URL from the breadcrumb note
  assert.ok(html.includes("http://lf/x"), "missing trace link");

  // Must NOT contain a standalone step row whose chip label is exactly
  // "implement".  A breadcrumb step row renders
  //   <b class="ev-state ev-step">implement</b>
  // The transition row renders
  //   <b class="ev-state s-code_review">implement → code_review</b>
  // so the substring "ev-step\">implement<" should be absent.
  const stepImplementRe = /ev-step">implement</;
  assert.ok(!stepImplementRe.test(html),
    "duplicate implement step row found — breadcrumb not absorbed");
});

// ======================================================================
// Scenario 2 — consecutive breadcrumbs with no transition → keep both
// ======================================================================
test("consecutive trace breadcrumbs without transition keep their rows", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "ready", note: "🔍 [Trace: implement](http://lf/1)", at: "2025-01-01T00:01:00Z" },
    { state: "ready", note: "🔍 [Trace: implement](http://lf/2)", at: "2025-01-01T00:02:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Both breadcrumbs should appear as ev-step rows
  const stepMatches = html.match(/ev-step">implement</g);
  assert.ok(stepMatches && stepMatches.length >= 2,
    "expected at least 2 ev-step implement breadcrumb rows, got " +
    (stepMatches ? stepMatches.length : 0));

  // Both URLs should appear
  assert.ok(html.includes("http://lf/1"), "missing first breadcrumb link");
  assert.ok(html.includes("http://lf/2"), "missing second breadcrumb link");
});

// ======================================================================
// Scenario 3 — orphan / interrupted-trace rows are unaffected
// ======================================================================
test("orphan trace rows are unaffected by breadcrumb merging", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
  ];
  const traces = [
    { trace_id: "orphan-1", at: "2025-01-01T00:02:00Z", name: "implement", cost: 0.1234, latency: 5000 },
  ];
  const html = renderHistoryHtml(events, "test-ticket", traces);

  assert.ok(html.includes("ev-orphan"), "missing orphan row");
  assert.ok(html.includes("$0.1234"), "missing orphan cost badge");
  assert.ok(html.includes("interrupted: implement"), "missing orphan label");
});

// ======================================================================
// Scenario 4 — breadcrumb + non-matching transition stage → no merge
// ======================================================================
test("breadcrumb + non-matching transition stage → no merge, both render", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "ready", note: "🔍 [Trace: implement](http://lf/x)", at: "2025-01-01T00:01:00Z" },
    // Transition note prefix "review:" → matchStep → stage "review",
    // which does not equal breadcrumb stage "implement".
    { state: "code_review", note: "review: looked at it", at: "2025-01-01T00:02:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Breadcrumb should still render as its own step row
  assert.ok(html.includes("http://lf/x"), "missing breadcrumb link");
  // Transition should render
  assert.ok(html.includes("review → code_review"), "missing transition chip");
  // The breadcrumb must appear as an ev-step row
  assert.ok(/ev-step">implement</.test(html), "missing standalone breadcrumb row");
});

// ======================================================================
// Scenario 5 — cost badge preserved on the merged transition row
// ======================================================================
test("cost badge is preserved on the merged row", () => {
  // buildEventTraceMap matches traces by eventAgentName.  For a
  // transition to state "code_review", eventAgentName returns
  // STATE_TRACE["code_review"] === "review".  We supply a trace
  // named "review" that times with the transition so it gets a cost.
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "ready", note: "🔍 [Trace: implement](http://lf/x)", at: "2025-01-01T00:01:00Z" },
    { state: "code_review", note: "implement: did the thing", at: "2025-01-01T00:02:00Z" },
  ];
  const traces = [
    { trace_id: "tr-abc", at: "2025-01-01T00:02:00Z", name: "review", cost: 0.5678 },
  ];
  const html = renderHistoryHtml(events, "test-ticket", traces);

  assert.ok(html.includes("implement → code_review"), "missing transition chip");
  assert.ok(html.includes("$0.5678"), "missing cost badge on merged row");

  // Also verify no duplicate
  const stepImplementRe = /ev-step">implement</;
  assert.ok(!stepImplementRe.test(html),
    "duplicate implement step row — breadcrumb not absorbed");
});

// ======================================================================
// Scenario 6 — first event is never absorbed (no prev to be a step)
// ======================================================================
test("first event with trace-like note is not absorbed", () => {
  // A trace-like note on the very first event cannot be a "step" (no
  // prior event to compare state with), so it must render as its own row.
  const events = [
    { state: "ready", note: "🔍 [Trace: refine](http://lf/r)", at: "2025-01-01T00:00:00Z" },
    { state: "code_review", note: "implement: did the thing", at: "2025-01-01T00:01:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // The first event is treated as "created" by eventChip (idx === 0),
  // so its chip label is "created", not "refine".
  assert.ok(html.includes("created"), "missing created chip for first event");
  assert.ok(html.includes("http://lf/r"), "missing trace link in first event");
});

// ======================================================================
// Scenario 7 — dedupe repeated implement_complete deliver.md cards
// ======================================================================
test("dedupe repeated implement_complete: only last gets deliver artifact", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:01:00Z" },
    { state: "fixing_ci", note: "", at: "2025-01-01T00:02:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:03:00Z" },
    { state: "fixing_ci", note: "", at: "2025-01-01T00:04:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:05:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Count data-art="deliver.md" occurrences — at most 1
  const deliverMatches = html.match(/data-art="deliver\.md"/g);
  const deliverCount = deliverMatches ? deliverMatches.length : 0;
  assert.equal(deliverCount, 1, "expected exactly 1 deliver.md artifact card, got " + deliverCount);

  // fixing_ci rows have suppressed artifact: data-art should be ""
  // (no ci_fix.md or merge.md artifact reference)
  const ciFixArt = html.match(/data-art="ci_fix\.md"/g);
  assert.equal(ciFixArt, null, "fixing_ci artifact must be suppressed (no data-art='ci_fix.md')");

  // No merge.md artifact reference for fixing_ci
  const mergeFixMatches = html.match(/fixing_ci.*merge\.md/);
  assert.equal(mergeFixMatches, null, "fixing_ci must not reference merge.md");

  // fixing_ci transition label should include "ci_fix" from STATE_TRACE
  assert.ok(html.includes("ci_fix → fixing_ci"), "missing ci_fix → fixing_ci transition label");
});

// ======================================================================
// Scenario 8 — no "(not yet written)" placeholder for suppressed rows
// ======================================================================
test("no not-yet-written placeholder for collapsed deliver or fixing_ci rows", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:01:00Z" },
    { state: "fixing_ci", note: "", at: "2025-01-01T00:02:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:03:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Only the last implement_complete gets a deliver.md artifact; the
  // earlier one and the fixing_ci row have data-art="" (suppressed).
  const deliverMatches = html.match(/data-art="deliver\.md"/g);
  const deliverCount = deliverMatches ? deliverMatches.length : 0;
  assert.equal(deliverCount, 1, "expected exactly 1 deliver.md artifact card, got " + deliverCount);

  // No ci_fix.md artifact reference (suppressed for fixing_ci rows)
  const ciFixArt = html.match(/data-art="ci_fix\.md"/g);
  assert.equal(ciFixArt, null, "fixing_ci artifact must be suppressed (no data-art='ci_fix.md')");

  // No "(not yet written)" text anywhere
  assert.ok(!html.includes("not yet written"), "must not contain 'not yet written' placeholder");
});

// ======================================================================
// Scenario 9 — single implement_complete (no loop) still shows its artifact
// ======================================================================
test("single implement_complete (no loop) still shows deliver card", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "code_review", note: "review: lgtm", at: "2025-01-01T00:01:00Z" },
    { state: "implement_complete", note: "", at: "2025-01-01T00:02:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Should have exactly one deliver.md artifact
  const deliverMatches = html.match(/data-art="deliver\.md"/g);
  const deliverCount = deliverMatches ? deliverMatches.length : 0;
  assert.equal(deliverCount, 1, "expected 1 deliver.md artifact for single implement_complete, got " + deliverCount);
});

// ======================================================================
// Scenario 10 — distinct non-loop stages render unchanged
// ======================================================================
test("distinct non-loop stages (refine, review, merge, done) render unchanged", () => {
  const events = [
    { state: "ready", note: "", at: "2025-01-01T00:00:00Z" },
    { state: "code_review", note: "review: lgtm", at: "2025-01-01T00:01:00Z" },
    { state: "waiting_auto_merge", note: "", at: "2025-01-01T00:02:00Z" },
    { state: "done", note: "merge: merged", at: "2025-01-01T00:03:00Z" },
    { state: "closed", note: "retrospect: all good", at: "2025-01-01T00:04:00Z" },
  ];
  const html = renderHistoryHtml(events, "test-ticket", []);

  // Review artifact
  assert.ok(html.includes('data-art="review.md"'), "missing review.md artifact");
  // Merge artifact (waiting_auto_merge → merge.md; done → merge.md)
  const mergeMatches = html.match(/data-art="merge\.md"/g);
  const mergeCount = mergeMatches ? mergeMatches.length : 0;
  assert.ok(mergeCount >= 1, "expected at least 1 merge.md artifact, got " + mergeCount);
  // Retrospect artifact
  assert.ok(html.includes('data-art="retrospect.md"'), "missing retrospect.md artifact");
  // Transition labels
  assert.ok(html.includes("review → code_review"), "missing review transition");
  assert.ok(html.includes("merge → done"), "missing merge transition");
  // closed is terminal (no stage in STATE_TRACE) — renders bare state name.
  assert.ok(html.includes('s-closed'), "missing closed state class");
});

// ======================================================================

if (failures > 0) {
  console.error("\n" + failures + " scenario(s) failed.");
  process.exit(1);
}
console.log("\nAll board-history scenarios passed.");
process.exit(0);
