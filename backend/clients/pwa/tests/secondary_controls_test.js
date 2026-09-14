// Isolated production-function tests: no browser, microphone, network, or live DB.
// Run: node --test backend/clients/pwa/tests/secondary_controls_test.js
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");
const names = ["textOf", "fillOl", "fillInbox", "markAllInboxRead", "refreshInbox", "refreshQueue",
  "refreshMemories", "openMemoryDetail", "submitCapture", "blobToBase64", "toggleVoiceNote",
  "evieVoiceNoteIsNormal", "releaseEvieVoiceNoteResources", "finishEvieVoiceNote",
  "stopEvieVoiceNoteSession", "cleanupEvieVoiceNote", "saveEvieVoiceNoteDraft",
  "conversationExportText", "copyConversation", "shareConversation", "resetLocal"];
const functions = names.map(name => {
  const match = source.match(new RegExp("^(?:async )?function " + name + "\\([^]*?^}", "m"));
  assert.ok(match, "Production function exists: " + name);
  return match[0];
}).join("\n");
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function harness() {
  const nodes = new Map();
  class Element {
    constructor(tag = "div") { this.tagName = tag; this.children = []; this.events = {}; this.attrs = {}; this.hidden = false; this.value = ""; this.disabled = false; this.isConnected = true; this._text = ""; }
    set id(value) { this._id = value; nodes.set(value, this); }
    get id() { return this._id; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    get firstChild() { return this.children[0]; }
    appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
    remove() { this.parentNode?.removeChild(this); nodes.delete(this.id); this.isConnected = false; }
    setAttribute(name, value) { this.attrs[name] = value; }
    getAttribute(name) { return this.attrs[name]; }
    addEventListener(name, action) { this.events[name] = action; }
    querySelector(selector) {
      return this.children.find(child => selector === "button.on" ? child.className === "on" : child.attrs.role === "status") || null;
    }
    focus() { context.document.activeElement = this; }
  }
  const node = id => {
    if (!nodes.has(id)) { const el = new Element(); el.id = id; }
    return nodes.get(id);
  };
  const ids = ["voice-note-btn", "voice-note-state", "capture-privacy", "capture-text", "capture-meta", "memory-list", "memory-detail", "memory-meta", "memory-detail-text", "memory-detail-meta", "memory-sources", "memory-versions", "memory-search-form", "conv-export-meta", "inbox-list", "inbox-ack-all-btn", "queue-list", "queue-meta", "history", "reply", "pair-token"];
  ids.forEach(node);
  const parent = new Element();
  parent.appendChild(node("inbox-list"));
  const selected = new Element("button");
  selected.className = "on";
  selected.setAttribute("data-privacy", "normal");
  node("capture-privacy").appendChild(selected);
  let serial = 0;
  const timers = new Map(), requests = [], recorders = [], streams = [], removed = [];
  class Recorder {
    static isTypeSupported() { return true; }
    constructor(stream) { this.stream = stream; this.mimeType = "audio/mp4"; this.state = "inactive"; recorders.push(this); }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      queueMicrotask(() => {
        this.ondataavailable?.({ data: new Blob(["recorded audio"]) });
        this.onstop?.();
      });
    }
  }
  const makeStream = () => {
    const track = { stopped: false, stop() { this.stopped = true; } };
    const stream = { track, getTracks: () => [track] };
    streams.push(stream);
    return stream;
  };
  const storage = new Map();
  const context = vm.createContext({
    console, Blob, URLSearchParams, Date, Promise, encodeURIComponent,
    state: { deviceToken: "device-A", accessToken: "access-A", device: { display_name: "Owner" }, history: [], inbox: [], queue: [] },
    document: { getElementById: id => nodes.get(id) || null, createElement: tag => new Element(tag), activeElement: node("memory-list"), querySelectorAll: () => [] },
    $: id => nodes.get(id) || null,
    crypto: { randomUUID: () => "uuid-" + (++serial) },
    navigator: { mediaDevices: { getUserMedia: async () => makeStream() }, clipboard: { writeText: async () => {} } },
    MediaRecorder: Recorder,
    FileReader: class { readAsDataURL() { this.result = "data:audio/mp4;base64,YXVkaW8="; queueMicrotask(() => this.onload()); } },
    setTimeout: (fn, ms) => { const id = ++serial; timers.set(id, { fn, ms }); return id; },
    clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id),
    localStorage: { removeItem: key => storage.delete(key) },
    sessionStorage: { setItem: (key, value) => storage.set(key, value) },
    idbDel: async key => { removed.push(key); storage.delete(key); },
    api: async (url, options) => { requests.push({ url, options }); return { ok: true }; },
    stopTalk: async () => { context.state.talking = false; context.state._talkInflight = false; },
    EviePhoneAlerts: { noticeInbox: () => {} },
    hello: async () => { context.hellos++; context.state.hello = { refreshed: true }; },
    hellos: 0, scheduleHelloRecheck: () => {}, render: () => {},
    setConn: value => { context.state.conn = value; }, setMood: value => { context.state.mood = value; },
  });
  context.window = context;
  vm.runInContext(source.match(/^const evieVoiceNoteLifecycle = .*$/m)[0] + '\nlet peopleCache = [], routineTimes = []; const OFFLINE_QUEUE_KEYS = "ev.offlineQueueKeys";\n' + functions, context);
  return { c: context, node, selected, timers, requests, recorders, streams, makeStream, storage, removed,
    lifecycle: () => vm.runInContext("evieVoiceNoteLifecycle", context) };
}

