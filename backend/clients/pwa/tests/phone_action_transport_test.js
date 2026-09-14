// Production event handlers with simulated transport boundaries only.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
function fixture() {
  const seen = [], nodes = {};
  const context = {
    state: { sessionGen: 9 }, engine: null,
    $: id => nodes[id] || (nodes[id] = {}),
    textOf: (node, value) => { node.textContent = value; },
    setMood() {}, pushHistory() {}, render() {}, pushActivity() {},
    showHomeStationResult: () => false,
    window: { EvieMobileActions: {
      present: payload => seen.push(["action", payload]),
      presentFromHud: payload => {
        const card = payload.hud || payload;
        if (card.kind !== "phone_action") return false;
        seen.push(["action", card]); return true;
      },
      onTranscript: text => seen.push(["transcript", text]),
    } },
  };
  vm.createContext(context);
  for (const name of ["handlePhoneHud", "handleLiveMessage"]) {
    const match = source.match(new RegExp("^(?:async )?function " + name + "\\([^]*?^}", "m"));
    if (match) vm.runInContext(match[0], context);
  }
  return { context, seen, deliver: (payload, generation = 9) => context.handleLiveMessage(generation, { data: JSON.stringify(payload) }) };
}
test("PCM voice delivers the server phone-action HUD instead of dropping it", async () => {
  const f = fixture();
  await f.deliver({ type: "hud", kind: "phone_action", hud: { kind: "phone_action", action_id: "timer-1", title: "Timer", status: "authorized" } });
  assert.equal(f.seen.length, 1);
  assert.equal(f.seen[0][1].action_id, "timer-1");
});
test("PCM final transcript reaches the confirmation handler", async () => {
  const f = fixture(); await f.deliver({ type: "final_transcript", text: "yes" });
  assert.deepEqual(f.seen, [["transcript", "yes"]]);
});
test("stale transport cannot present actions or confirm newer ones", async () => {
  const f = fixture();
  await f.deliver({ type: "hud", hud: { kind: "phone_action", action_id: "old" } }, 8);
  await f.deliver({ type: "final_transcript", text: "yes" }, 8);
  assert.equal(f.seen.length, 0);
});
test("shared handler unwraps WebRTC action results once and tolerates malformed HUD", () => {
  const f = fixture();
  assert.equal(typeof f.context.handlePhoneHud, "function");
  f.context.handlePhoneHud(null);
  f.context.handlePhoneHud({ kind: "result", phone_action: { action_id: "maps-1", card: { kind: "phone_action" } } });
  assert.equal(f.seen.length, 1);
  assert.equal(f.seen[0][1].action_id, "maps-1");
});
