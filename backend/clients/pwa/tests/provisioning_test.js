// Phone-2 provisioning: ?pair= token extraction from the PWA boot URL.
// Run: node --test backend/clients/pwa/tests/provisioning_test.js
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

const match = source.match(/^function pairTokenFromSearch\([^]*?^}/m);
assert.ok(match, "Production function exists: pairTokenFromSearch");
const sandbox = { URLSearchParams };
vm.createContext(sandbox);
vm.runInContext(match[0] + "\nthis.pairTokenFromSearch = pairTokenFromSearch;", sandbox);
const pairTokenFromSearch = sandbox.pairTokenFromSearch;

test("extracts the pairing token", () => {
  assert.equal(pairTokenFromSearch("?pair=abc123"), "abc123");
});

test("trims whitespace", () => {
  assert.equal(pairTokenFromSearch("?pair=%20%20tok%20%20"), "tok");
});

test("ignores other params", () => {
  assert.equal(pairTokenFromSearch("?wake=1&pair=tok&x=2"), "tok");
});

test("missing or blank pair yields null", () => {
  assert.equal(pairTokenFromSearch(""), null);
  assert.equal(pairTokenFromSearch("?wake=1"), null);
  assert.equal(pairTokenFromSearch("?pair="), null);
  assert.equal(pairTokenFromSearch("?pair=%20%20"), null);
});

test("garbage input never throws", () => {
  assert.equal(pairTokenFromSearch(null), null);
  assert.equal(pairTokenFromSearch(undefined), null);
  assert.equal(pairTokenFromSearch("%"), null);
});