test("voice save releases tracks and timer; failed save retries identical blob/key", async () => {
  const h = harness();
  await h.c.toggleVoiceNote();
  assert.equal(h.timers.size, 1);
  h.c.api = async (url, options) => { h.requests.push({ url, options }); return { ok: h.requests.length > 1 }; };
  await h.c.toggleVoiceNote(); await settle();
  assert.ok(h.streams[0].track.stopped);
  assert.equal(h.timers.size, 0);
  const draft = h.lifecycle().draft;
  assert.ok(draft.blob.size);
  assert.match(h.node("voice-note-state").textContent, /kept.*retry/);
  await h.c.toggleVoiceNote();
  assert.equal(h.requests[0].options.body, h.requests[1].options.body);
  assert.equal(h.lifecycle().draft, null);
  assert.equal(h.recorders.length, 1);
});

test("double start and close fence late microphone permission, including a newer recording", async () => {
  const h = harness(), permission = deferred();
  let acquisitions = 0;
  h.c.navigator.mediaDevices.getUserMedia = () => { acquisitions++; return permission.promise; };
  const first = h.c.toggleVoiceNote();
  await h.c.toggleVoiceNote();
  assert.equal(acquisitions, 1);
  h.c.cleanupEvieVoiceNote();
  h.c.navigator.mediaDevices.getUserMedia = async () => h.makeStream();
  await h.c.toggleVoiceNote();
  const late = h.makeStream(); permission.resolve(late); await first;
  assert.ok(late.track.stopped);
  assert.equal(h.recorders.length, 1);
  assert.equal(h.lifecycle().active.phase, "recording");
  h.c.cleanupEvieVoiceNote(); await settle();
  assert.equal(h.requests.length, 0);
  assert.equal(h.timers.size, 0);
  assert.ok(h.lifecycle().draft.blob.size);
});

test("repeated cleanup waits for the final recorder data event and never uploads", async () => {
  const h = harness();
  await h.c.toggleVoiceNote();
  h.c.cleanupEvieVoiceNote();
  h.c.cleanupEvieVoiceNote();
  await settle();
  assert.ok(h.lifecycle().draft.blob.size);
  assert.equal(h.requests.length, 0);
  assert.equal(h.timers.size, 0);
});

test("private/sensitive audio refused, including privacy changed while acquiring; text still saves", async () => {
  const h = harness();
  for (const privacy of ["private", "sensitive"]) {
    h.selected.setAttribute("data-privacy", privacy);
    await h.c.toggleVoiceNote();
    assert.equal(h.streams.length, 0);
    assert.match(h.node("voice-note-state").textContent, /text note/);
    h.node("capture-text").value = privacy + " text";
    await h.c.submitCapture();
    assert.equal(JSON.parse(h.requests.at(-1).options.body).privacy_level, privacy);
  }
  h.selected.setAttribute("data-privacy", "normal");
  const permission = deferred();
  h.c.navigator.mediaDevices.getUserMedia = () => permission.promise;
  const start = h.c.toggleVoiceNote();
  h.selected.setAttribute("data-privacy", "private");
  const stream = h.makeStream(); permission.resolve(stream); await start;
  assert.ok(stream.track.stopped);
  assert.equal(h.recorders.length, 0);
});

