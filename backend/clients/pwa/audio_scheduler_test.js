const audio = require("./audio.js");
const assert = require("assert");

const times = [];
let queued = 0;
for (let i = 0; i < 8; i += 1) {
  const plan = audio.nextPlayTime(0, queued, 0.02, 0.1);
  times.push(plan.start);
  queued = plan.queuedUntil;
}
assert.strictEqual(times[0], 0.1);
for (let i = 1; i < times.length; i += 1) {
  assert.ok(times[i] > times[i - 1], "chunks must not share a start time");
  assert.ok(Math.abs(times[i] - times[i - 1] - 0.02) < 1e-9);
}

function sine(n, rate, hz, amp) {
  const out = new Float32Array(n);
  for (let i = 0; i < n; i += 1) out[i] = Math.sin((2 * Math.PI * hz * i) / rate) * (amp || 0.5);
  return out;
}

const srcRate = 16000;
const dstRate = 48000;
const src = sine(1600, srcRate, 440, 0.5);
const chunk = 320;
const naive = [];
for (let off = 0; off < src.length; off += chunk) {
  const slice = src.subarray(off, off + chunk);
  const pcm = new Int16Array(slice.length);
  for (let i = 0; i < slice.length; i += 1) pcm[i] = Math.round(slice[i] * 32767);
  const converted = audio.pcm16ToFloat32(new Uint8Array(pcm.buffer), srcRate, dstRate);
  for (let i = 0; i < converted.length; i += 1) naive.push(converted[i]);
}
const stateful = new audio.LinearResampler(srcRate, dstRate);
const good = [];
for (let off = 0; off < src.length; off += chunk) {
  const part = stateful.process(src.subarray(off, off + chunk));
  for (let i = 0; i < part.length; i += 1) good.push(part[i]);
}
const naiveJump = audio.maxAdjacentJump(naive);
const goodJump = audio.maxAdjacentJump(good);
assert.ok(good.length > 4000, "stateful resampler should emit continuous 48k frames");
assert.ok(goodJump < 0.12, "stateful resampler should not click at 20ms boundaries, got " + goodJump);
assert.ok(naiveJump > goodJump, "per-chunk resampler must be worse than stateful");

const ring = new audio.RingBuffer(64);
ring.push(new Float32Array([1, 2, 3, 4]));
assert.strictEqual(ring.available(), 4);
const taken = ring.shift(2);
assert.strictEqual(taken[0], 1);
assert.strictEqual(ring.available(), 2);

const jitter = new audio.JitterController();
for (let i = 0; i < 20; i += 1) jitter.noteArrival(i * 40, 20);
jitter.locked = false;
const target = jitter.adapt(true);
assert.ok(target >= audio.JITTER_MIN_S && target <= audio.JITTER_MAX_S);
jitter.locked = true;
const locked = jitter.targetS;
jitter.adapt(false);
assert.strictEqual(jitter.targetS, locked);

const engine = new audio.EvieAudioPlaybackEngine();
engine.socketGeneration = 2;
engine.beginTurn({ responseId: "r1", socketGeneration: 2 });
const ok = engine._accept({ seq: 1, socketGeneration: 2, responseId: "r1" });
const dup = engine._accept({ seq: 1, socketGeneration: 2, responseId: "r1" });
const ooo = engine._accept({ seq: 0, socketGeneration: 2, responseId: "r1" });
const stale = engine._accept({ seq: 2, socketGeneration: 9, responseId: "r1" });
assert.strictEqual(ok, true);
assert.strictEqual(dup, false);
assert.strictEqual(ooo, false);
assert.strictEqual(stale, false);
assert.ok(engine.metrics.duplicateDropped >= 1);
assert.ok(engine.metrics.outOfOrderDropped >= 1);
assert.ok(engine.metrics.staleDropped >= 1);
assert.strictEqual(audio.AUDIO_ENGINE_VERSION, "4");
assert.ok(audio.PRIME_S >= 0.2, "cold prime must cover WAN jitter, got " + audio.PRIME_S);

// Tool-continuation adopt: a new response id while audio is still draining
// must NOT flush (no generation bump) and must reset the seq dedup map, or
// the restarted chunk counter reads as duplicates and the post-tool answer
// goes silent / chops mid-word.
const cont = new audio.EvieAudioPlaybackEngine();
cont.socketGeneration = 2;
cont.beginTurn({ responseId: "pre", socketGeneration: 2 });
const genBefore = cont.generation;
assert.strictEqual(cont._accept({ seq: 0, socketGeneration: 2, responseId: "pre" }), true);
assert.strictEqual(cont._accept({ seq: 1, socketGeneration: 2, responseId: "pre" }), true);
cont.playing = true; // preamble still draining when continuation opens
assert.strictEqual(cont._accept({ seq: 0, socketGeneration: 2, responseId: "cont" }), true);
assert.strictEqual(cont.generation, genBefore, "adopt must not flush/bump generation");
assert.strictEqual(cont.responseId, "cont");
assert.strictEqual(cont._accept({ seq: 1, socketGeneration: 2, responseId: "cont" }), true);
assert.strictEqual(cont.metrics.duplicateDropped || 0, 0, "continuation seq must not read as duplicate");
// True new turn while idle still flushes via beginTurn.
cont.playing = false;
assert.strictEqual(cont._accept({ seq: 0, socketGeneration: 2, responseId: "next" }), true);
assert.ok(cont.generation > genBefore, "idle new id must begin a fresh turn");

const odd = new Uint8Array([0x00, 0x10, 0xFF]);
const first = audio.int16BytesToFloat32(odd);
assert.ok(first.samples.length === 1);
assert.strictEqual(first.remainder.length, 1);
const second = audio.int16BytesToFloat32(new Uint8Array([0x20]), first.remainder);
assert.strictEqual(second.remainder.length, 0);
assert.ok(second.samples.length === 1);

const tight = new audio.RingBuffer(8);
const dumped = tight.push(new Float32Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]));
assert.ok(dumped > 0);
assert.ok(tight.available() <= 7);

console.log("audio_scheduler_ok", {
  naiveJump: naiveJump.toFixed(4),
  goodJump: goodJump.toFixed(4),
  engine: audio.AUDIO_ENGINE_VERSION,
});
