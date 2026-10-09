/* Bottom-sheet entry + grabber drag-close (PWA surface).
   Run: node --test backend/clients/pwa/tests/sheet_drag_close_test.js

   Pins the sheet Showcase contract: cards rise from the bottom edge (no
   grow-in-place), every bottom sheet carries a grab anchor, and a
   downward drag from the grabber follows the finger and dismisses past
   a distance or flick threshold — while sideways drags, taps, and
   scrolled sheets stay untouched. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

function extract(name) {
  const fn = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(fn, `${name} not found in app.js`);
  return fn[0];
}

function loadDrag() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(extract("sheetGrabHit") + "\n" + extract("createSheetDragClose"), context);
  return context;
}

function makeView({ zone = true, top = true } = {}) {
  const calls = [];
  const view = {
    calls,
    atTop: () => top,
    isGrabZone: () => zone,
    maxTravel: () => 600,
    begin: () => calls.push(["begin"]),
    setShift: (px) => calls.push(["shift", px]),
    snapback: () => calls.push(["snapback"]),
    commit: () => calls.push(["commit"]),
  };
  return view;
}

const BOTTOM_SHEETS = ["more-sheet", "today-sheet", "weather-sheet", "health-sheet",
  "looks-sheet", "people-sheet", "routines-sheet", "queue-sheet", "capture-sheet",
  "search-sheet", "memory-sheet", "conversation-sheet", "devices-sheet",
  "activity-sheet", "inbox-sheet", "mission-sheet", "settings-sheet"];
const NO_GRAB_SHEETS = ["welcome", "session-sheet", "tactical-sheet", "camera-sheet"];

test("sheets enter from the bottom edge with no grow-in-place", () => {
  assert.ok(css.includes("from { opacity: 0; transform: translate3d(0, 100%, 0); }"));
  assert.ok(!css.includes("scale(0.985)"));
});

test("grab anchor is invisible and never intercepts taps", () => {
  const rule = css.match(/\.sheet-grab \{[^}]*\}/s);
  assert.ok(rule, ".sheet-grab rule exists");
  assert.ok(rule[0].includes("pointer-events: none"));
});

test("every bottom sheet carries a grab anchor; full-screen and directional sheets do not", () => {
  const chunks = html.split('<section id="');
  const chunkFor = (id) => chunks.find((c) => c.startsWith(id + '"'));
  for (const id of BOTTOM_SHEETS) {
    const chunk = chunkFor(id);
    assert.ok(chunk, `${id} section exists`);
    assert.ok(chunk.includes('<div class="sheet-grab"'), `${id} has a grab anchor`);
  }
  for (const id of NO_GRAB_SHEETS) {
    const chunk = chunkFor(id);
    assert.ok(chunk, `${id} section exists`);
    assert.ok(!chunk.includes("sheet-grab"), `${id} has no grab anchor`);
  }
});

test("drag-close wiring runs at startup and pull-to-refresh yields the grab zone", () => {
  assert.ok(source.includes("wireSheetDragClose();"));
  assert.ok(extract("wireSheetDragClose").includes("createSheetDragClose"));
  assert.ok(extract("wireSessionPull").includes("sheetGrabHit"));
});

test("grab hit test is centered on the pill and rejects corners and body", () => {
  const context = loadDrag();
  const sheet = {};
  const grab = { getBoundingClientRect: () => ({ left: 176, top: 200, width: 38, height: 4 }) };
  assert.equal(context.sheetGrabHit(sheet, grab, 195, 202), true);
  assert.equal(context.sheetGrabHit(sheet, grab, 130, 210), true);
  assert.equal(context.sheetGrabHit(sheet, grab, 100, 202), false);
  assert.equal(context.sheetGrabHit(sheet, grab, 350, 202), false);
  assert.equal(context.sheetGrabHit(sheet, grab, 195, 300), false);
  assert.equal(context.sheetGrabHit(sheet, grab, 195, 150), false);
  assert.equal(context.sheetGrabHit(sheet, null, 195, 202), false);
  assert.equal(context.sheetGrabHit(sheet, {}, 195, 202), false);
});

test("drag starting outside the grab zone or while scrolled never tracks", () => {
  const context = loadDrag();
  for (const opts of [{ zone: false }, { top: false }]) {
    const view = makeView(opts);
    const drag = context.createSheetDragClose({}, view);
    drag.start(195, 202);
    assert.equal(drag.move(195, 300, 100), false);
    assert.equal(drag.end(), "idle");
    assert.deepEqual(view.calls, []);
  }
});

test("tap on the grabber leaves the sheet untouched", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  assert.equal(drag.end(), "tap");
  assert.deepEqual(view.calls, []);
});

test("sideways drag from the grabber stays with the horizontal gesture", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  assert.equal(drag.move(260, 207, 100), false);
  assert.equal(drag.end(), "idle");
  assert.deepEqual(view.calls, []);
});

test("short downward drag follows the finger then snaps back", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  assert.equal(drag.move(195, 207, 100), false);
  assert.equal(drag.move(196, 252, 400), true);
  assert.equal(drag.end(), "snap");
  assert.deepEqual(view.calls[0], ["begin"]);
  assert.deepEqual(view.calls[1], ["shift", 50]);
  assert.deepEqual(view.calls[view.calls.length - 1], ["snapback"]);
  assert.ok(!view.calls.some((c) => c[0] === "commit"));
});

test("drag past the commit distance closes the sheet", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  drag.move(195, 250, 100);
  drag.move(195, 320, 500);
  assert.equal(drag.end(), "close");
  assert.ok(view.calls.some((c) => c[0] === "commit"));
  assert.ok(!view.calls.some((c) => c[0] === "snapback"));
});

test("fast flick closes even short of the commit distance", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  drag.move(195, 222, 100);
  drag.move(195, 252, 110);
  assert.equal(drag.end(), "close");
  assert.ok(view.calls.some((c) => c[0] === "commit"));
});

test("upward drag resists rubbery and never commits", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  assert.equal(drag.move(195, 172, 100), true);
  assert.equal(drag.end(), "snap");
  const shifts = view.calls.filter((c) => c[0] === "shift").map((c) => c[1]);
  assert.deepEqual(shifts, [-30 * 0.16]);
  assert.ok(!view.calls.some((c) => c[0] === "commit"));
});

test("downward travel caps at the view's maximum", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  drag.move(195, 972, 100);
  const shifts = view.calls.filter((c) => c[0] === "shift").map((c) => c[1]);
  assert.deepEqual(shifts, [600]);
});

test("cancel mid-drag snaps back without committing", () => {
  const context = loadDrag();
  const view = makeView();
  const drag = context.createSheetDragClose({}, view);
  drag.start(195, 202);
  drag.move(195, 252, 100);
  assert.equal(drag.cancel(), "snap");
  assert.ok(view.calls.some((c) => c[0] === "snapback"));
  assert.ok(!view.calls.some((c) => c[0] === "commit"));
  assert.equal(drag.end(), "idle");
});