test("privacy changed before stop retains audio without sending; timeout also stops tracks", async () => {
  const h = harness();
  await h.c.toggleVoiceNote();
  h.selected.setAttribute("data-privacy", "private");
  await h.c.toggleVoiceNote(); await settle();
  assert.equal(h.requests.length, 0);
  assert.ok(h.lifecycle().draft);
  assert.ok(h.streams[0].track.stopped);
  h.c.cleanupEvieVoiceNote({ discard: true });
  h.selected.setAttribute("data-privacy", "normal");
  await h.c.toggleVoiceNote();
  const timer = [...h.timers.values()][0];
  assert.equal(timer.ms, 90000);
  timer.fn(); await settle();
  assert.equal(h.timers.size, 0);
  assert.ok(h.streams[1].track.stopped);
});

test("recorder construction/start/stop/error failures release resources", async () => {
  for (const stage of ["constructor", "start", "stop", "error"]) {
    const h = harness(), Base = h.c.MediaRecorder;
    h.c.MediaRecorder = class extends Base {
      constructor(stream) { super(stream); if (stage === "constructor") throw new Error("constructor failed"); }
      start() { if (stage === "start") throw new Error("start failed"); super.start(); }
      stop() { if (stage === "stop") throw new Error("stop failed"); super.stop(); }
    };
    await h.c.toggleVoiceNote();
    if (stage === "error") h.recorders[0].onerror();
    if (stage === "stop") await h.c.toggleVoiceNote();
    await settle();
    assert.ok(h.streams[0].track.stopped, stage);
    assert.equal(h.timers.size, 0, stage);
    assert.equal(h.lifecycle().active, null, stage);
  }
});

test("failed text saves keep content and idempotency key", async () => {
  const h = harness();
  h.node("capture-text").value = "Keep this";
  h.c.api = async (url, options) => { h.requests.push({ options }); throw new Error("offline"); };
  await h.c.submitCapture(); await h.c.submitCapture();
  assert.equal(h.node("capture-text").value, "Keep this");
  assert.equal(h.requests[0].options.body, h.requests[1].options.body);
});

test("memory reopen restores list, resets provenance and rejects stale detail responses", async () => {
  const h = harness(), pending = deferred();
  h.node("memory-list").hidden = true;
  h.node("memory-versions").textContent = "old private provenance";
  h.c.api = async url => url.endsWith("/old") ? pending.promise : { memories: [{ id: "new", text: "New" }], total: 1 };
  const detail = h.c.openMemoryDetail("old");
  await h.c.refreshMemories();
  pending.resolve({ memory: { text: "Old" } }); await detail;
  assert.equal(h.node("memory-list").hidden, false);
  assert.equal(h.node("memory-detail").hidden, true);
  assert.equal(h.node("memory-versions").textContent, "");
  const row = h.node("memory-list").firstChild;
  assert.equal(row.tabIndex, 0);
  assert.equal(row.getAttribute("role"), "button");
  let opened, prevented = false;
  h.c.openMemoryDetail = id => { opened = id; };
  row.events.keydown({ key: " ", preventDefault: () => { prevented = true; } });
  assert.equal(opened, "new"); assert.ok(prevented);
});

test("failed provenance never displays another memory's version history", async () => {
  const h = harness();
  h.node("memory-versions").textContent = "old versions";
  h.c.api = async url => {
    if (url.endsWith("/provenance")) throw new Error("offline");
    return { memory: { text: "Current" }, sources: [] };
  };
  await h.c.openMemoryDetail("new");
  assert.match(h.node("memory-versions").textContent, /unavailable/);
  assert.doesNotMatch(h.node("memory-versions").textContent, /old versions/);
  h.node("memory-back-btn").events.click();
  assert.equal(h.node("memory-list").hidden, false);
});

test("export recognizes evie and assistant and limits to last 24 local messages", async () => {
  const h = harness();
  h.c.state.history = Array.from({ length: 26 }, (_, i) => ({ role: i % 2 ? "evie" : "assistant", text: String(i) }));
  const text = h.c.conversationExportText();
  assert.equal(text.split("\n").length, 24);
  assert.ok(text.startsWith("Evie: 2\n"));
  await h.c.copyConversation();
  assert.match(h.node("conv-export-meta").textContent, /last 24.*phone/);
});

