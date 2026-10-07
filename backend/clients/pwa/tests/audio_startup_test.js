/* Talk-startup hang regression: every audio await behind the mic button must
   settle. Run: node backend/clients/pwa/tests/audio_startup_test.js
   (also wired into backend/tests/test_pwa_audio.py). */
const audio = require("../audio.js");
const assert = require("assert");

const never = () => new Promise(() => {});
const elapsedOk = (start, budgetMs) => {
  const ms = Date.now() - start;
  assert.ok(ms < budgetMs, `settled in ${ms}ms, budget ${budgetMs}ms`);
  return ms;
};
// Outer guard: a regressed (unbounded) await must FAIL, never hang the runner.
const mustSettle = (promise, ms, label) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error(label + " hung")), ms);
  Promise.resolve(promise).then(
    (value) => { clearTimeout(timer); resolve(value); },
    (err) => { clearTimeout(timer); reject(err); },
  );
});

async function main() {
  assert.strictEqual(typeof audio.withTimeout, "function", "withTimeout exported");
  assert.strictEqual(typeof audio.resumeBounded, "function", "resumeBounded exported");
  assert.ok(audio.STARTUP_RESUME_MS > 0 && audio.STARTUP_RESUME_MS <= 5000, "resume budget sane");
  assert.ok(audio.STARTUP_WORKLET_MS > 0 && audio.STARTUP_WORKLET_MS <= 5000, "worklet budget sane");

  // Fast path resolves with the inner value.
  assert.strictEqual(await mustSettle(audio.withTimeout(Promise.resolve(7), 50, "fast"), 2000, "fast"), 7);

  // Never-settling promise rejects at the budget (the iOS resume stall).
  let start = Date.now();
  await assert.rejects(mustSettle(audio.withTimeout(never(), 25, "stalled"), 2000, "stall"),
    (err) => err && err.code === "audio_startup_timeout" && err.message === "stalled");
  elapsedOk(start, 1500);

  // Inner rejection still wins before the budget.
  await assert.rejects(mustSettle(audio.withTimeout(Promise.reject(new Error("boom")), 1000), 2000, "reject"),
    /boom/);

  // Running context: no resume call, resolves the context itself.
  const running = { state: "running", resume: () => { throw new Error("must not resume"); } };
  assert.strictEqual(await mustSettle(audio.resumeBounded(running, 25), 2000, "running"), running);

  // Suspended context with resolving resume: resolves the context.
  const resumable = { state: "suspended", resume: () => Promise.resolve() };
  assert.strictEqual(await mustSettle(audio.resumeBounded(resumable, 100), 2000, "resumable"), resumable);

  // Suspended context with stalled resume: rejects fast, never hangs.
  const stalled = { state: "suspended", resume: () => never() };
  start = Date.now();
  await assert.rejects(mustSettle(audio.resumeBounded(stalled, 25), 2000, "stalled-resume"),
    /audio_context_suspended/);
  elapsedOk(start, 1500);

  // ensure() on a stalled context rejects (production budget) instead of
  // hanging the tap on "Connecting microphone…" forever.
  const engine = new audio.EvieAudioPlaybackEngine();
  engine.ctx = { state: "suspended", resume: () => never() };
  start = Date.now();
  await assert.rejects(mustSettle(engine.ensure(), 8000, "ensure"), /audio_context_suspended/);
  elapsedOk(start, 7000);

  // Stalled playback-worklet fetch falls back instead of hanging ensure().
  const fallback = new audio.EvieAudioPlaybackEngine();
  fallback.ctx = { audioWorklet: { addModule: () => never() } };
  start = Date.now();
  await mustSettle(fallback._attachWorklet(), 8000, "attachWorklet");
  elapsedOk(start, 7000);
  assert.strictEqual(fallback.backend, "scheduled-buffer-fallback");

  console.log("audio_startup_ok");
}

main().then(
  () => {},
  (err) => { console.error("audio_startup_fail", err); process.exit(1); },
);
