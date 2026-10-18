// WP11 phone panel: approvals inbox, upload staging, stop, status.
// Run: node --test backend/clients/pwa/tests/evie_phone_test.js
//
// Loads the real clients/pwa/evie-phone.js inside a vm sandbox with a tiny DOM
// stub and a fetch stub, so the assertions cover production code (not a copy):
// the panel reads the session from window.EvieSession, never invents a token,
// stages only artifacts the server read back, and renders server strings with
// textContent (no innerHTML of remote data).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const PANEL = path.join(__dirname, "../evie-phone.js");

class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.style = {};
    this.attrs = {};
    this.children = [];
    this.textContent = "";
    this.hidden = false;
    this.listeners = {};
    this._virtual = {};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get id() { return this.attrs.id || ""; }
  set id(v) { this.attrs.id = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this, preventDefault() {} })); }
  get className() { return this.attrs.class || ""; }
  set className(v) { this.attrs.class = String(v); }
  set innerHTML(markup) {
    this._html = String(markup);
    const virtual = {};
    const re = /data-(role|act)="([^"]+)"/g;
    let match;
    while ((match = re.exec(markup)) !== null) {
      const node = new El(match[1] === "act" && match[2] === "file" ? "input" : "div");
      node.setAttribute("data-" + match[1], match[2]);
      virtual['[data-' + match[1] + '="' + match[2] + '"]'] = node;
    }
    this._virtual = virtual;
  }
  get innerHTML() { return this._html || ""; }
  querySelector(selector) {
    if (this._virtual[selector]) return this._virtual[selector];
    for (const child of this.children) {
      if (child.matches && child.matches(selector)) return child;
    }
    return null;
  }
  matches(selector) {
    const m = selector.match(/^\[data-(role|act)="(.+)"\]$/);
    if (!m) return false;
    return this.getAttribute("data-" + m[1]) === m[2];
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
  }
}

function makeDocument() {
  const body = new El("body");
  const byId = {};
  const document = {
    readyState: "complete",
    body,
    createElement: (tag) => {
      const el = new El(tag);
      el.parent = body;
      return el;
    },
    getElementById: (id) => byId[id] || null,
    addEventListener() {},
  };
  const originalAppend = body.appendChild.bind(body);
  body.appendChild = (child) => {
    const el = originalAppend(child);
    if (el.attrs.id) byId[el.attrs.id] = el;
    return el;
  };
  return { document, byId, body };
}

