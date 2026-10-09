/* Run: node --test backend/clients/pwa/tests/webrtc_lease_lost_test.js
   A 409 from /live/events means the server-side lease is dead (fenced or
   generation-bumped). The poller must stop and signal recovery instead of
   spinning on the dead lease every 280ms. Production showed repeated 409s
   with a stale client_generation while the poller never recovered. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "..", "webrtc.js"), "utf8");
async function flush() { for (let i = 0; i < 80; i++) await Promise.resolve(); }

function fixture(apiImpl) {
  const timers = new Map();
  let timerId = 0;
  const states = [];
  const context = vm.createContext({
    module: { exports: {} },
    console,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
  });
  context.window = context;
  vm.runInContext(source, context, { filename: "webrtc.js" });
  const rtc = new context.module.exports.EvieWebRTC({ api: apiImpl, onState: (s) => states.push(s) });
  rtc.sessionId = "sess-1";
  rtc.leaseId = "lease-1";
  rtc.generation = 1;
  rtc.closed = false;
  rtc._attempt = {};
  function fire() {
    for (const [id, t] of [...timers]) {
      if (!timers.has(id)) continue;
      timers.delete(id);
      t.fn();
    }
  }
  return { rtc, states, timers, fire };
}

test("live-events 409 stops the poller and signals lease_lost", async () => {
  const f = fixture(async (url) => {
    if (String(url).includes("/live/events")) {
      const err = new Error("conflict");
      err.status = 409;
      throw err;
    }
    return {};
  });
  f.rtc._pollEvents();
  await flush();
  f.fire();
  await flush();
  assert.ok(f.states.includes("lease_lost"), "expected lease_lost, got " + JSON.stringify(f.states));
  assert.equal(f.timers.size, 0, "dead lease must not reschedule polls");
});

test("reconnect_required poll event signals recovery and keeps polling", async () => {
  const f = fixture(async (url) => {
    if (String(url).includes("/live/events")) {
      return { events: [{ type: "reconnect_required", code: "PHONE_SESSION_CHANGED" }] };
    }
    return {};
  });
  f.rtc._pollEvents();
  await flush();
  f.fire();
  await flush();
  assert.ok(f.states.includes("reconnect_required"), "expected reconnect_required, got " + JSON.stringify(f.states));
  assert.equal(f.timers.size, 1, "event-driven recovery must not stop the poller");
});

test("transient poll errors stay best-effort and reschedule", async () => {
  const f = fixture(async () => { throw new Error("boom"); });
  f.rtc._pollEvents();
  await flush();
  f.fire();
  await flush();
  assert.ok(!f.states.includes("lease_lost"));
  assert.equal(f.timers.size, 1, "transient failure must keep polling");
});
