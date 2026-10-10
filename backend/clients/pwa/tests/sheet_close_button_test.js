/* Sheet X close button contract (PWA surface).
   Run: node --test backend/clients/pwa/tests/sheet_close_button_test.js

   Pins the on-language drop-in: every reachable sheet's X is an inline
   SVG cross (not the U+2715 text glyph), sits symmetric with the 26px
   padding grid, carries a sheet-specific label, and closes cleanly
   under reduced motion without fighting pull-to-refresh. The
   unreachable tactical sheet keeps its legacy glyph until product
   decides its fate. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

const SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

// data-close target -> expected accessible label, from each sheet's h2.
const LABELS = {
  "more-sheet": "Close Your tools",
  "weather-sheet": "Close Weather",
  "health-sheet": "Close Health",
  "looks-sheet": "Close Look history",
  "people-sheet": "Close People",
  "routines-sheet": "Close Routines",
  "queue-sheet": "Close Queue",
  "capture-sheet": "Close Capture",
  "search-sheet": "Close Search",
  "memory-sheet": "Close Memory",
  "today-sheet": "Close Today",
  "conversation-sheet": "Close Conversation",
  "devices-sheet": "Close Devices",
  "activity-sheet": "Close Now",
  "mission-sheet": "Close In flight",
  "inbox-sheet": "Close Inbox",
  "settings-sheet": "Close This iPhone",
  "camera-sheet": "Cancel camera",
};

function xButtons() {
  return html.split("\n").filter((line) => line.includes("sheet-close sheet-x"));
}

test("every reachable X is an inline SVG cross with a sheet-specific label", () => {
  const buttons = xButtons();
  assert.equal(buttons.length, 19);
  for (const [target, label] of Object.entries(LABELS)) {
    const line = buttons.find((b) => b.includes(`data-close="${target}"`));
    assert.ok(line, `X button exists for ${target}`);
    assert.ok(line.includes(SVG), `${target} X uses inline SVG`);
    assert.ok(!line.includes("✕"), `${target} X has no text glyph`);
    assert.ok(line.includes(`aria-label="${label}"`), `${target} label is "${label}"`);
  }
});

test("unreachable tactical sheet keeps its legacy glyph pending product decision", () => {
  const buttons = xButtons();
  const tactical = buttons.find((b) => b.includes('data-close="tactical-sheet"'));
  assert.ok(tactical, "tactical X still present");
  assert.ok(tactical.includes("✕"), "tactical keeps the legacy glyph");
  assert.ok(!tactical.includes("<svg"), "tactical has no SVG swap");
  assert.equal(html.split("✕").length - 1, 1);
});

test("X sits symmetric with the padding grid and tints for stuck-state legibility", () => {
  const quiet = css.match(/\.quiet-room \.sheet-x \{[^}]*\}/);
  assert.ok(quiet, "quiet X rule exists");
  assert.ok(quiet[0].includes("margin: 0 0 -44px 0"));
  assert.ok(!quiet[0].includes("-4px"), "no off-grid nudge remains");
  assert.ok(css.includes(".quiet-room .sheet-x { background: color-mix(in srgb, var(--ink) 8%, var(--elevated)); box-shadow: none; }"));
  const glyph = css.match(/\.sheet-x svg \{[^}]*\}/s);
  assert.ok(glyph, ".sheet-x svg rule exists");
  assert.ok(glyph[0].includes("width: 17px"));
  assert.ok(glyph[0].includes("stroke-width: 1.5"));
  assert.ok(glyph[0].includes("stroke: currentColor"));
});

test("X close skips the room glide under reduced motion", () => {
  assert.ok(source.includes("if (!heroReducedMotion()) stageReturn();"));
});

test("swipe fling commits instantly under reduced motion", () => {
  const fling = source.match(/if \(towardClose > CLOSE_AT \|\| flickTowardClose\) \{[^}]*if \(heroReducedMotion\(\)\) \{[^}]*showSheet\(cfg\.id, false\);/s);
  assert.ok(fling, "fling has an instant reduced-motion commit");
});

test("pull-to-refresh ignores touches starting on interactive elements", () => {
  const wire = source.match(/^function wireSessionPull\(\)[^]*?^}/m);
  assert.ok(wire, "wireSessionPull exists");
  assert.ok(wire[0].includes('ev.target.closest("button, input, a, textarea, select, .camera-ask, .choice-list")'));
});