function loadPanel(handler) {
  const { document, byId } = makeDocument();
  const calls = [];
  const fetchStub = async (url, opts) => {
    calls.push({ url, opts: opts || {} });
    const body = handler(url, opts || {});
    return {
      ok: body.__status ? body.__status < 400 : true,
      status: body.__status || 200,
      json: async () => {
        const copy = { ...body };
        delete copy.__status;
        return copy;
      },
    };
  };
  const sessionToken = "device-token-xyz";
  const sandbox = {
    window: {
      EvieSession: { token: () => sessionToken, instanceId: () => "tab-1", notify() {} },
    },
    document,
    fetch: fetchStub,
    console,
    FileReader: class {
      readAsDataURL(file) {
        this.result = "data:image/png;base64," + Buffer.from(String(file.bytes)).toString("base64");
        this.onload();
      }
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(PANEL, "utf8"), sandbox);
  return { sandbox, calls, byId, document, token: sessionToken };
}

test("panel mounts one toggle and keeps itself hidden until opened", () => {
  const { sandbox, byId } = loadPanel(() => ({}));
  assert.ok(sandbox.window.EviePhone, "window.EviePhone is exposed");
  const toggle = byId["evie-phone-toggle"];
  assert.ok(toggle, "a toggle button is mounted");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  sandbox.window.EviePhone.open();
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
});

test("inbox reads the server list and approves by id, refreshing after", async () => {
  let inbox = { count: 1, approvals: [
    { id: "a1", action_type: "files_op", title: "write notes.txt", status: "pending" },
  ] };
  const { sandbox, calls } = loadPanel((url) => {
    if (url === "/v1/fabric/approvals/pending") return inbox;
    if (url === "/v1/fabric/approvals/a1/approve") {
      inbox = { count: 0, approvals: [] };
      return { ok: true, status: "approved" };
    }
    throw new Error("unexpected url " + url);
  });
  sandbox.window.EviePhone.open();
  const pending = await sandbox.window.EviePhone.refreshInbox();
  assert.equal(pending, 1);
  assert.equal(inbox.count, 1);
  const listCall = calls.find((c) => c.url === "/v1/fabric/approvals/pending");
  assert.equal(listCall.opts.headers.Authorization, "Bearer device-token-xyz",
    "the panel uses the session token, never a fabricated one");
  const before = calls.filter((c) => c.url === "/v1/fabric/approvals/pending").length;
  await sandbox.window.EviePhone.decide("a1", true);
  const approve = calls.find((c) => c.url === "/v1/fabric/approvals/a1/approve");
  assert.ok(approve, "approve hit the per-id endpoint");
  assert.equal(approve.opts.method, "POST");
  const after = calls.filter((c) => c.url === "/v1/fabric/approvals/pending").length;
  assert.equal(after, before + 1, "inbox refreshes from the server after a decision");
});

test("upload stages only what the server read back", async () => {
  const { sandbox, calls, document } = loadPanel((url) => {
    if (url === "/v1/fabric/attach") {
      return { ok: true, artifact_id: "art-1", sha256: "abc123", bytes: 3 };
    }
    return {};
  });
  assert.equal(sandbox.window.EviePhone.pendingIds().length, 0);
  const file = { name: "shot.png", bytes: "PNG" };
  const staged = await sandbox.window.EviePhone.upload(file);
  assert.ok(staged && staged.sha256 === "abc123");
  assert.deepEqual([...sandbox.window.EviePhone.pendingIds()], ["art-1"]);
  const attach = calls.find((c) => c.url === "/v1/fabric/attach");
  const payload = JSON.parse(attach.opts.body);
  assert.equal(payload.filename, "shot.png");
  assert.equal(payload.content_base64, Buffer.from("PNG").toString("base64"));
  assert.ok(document, "document stub is stable");
});

test("a rejected upload stages nothing", async () => {
  const { sandbox } = loadPanel((url) => {
    if (url === "/v1/fabric/attach") return { __status: 422, detail: "unsupported image type" };
    return {};
  });
  const staged = await sandbox.window.EviePhone.upload({ name: "x.txt", bytes: "nope" });
  assert.equal(staged, null);
  assert.deepEqual([...sandbox.window.EviePhone.pendingIds()], []);
});

test("staged images clear only after the server answers the turn", () => {
  const { sandbox } = loadPanel(() => ({ ok: true, artifact_id: "art-1", sha256: "s", bytes: 1 }));
  sandbox.window.EviePhone.state.attachments.push({ id: "art-1" });
  sandbox.window.EviePhone.onTurnOutcome({});
  assert.equal(sandbox.window.EviePhone.pendingIds().length, 1, "no reply -> still staged");
  sandbox.window.EviePhone.onTurnOutcome({ ok: true, reply: "done" });
  assert.equal(sandbox.window.EviePhone.pendingIds().length, 0);
});

test("stop reports the cancel endpoint's own count", async () => {
  const { sandbox } = loadPanel((url) => {
    if (url === "/v1/fabric/cancel") {
      return { ok: true, verified: true, evidence: { cancelled: 2 }, error: null, undo: null };
    }
    return {};
  });
  const out = await sandbox.window.EviePhone.stop();
  assert.equal(out.evidence.cancelled, 2);
});

test("status reads fabric/panic/lanes from the server response", async () => {
  const { sandbox } = loadPanel((url) => {
    if (url === "/v1/fabric/status") {
      return { fabric_enabled: true, contract: "v1", lanes: ["files", "shell"],
               panic: { panicked: true } };
    }
    return {};
  });
  const body = await sandbox.window.EviePhone.status();
  assert.equal(body.contract, "v1");
  assert.equal(body.panic.panicked, true);
});

test("panel source never injects server strings with innerHTML", () => {
  const source = fs.readFileSync(PANEL, "utf8");
  const remote = /(item|body|payload|label)\.[a-zA-Z]+/g;
  const innerHtmlWrites = (source.match(/innerHTML\s*=/g) || []).length;
  assert.equal(innerHtmlWrites, 1, "exactly one static innerHTML template");
  const template = source.slice(source.indexOf("panel.innerHTML"));
  const staticMarkup = template.slice(0, template.indexOf(";"));
  assert.ok(!/item\.|body\.|payload\./.test(staticMarkup),
    "the only innerHTML is a literal, never server data");
  assert.ok(remote, "guard against accidental removal of the check");
});
