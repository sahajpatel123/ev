// Focused source/production-render checks; no live microphone or network.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

test("four home controls have no visible words and retain accessible names", () => {
  for (const [selector, label] of [[/data-surface="conversation"/, "Conversation thread"], [/id="talk"/, "Talk to Evie"], [/id="type-btn"/, "Write a thought"], [/id="more-btn"/, "Tools"]]) {
    const button = html.match(/<button\b[^]*?<\/button>/g).find(tag => selector.test(tag));
    assert.ok(button);
    assert.ok(button.includes(`aria-label="${label}"`));
    assert.equal(button.replace(/<[^>]+>/g, "").trim(), "");
    if (label !== "Talk to Evie") assert.match(button, /<svg[^>]*aria-hidden="true"/);
  }
  assert.match(html, /id="type-btn"[^>]*aria-controls="text-form"[^>]*aria-expanded="false"/);
  assert.match(html, /id="more-btn"[^>]*aria-haspopup="dialog"[^>]*aria-controls="more-sheet"/);
});

test("real paintLive keeps Talk icon-only and names Stop during connection and active voice", () => {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: "", setAttribute(k, v) { this[k] = v; }, dataset: {} });
    return nodes.get(id);
  };
  const state = { deviceToken: "fixture", conn: "READY", ui: "READY", talking: false, _talkInflight: false };
  const context = { $, state, textOf() {}, syncQuietRoom() {}, setMood() {} };
  vm.createContext(context);
  vm.runInContext(source.match(/^function paintLive\([^]*?^}/m)[0], context);
  for (const [talking, pending, label] of [[false, false, "Talk to Evie"], [false, true, "Stop talking"], [true, false, "Stop talking"], [false, false, "Talk to Evie"]]) {
    state.talking = talking; state._talkInflight = pending;
    context.paintLive();
    assert.equal($("talk").textContent, "");
    assert.equal($("talk")["aria-label"], label);
    assert.equal($("talk").title, label);
    assert.equal($("talk").disabled, false);
  }
  assert.doesNotMatch(source, /\$\("talk"\)\.textContent\s*=\s*"(?:Talk|Stop)"/);
});

test("icon targets remain spacious with a centered microphone and distinct Stop", () => {
  assert.match(css, /\.quiet-room \.room-talk \{[^}]*min-width: 56px;[^}]*min-height: 56px;[^}]*place-items: center/);
  assert.match(css, /\.quiet-room \.room-icon \{[^}]*min-width: 44px;[^}]*height: 44px/);
  assert.match(css, /\.quiet-room \.room-talk::before \{[^}]*margin: 0/);
  assert.match(css, /\[data-session="active"\] \.room-talk::before \{[^}]*-webkit-mask: none; mask: none/);
});
