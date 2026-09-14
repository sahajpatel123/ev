/* Run: node --test backend/clients/pwa/tests/webrtc_lifecycle_test.js
   Real EvieWebRTC entry point; isolated VM, fake timers/media/network only. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "..", "webrtc.js"), "utf8");
const SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=sendrecv\r\n"
  + "a=rtpmap:111 opus/48000/2\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n";
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
async function flush() { for (let i = 0; i < 80; i++) await Promise.resolve(); }
function track() {
  return { kind: "audio", enabled: true, readyState: "live", stops: 0,
    stop() { this.stops++; this.readyState = "ended"; } };
}
function stream(t = track()) { return { getTracks: () => [t], getAudioTracks: () => [t] }; }
class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  emit(type, ev = {}) {
    this["on" + type]?.(ev);
    for (const fn of this.listeners.get(type) || []) fn(ev);
  }
  count() { return [...this.listeners.values()].reduce((n, set) => n + set.size, 0); }
}
function fixture(options = {}) {
  const timers = new Map(), peers = [], requests = [], states = [];
  let timerId = 0, plays = 0;
  const setTimer = (fn, ms, repeat = false) => {
    const id = ++timerId;
    timers.set(id, { fn, ms, repeat });
    return id;
  };
  const mic = stream();
  class Peer extends Events {
    constructor() {
      super();
      this.connectionState = options.stage === "ice" ? "connecting" : "connected";
      this.iceConnectionState = options.stage === "ice" ? "checking" : "connected";
      this.remoteDescriptions = 0;
      peers.push(this);
    }
    createDataChannel() {
      this.dc = new Events();
      this.dc.readyState = options.stage === "dc" ? "connecting" : "open";
      this.dc.send = () => {};
      this.dc.close = () => { this.dc.readyState = "closed"; this.dc.emit("close"); };
      return this.dc;
    }
    addTrack(t) { this.track = t; }
    getSenders() { return [{ track: this.track }]; }
    createOffer() { return options.offer?.promise || Promise.resolve({ sdp: SDP }); }
    setLocalDescription(offer) {
      this.localDescription = offer;
      return options.local?.promise || Promise.resolve();
    }
    setRemoteDescription() {
      this.remoteDescriptions++;
      if (options.remote) return options.remote.promise;
      this.emit("connectionstatechange");
      if (options.stage !== "session") this.dc.emit("message", { data: JSON.stringify({ type: "session.created" }) });
      if (options.stage !== "remote") this.emit("track", { track: track(), streams: [] });
      return Promise.resolve();
    }
    getStats() { return Promise.resolve(new Map()); }
    close() { this.connectionState = "closed"; this.emit("connectionstatechange"); }
  }
  const audio = {
    srcObject: null, pause() {}, setAttribute() {},
    play() {
      plays++;
      // First call is the source-less best-effort activation attempt.
      if (plays === 2 && options.playback) return options.playback.promise;
      if (plays === 3 && options.enable) return options.enable.promise;
      return Promise.resolve();
    },
  };
  const context = vm.createContext({
    module: { exports: {} }, console, AbortController, TextEncoder,
    isSecureContext: true,
    navigator: { userActivation: options.activation === undefined ? { isActive: false } : options.activation,
      mediaDevices: { getUserMedia: () => options.mic?.promise || Promise.resolve(mic) } },
    RTCPeerConnection: Peer,
    MediaStream: class { constructor(tracks) { this.tracks = tracks; } },
    fetch() { throw new Error("Unexpected network access in lifecycle test"); },
    setTimeout: (fn, ms) => setTimer(fn, ms), clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => setTimer(fn, ms, true), clearInterval: id => timers.delete(id),
  });
  context.window = context;
  vm.runInContext(source, context, { filename: "webrtc.js" });
  const rtc = new context.module.exports.EvieWebRTC({ audioEl: audio,
    onState: state => states.push(state), onHealth() {},
    api(url, opts) {
      if (url.endsWith("/heartbeat")) return Promise.resolve({});
      requests.push({ url, opts });
      assert.match(url, /\/live\/webrtc\/sdp$/);
      return options.signaling?.promise || Promise.resolve({ sdp: SDP });
    },
  });
  function start() {
    const outcome = { settled: false };
    outcome.promise = rtc.start({ session_id: "fixture", client_generation: 7 }).then(
      value => Object.assign(outcome, { settled: true, value }),
      error => Object.assign(outcome, { settled: true, error })
    );
    return outcome;
  }
  function fire(ms) {
    for (const [id, timer] of [...timers]) {
      if (timer.ms !== ms || !timers.has(id)) continue;
      if (!timer.repeat) timers.delete(id);
      timer.fn();
    }
  }
  return { rtc, mic, audio, peers, requests, states, timers, start, fire, options };
}
async function cancelled(f, outcome) {
  f.rtc.stop();
  await flush();
  assert.equal(outcome.settled, true, "stop must settle start without waiting for external completion");
  assert.equal(outcome.error?.name, "AbortError");
  assert.equal(outcome.error.cancelled, true);
  assert.equal(f.rtc.runtime, "ENDED", "cancellation must not become FAILED");
  assert.equal(f.timers.size, 0, "stop must remove startup and background timers");
  for (const pc of f.peers) {
    assert.equal(pc.count(), 0);
    assert.equal(pc.dc.count(), 0);
  }
}

test("pending mic cancels immediately and a late stream is stopped", async () => {
  const mic = deferred(), f = fixture({ mic }), outcome = f.start();
  await cancelled(f, outcome);
  const late = stream();
  mic.resolve(late);
  await flush();
  assert.equal(late.getTracks()[0].readyState, "ended");
  assert.equal(f.peers.length, 0);
  assert.equal(f.rtc.mic, null);
});

test("late mic cannot attach to a new attempt with the same server generation", async () => {
  const old = deferred(), next = deferred(), f = fixture({ mic: old });
  const first = f.start();
  f.options.mic = next;
  const second = f.start();
  await flush();
  assert.equal(first.error?.name, "AbortError");
  const late = stream();
  old.resolve(late);
  await flush();
  assert.equal(late.getTracks()[0].readyState, "ended");
  assert.equal(f.peers.length, 0);
  assert.equal(second.settled, false);
  await cancelled(f, second);
  next.reject(new Error("late permission failure"));
  await flush();
});

test("mic ownership is cleaned when Stop runs between promise continuations", async () => {
  const mic = deferred(), f = fixture({ mic }), outcome = f.start(), late = stream();
  mic.resolve(late);
  queueMicrotask(() => f.rtc.stop());
  await flush();
  assert.equal(outcome.error?.name, "AbortError");
  assert.equal(late.getTracks()[0].readyState, "ended");
  assert.equal(f.peers.length, 0);
  assert.equal(f.rtc.mic, null);
});

test("signaling abort settles even when the API ignores its signal", async () => {
  const signaling = deferred(), f = fixture({ signaling }), outcome = f.start();
  await flush();
  assert.equal(f.requests.length, 1);
  const pc = f.peers[0];
  await cancelled(f, outcome);
  assert.equal(f.requests[0].opts.signal.aborted, true);
  assert.equal(f.mic.getTracks()[0].readyState, "ended");
  signaling.resolve({ sdp: SDP });
  await flush();
  assert.equal(pc.remoteDescriptions, 0);
  assert.equal(f.rtc.runtime, "ENDED");
});

for (const stage of ["offer", "local", "remote"]) {
  test("stop settles pending " + stage + " description work", async () => {
    const pending = deferred(), f = fixture({ [stage]: pending }), outcome = f.start();
    await flush();
    assert.equal(outcome.settled, false);
    await cancelled(f, outcome);
    pending.resolve(stage === "offer" ? { sdp: SDP } : undefined);
    await flush();
    assert.equal(f.rtc.runtime, "ENDED");
  });
}

for (const stage of ["ice", "dc", "session", "remote"]) {
  test("stop settles " + stage + " wait and removes original object listeners", async () => {
    const f = fixture({ stage }), outcome = f.start();
    await flush();
    assert.equal(outcome.settled, false);
    if (stage === "ice") assert.equal(f.peers[0].count(), 2);
    if (stage === "dc") assert.equal(f.peers[0].dc.count(), 2);
    await cancelled(f, outcome);
    f.fire(20000);
    f.fire(12000);
    assert.equal(f.rtc.runtime, "ENDED");
  });
}

test("successful startup reports actual activation and resolved playback", async () => {
  const f = fixture(), outcome = f.start();
  await flush();
  assert.equal(outcome.settled, true);
  assert.equal(outcome.error, undefined);
  assert.equal(f.rtc.diag.stages.M00.gesture, false);
  assert.equal(f.rtc.diag.stages.M00.status, "not_observed");
  assert.equal(f.rtc.diag.stages.M20.status, "pass");
  assert.equal(f.rtc.diag.stages.M21.status, "pass");
  assert.equal(f.rtc.runtime, "VOICE_READY");
  assert.equal(f.rtc.snapshot().health.ready, true);
  f.rtc.stop();
  assert.equal(f.timers.size, 0);
});

test("unavailable userActivation is unknown; observed activation passes", async () => {
  for (const activation of [null, { isActive: true }]) {
    const f = fixture({ activation }), outcome = f.start();
    await flush();
    assert.equal(outcome.error, undefined);
    assert.equal(f.rtc.diag.stages.M00.gesture, activation ? true : null);
    assert.equal(f.rtc.diag.stages.M00.status, activation ? "pass" : "not_observed");
    f.rtc.stop();
  }
});

test("pending playback cannot pass readiness and its late completion is inert after stop", async () => {
  const playback = deferred(), f = fixture({ playback }), outcome = f.start();
  await flush();
  assert.equal(outcome.settled, false);
  assert.equal(f.rtc.diag.stages.M20.status, "pending");
  assert.equal(f.rtc.diag.stages.M21.status, "pending");
  assert.equal(f.rtc.snapshot().health.ready, false);
  await cancelled(f, outcome);
  playback.resolve();
  await flush();
  assert.equal(f.rtc.playbackReady, false);
  assert.equal(f.rtc.diag.stages.M20.status, "pending");
});

test("blocked playback preserves connection for Enable audio and then becomes ready", async () => {
  const playback = deferred(), f = fixture({ playback }), outcome = f.start();
  await flush();
  playback.reject(Object.assign(new Error("gesture required"), { name: "NotAllowedError" }));
  await flush();
  f.fire(80);
  await flush();
  assert.equal(outcome.error?.audio_blocked, true);
  assert.equal(f.rtc.closed, false);
  assert.equal(f.rtc.diag.stages.M20.status, "fail");
  assert.equal(f.rtc.diag.stages.M21.status, "pending");
  await f.rtc.enableAudio();
  assert.equal(f.rtc.diag.failed_stage, null);
  assert.equal(f.rtc.runtime, "VOICE_READY");
  assert.equal(f.rtc.diag.stages.M20.status, "pass");
  f.rtc.stop();
});

test("playback timeout is recoverable and an old play rejection cannot undo Enable audio", async () => {
  const playback = deferred(), f = fixture({ playback }), outcome = f.start();
  await flush();
  f.fire(12000);
  await flush();
  assert.equal(outcome.error?.audio_blocked, true);
  await f.rtc.enableAudio();
  playback.reject(new Error("old playback failed"));
  await flush();
  assert.equal(f.rtc.playbackReady, true);
  assert.equal(f.rtc.playBlocked, null);
  f.rtc.stop();
});

test("Stop settles pending Enable audio without reviving readiness", async () => {
  const playback = deferred(), enable = deferred(), f = fixture({ playback, enable });
  f.start();
  await flush();
  f.fire(12000);
  await flush();
  const enabling = f.rtc.enableAudio();
  const rejected = assert.rejects(enabling, { name: "AbortError" });
  f.rtc.stop();
  await rejected;
  enable.resolve();
  playback.resolve();
  await flush();
  assert.equal(f.rtc.playbackReady, false);
  assert.equal(f.rtc.runtime, "ENDED");
});

test("late playback from a replaced attempt cannot mark the new attempt ready", async () => {
  const playback = deferred(), nextMic = deferred(), f = fixture({ playback });
  const first = f.start();
  await flush();
  const oldDc = f.peers[0].dc;
  f.options.mic = nextMic;
  const second = f.start();
  await flush();
  assert.equal(first.error?.name, "AbortError");
  playback.resolve();
  oldDc.emit("message", { data: JSON.stringify({ type: "session.created" }) });
  await flush();
  assert.equal(f.rtc.playbackReady, false);
  assert.equal(f.rtc.sessionCreated, false);
  assert.equal(f.rtc.diag.stages.M20.status, "pending");
  await cancelled(f, second);
});

test("playback resolving after its timeout clears the recovered M20 diagnostic", async () => {
  const playback = deferred(), f = fixture({ playback }), outcome = f.start();
  await flush();
  f.fire(12000);
  await flush();
  assert.equal(outcome.error?.audio_blocked, true);
  playback.resolve();
  await flush();
  assert.equal(f.rtc.diag.failed_stage, null);
  assert.equal(f.rtc.diag.stages.M20.status, "pass");
  assert.equal(f.rtc.runtime, "VOICE_READY");
  f.rtc.stop();
});