test("share works without canShare and distinguishes cancellation and failure", async () => {
  const h = harness(); h.c.state.history = [{ role: "evie", text: "Hello" }];
  let shared;
  h.c.navigator.share = async data => { shared = data; };
  await h.c.shareConversation(); assert.equal(shared.text, "Evie: Hello");
  h.c.navigator.share = async () => { throw Object.assign(new Error("cancel"), { name: "AbortError" }); };
  await h.c.shareConversation(); assert.match(h.node("conv-export-meta").textContent, /cancelled/);
  h.c.navigator.share = async () => { throw new Error("denied"); };
  await h.c.shareConversation(); assert.match(h.node("conv-export-meta").textContent, /failed.*Copy/);
  h.c.navigator.canShare = () => { throw new Error("unsupported"); };
  await h.c.shareConversation(); assert.match(h.node("conv-export-meta").textContent, /failed/);
});

test("inbox failed mutations and refresh preserve rows and show feedback", async () => {
  const h = harness(); h.c.state.inbox = [{ id: "notice", unread: true, title: "Keep me" }];
  h.c.fillInbox();
  const row = h.node("inbox-list").firstChild, ack = row.children[1];
  h.c.api = async () => { throw new Error("offline"); };
  await ack.events.click();
  assert.equal(h.node("inbox-list").firstChild, row);
  assert.match(row.textContent, /Could not dismiss/);
  assert.equal(ack.disabled, false);
  await h.c.markAllInboxRead();
  assert.match(h.node("inbox-secondary-status").textContent, /Could not mark all/);
  await h.c.refreshInbox();
  assert.equal(h.node("inbox-list").firstChild, row);
  assert.match(h.node("inbox-secondary-status").textContent, /previous list/);
});

test("queue failed delete and refresh preserve the existing row", async () => {
  const h = harness();
  h.c.api = async () => ({ items: [{ id: "pending", state: "pending", kind: "note" }] });
  await h.c.refreshQueue();
  const row = h.node("queue-list").firstChild;
  h.c.api = async () => ({ ok: false });
  await row.firstChild.events.click();
  assert.equal(h.node("queue-list").firstChild, row);
  assert.match(h.node("queue-meta").textContent, /Could not drop/);
  h.c.api = async () => { throw new Error("offline"); };
  await h.c.refreshQueue();
  assert.equal(h.node("queue-list").firstChild, row);
});

test("Forget clears both credentials, personal state and draft without server revoke", async () => {
  const h = harness();
  for (const key of ["device_token", "access_token", "ev.offlineQueueKeys", "evie_trust_seen"]) h.storage.set(key, "old");
  h.c.state.history = [{ role: "evie", text: "Private" }];
  h.c.state.status = { trust_state: "trusted" };
  h.node("history").textContent = "Private";
  await h.c.toggleVoiceNote();
  await h.c.resetLocal(true); await settle();
  assert.deepEqual(h.removed, ["device_token", "access_token"]);
  assert.equal(h.storage.has("access_token"), false);
  assert.equal(h.c.state.deviceToken, null);
  assert.equal(h.c.state.accessToken, null);
  assert.equal(h.c.state.status, null);
  assert.equal(h.c.state.history.length, 0);
  assert.equal(h.node("history").textContent, "");
  assert.equal(h.lifecycle().draft, null);
  assert.ok(h.streams[0].track.stopped);
  assert.match(h.c.state.caption, /Server trust was not revoked/);
  assert.equal(h.requests.length, 0);
});

test("reset preserves pairing, requests hello again, and reports credential deletion failures honestly", async () => {
  const h = harness();
  await h.c.resetLocal(false);
  assert.equal(h.c.hellos, 1);
  assert.equal(h.c.state.deviceToken, "device-A");
  h.c.idbDel = async key => { h.removed.push(key); throw new Error("storage blocked"); };
  await h.c.resetLocal(true);
  assert.deepEqual(h.removed, ["device_token", "access_token"]);
  assert.match(h.c.state.caption, /Could not remove persisted access_token/);
});

test("late upload completion cannot restore a discarded voice draft or overwrite Forget", async () => {
  const h = harness(), upload = deferred();
  h.c.api = () => upload.promise;
  await h.c.toggleVoiceNote(); await h.c.toggleVoiceNote(); await settle();
  assert.equal(h.lifecycle().saving, true);
  await h.c.resetLocal(true);
  upload.resolve({ ok: true }); await settle();
  assert.equal(h.lifecycle().draft, null);
  assert.match(h.node("voice-note-state").textContent, /cleared/);
  assert.match(h.c.state.caption, /Server trust was not revoked/);
});
