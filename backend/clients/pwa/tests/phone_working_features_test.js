// Isolated production-function tests: no browser, microphone, network, or live DB.
// Run: node --test backend/clients/pwa/tests/phone_working_features_test.js
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

function extract(name) {
  const match = source.match(new RegExp("^(?:async )?function " + name + "\\([^]*?^}", "m"));
  assert.ok(match, "Production function exists: " + name);
  return match[0];
}

const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(
  [
    "digitalFollowPrompts",
    "turnOutcomeLine",
    "mergePeopleRows",
    "trustBannerCopy",
    "missionLines",
    "changedLines",
    "micRmsFloat32",
    "onsetDetectorPoll",
    "phoneAecActive",
  ].map(extract).join("\n"),
  sandbox
);

test("digest brief offers more-about follow-up, never a send chip", () => {
  const chips = sandbox.digitalFollowPrompts({
    manner: "digest",
    status: "COMPLETED_VERIFIED",
    sent: false,
    focus: { person: "Mansi", channel: "whatsapp" },
  });
  assert.equal(chips.some((c) => c.prompt === "more about that particular chat"), true);
  assert.equal(chips.some((c) => /Mansi/.test(c.label)), true);
  assert.equal(chips.some((c) => /send/i.test(c.prompt)), false);
});

test("prepared reroute requires an explicit approve-send chip", () => {
  const chips = sandbox.digitalFollowPrompts({
    manner: "reroute",
    status: "PREPARED",
    sent: false,
    focus: { person: "Mansi" },
    destination: "Rahul",
  });
  assert.equal(chips.some((c) => c.prompt === "approve send"), true);
  assert.equal(chips.some((c) => c.prompt === "do not send"), true);
});

test("turn outcome never claims a send happened", () => {
  const line = sandbox.turnOutcomeLine({
    route: "DIGITAL_OPS",
    digital: { kind: "phone_brief", manner: "digest", sent: false, focus: { person: "Puran" } },
  });
  assert.match(line, /not sent/);
  assert.match(line, /summary/);
  const home = sandbox.turnOutcomeLine({
    route_target: "HOME_STATION",
    queued: true,
    action_result: "QUEUED",
  });
  assert.match(home, /Home Station/);
  assert.match(home, /queued/);
});

test("people merge prefers phone snapshot then Home Station names", () => {
  const rows = sandbox.mergePeopleRows({
    contacts: [{ name: "Aarav" }],
    home: [{ name: "Aarav" }, { name: "Mansi" }],
  });
  const names = rows.map((r) => r.name).join(",");
  assert.equal(names, "Aarav,Mansi");
  assert.equal(rows[0].source, "phone");
  assert.equal(rows[1].source, "home");
  const callable = sandbox.mergePeopleRows({
    home: [{ name: "Mansi", callable: true, channels: ["whatsapp"] }],
  });
  assert.equal(callable[0].callable, true);
});

test("trust banner maps owner hello to trusted device copy", () => {
  const copy = sandbox.trustBannerCopy({ environment: "OWNER", status: { trust_state: "TRUSTED_OWNER_DEVICE" } });
  assert.equal(copy.tone, "good");
  assert.match(copy.label, /Trusted/);
});

test("mission lines stay objective-only", () => {
  const lines = sandbox.missionLines({
    mission: {
      now: { objective: "File the quote" },
      waiting: [{ objective: "Akash reply" }],
      needs_you: [{ title: "Approve send" }],
    },
  });
  assert.equal(lines[0], "Now · File the quote");
  assert.equal(sandbox.changedLines({ changes: [{ summary: "Calendar updated" }] })[0], "Calendar updated");
});

test("mic RMS measures real energy, silence reads zero", () => {
  assert.equal(sandbox.micRmsFloat32(new Float32Array([0, 0, 0, 0])), 0);
  assert.equal(sandbox.micRmsFloat32(null), 0);
  const level = sandbox.micRmsFloat32(new Float32Array([0.5, -0.5, 0.5, -0.5]));
  assert.ok(Math.abs(level - 0.5) < 1e-6);
});

test("onset confirms clear speech above playback bleed, once", () => {
  const det = { streak: 0, onsetAt: 0, cooldownUntil: 0 };
  assert.equal(sandbox.onsetDetectorPoll(det, 0.9, 0.4, true, 1000), null);
  assert.equal(sandbox.onsetDetectorPoll(det, 0.9, 0.4, true, 1100), null);
  const done = sandbox.onsetDetectorPoll(det, 0.9, 0.4, true, 1200);
  assert.ok(done);
  assert.ok(done.speechMs >= 160);
  assert.ok(done.confidence >= 0.5);
  assert.equal(sandbox.onsetDetectorPoll(det, 0.9, 0.4, true, 1300), null);
});

test("onset rejects bleed, broken streaks, silence, and idle playback", () => {
  const bleed = { streak: 0, onsetAt: 0, cooldownUntil: 0 };
  for (let i = 0; i < 6; i += 1) {
    assert.equal(sandbox.onsetDetectorPoll(bleed, 0.35, 0.5, true, 1000 + i * 100), null);
  }
  const broken = { streak: 0, onsetAt: 0, cooldownUntil: 0 };
  const levels = [0.9, 0.9, 0.05, 0.9, 0.9];
  levels.forEach((level, i) => {
    assert.equal(sandbox.onsetDetectorPoll(broken, level, 0.4, true, 1000 + i * 100), null);
  });
  const idle = { streak: 0, onsetAt: 0, cooldownUntil: 0 };
  assert.equal(sandbox.onsetDetectorPoll(idle, 0.95, 0.0, false, 1000), null);
});

test("phone AEC reports applied state, fails closed when unknown", () => {
  assert.equal(sandbox.phoneAecActive({ echoCancellation: true }), true);
  assert.equal(sandbox.phoneAecActive({ echoCancellation: false }), false);
  assert.equal(sandbox.phoneAecActive({}), false);
  assert.equal(sandbox.phoneAecActive(undefined), false);
  assert.equal(sandbox.phoneAecActive(null), false);
  assert.equal(
    sandbox.phoneAecActive({ source: "avaudioengine", sampleRate: 16000 }),
    false
  );
});
