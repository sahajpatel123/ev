// Stuck-Thinking recovery: unknown live error codes always surface a caption.
// Run: node --test backend/clients/pwa/tests/live_error_caption_test.js
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

const match = source.match(/^function defaultLiveErrorCaption\([^]*?^}/m);
assert.ok(match, "Production function exists: defaultLiveErrorCaption");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(match[0] + "\nthis.defaultLiveErrorCaption = defaultLiveErrorCaption;", sandbox);
const caption = sandbox.defaultLiveErrorCaption;

test("prefers the server message", () => {
  assert.equal(caption("turn_suppressed", "Still answering — say it again."), "Still answering — say it again.");
});

test("names an unknown code when there is no message", () => {
  assert.equal(caption("no_responder", ""), "Voice hiccup (no_responder). Try again.");
  assert.equal(caption("weird_code", null), "Voice hiccup (weird_code). Try again.");
});

test("never returns empty", () => {
  assert.equal(caption("", ""), "Voice hiccup. Try again.");
  assert.equal(caption(null, null), "Voice hiccup. Try again.");
  assert.ok(caption(undefined, undefined).length > 0);
});
