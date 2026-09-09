/* Real renderer/lifecycle with an instrumented canvas and frame clock.
   No browser, image synthesis, network or microphone. Pixel QA is separate. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname, "../presence.js"), "utf8");

function fixture() {
  const frames = new Map(), events = {}, listeners = {}, commands = [];
  let frameId = 0, intersection;
  const gradient = () => ({ addColorStop() {} });
  const ctx = new Proxy({ createRadialGradient: gradient }, {
    get(target, key) { return key in target ? target[key] : (...args) => commands.push([key, ...args]); },
    set(target, key, value) { target[key] = value; return true; },
  });
  const canvas = {
    dataset: {}, clientWidth: 320, clientHeight: 220, width: 0, height: 0,
    getContext: () => ctx,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 320, height: 220 }),
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  const document = {
    documentElement: {}, visibilityState: "visible",
    addEventListener: (name, fn) => { events[name] = fn; },
    createElement: () => ({ width: 0, height: 0, getContext: () => ctx }),
  };
  const world = {
    document, Math, Image: class { set src(_value) { this.onload(); } },
    getComputedStyle: () => ({ getPropertyValue: () => "#858a76" }),
    requestAnimationFrame: fn => { frames.set(++frameId, fn); return frameId; },
    cancelAnimationFrame: id => frames.delete(id),
    IntersectionObserver: class { constructor(fn) { intersection = fn; } observe() {} },
    ResizeObserver: class { observe() {} },
    matchMedia: () => ({ addEventListener() {} }), devicePixelRatio: 2,
  };
  world.window = world;
  vm.createContext(world);
  vm.runInContext(code, world);
  const presence = new world.EviePresence(canvas);
  function frame(now) {
    const ready = [...frames.values()]; frames.clear();
    ready.forEach(fn => fn(now));
  }
  return { presence, canvas, commands, frames, frame, events, listeners, document,
    intersect: on => intersection([{ isIntersecting: on }]) };
}

test("idle clock advances in real time at both 60Hz and 120Hz", () => {
  for (const hz of [60, 120]) {
    const f = fixture(); f.presence.start();
    for (let i = 0; i <= hz * 5; i++) f.frame(1000 + i * 1000 / hz);
    assert.ok(Math.abs(f.presence.t - 5) < .05, `clock ${f.presence.t} at ${hz}Hz`);
    assert.equal(f.frames.size, 1, "only one loop");
    assert.equal(f.canvas.dataset.motion, "running");
  }
});

test("breath, floating turn and internal light vary without a voice session", () => {
  const f = fixture();
  f.commands.length = 0; f.presence.t = 0; f.presence.draw();
  const initial = f.commands.filter(row => ["translate", "rotate", "scale", "bezierCurveTo"].includes(row[0]));
  f.commands.length = 0; f.presence.t = 1.7; f.presence.draw();
  const later = f.commands.filter(row => ["translate", "rotate", "scale", "bezierCurveTo"].includes(row[0]));
  assert.notDeepEqual(initial, later);
  const transforms = later.filter(row => row[0] === "translate");
  const original = initial.filter(row => row[0] === "translate");
  assert.ok(Math.abs(transforms[1][2] - original[1][2]) > 12, "visible float in device pixels");
  assert.equal(f.presence.state, "idle");
  assert.equal(f.presence.targetAmp, 0, "decorative motion does not fabricate microphone input");
});

test("reduced motion freezes every transform and disables touch energy", () => {
  const f = fixture(); f.presence.start(); f.presence.setReduced(true);
  f.commands.length = 0; f.presence.t = 1; f.presence.draw();
  const first = JSON.stringify(f.commands);
  f.commands.length = 0; f.presence.t = 20; f.presence.draw();
  assert.equal(JSON.stringify(f.commands), first);
  f.listeners.pointerdown();
  assert.equal(f.presence.touchEnergy, 0);
  assert.equal(f.frames.size, 0);
});

test("offscreen, modal and background pause; resuming does not jump", () => {
  const f = fixture(); f.presence.start(); f.frame(1000); f.frame(1040);
  const before = f.presence.t;
  f.intersect(false); assert.equal(f.frames.size, 0);
  f.intersect(true); f.frame(100000);
  assert.equal(f.presence.t, before);
  f.presence.setPaused(true); assert.equal(f.frames.size, 0);
  f.presence.setPaused(false); assert.equal(f.frames.size, 1);
  f.document.visibilityState = "hidden"; f.events.visibilitychange();
  assert.equal(f.frames.size, 0);
  f.document.visibilityState = "visible"; f.events.visibilitychange();
  assert.equal(f.frames.size, 1);
});

test("touch response eases away and repeated state updates do not multiply loops", () => {
  const f = fixture(); f.presence.start();
  f.listeners.pointermove({ clientX: 300, clientY: 80 });
  f.listeners.pointerdown(); f.frame(1000); f.frame(1040);
  assert.ok(f.presence.pointer.x > 0);
  assert.ok(f.presence.touchEnergy > 0 && f.presence.touchEnergy < 1);
  f.listeners.pointerup();
  for (let i = 0; i < 100; i++) { f.presence.setState("idle"); f.frame(1080 + i * 40); }
  assert.ok(f.presence.touchEnergy < .001);
  assert.ok(Math.abs(f.presence.pointer.x) < .001);
  assert.equal(f.frames.size, 1);
});

test("a failed Talk connection does not freeze decorative breathing", () => {
  const f = fixture(); f.presence.setState("error");
  f.frame(1000); f.frame(1040);
  assert.ok(f.presence.t > 0);
  assert.equal(f.frames.size, 1);
  assert.equal(f.presence.targetAmp, 0);
});
