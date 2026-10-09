/* Session-categorized phone conversation history (PWA surface).
   Run: node --test backend/clients/pwa/tests/session_history_test.js

   Pins the replacement for the old flat "Recent turns on this phone" list:
   sessions are grouped server-side, each card opens one session's exchange,
   and a turn whose reply was never recorded says so instead of echoing the
   owner's own words back as an answer. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "app.js"), "utf8");
const css = fs.readFileSync(path.join(root, "style.css"), "utf8");

function extract(name) {
  const fn = source.match(new RegExp(`^(?:async )?function ${name}\\([^]*?^}`, "m"));
  assert.ok(fn, `${name} not found in app.js`);
  return fn[0];
}

function makeNode(id) {
  return {
    id,
    textContent: "",
    children: [],
    attributes: {},
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k]; },
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; },
    addEventListener() {},
  };
}

function makeHost() {
  const nodes = new Map();
  const $ = (id) => {
    if (!nodes.has(id)) nodes.set(id, makeNode(id));
    return nodes.get(id);
  };
  return { nodes, $ };
}

const documentStub = { createElement: () => makeNode() };

test("the flat recent-turns list is gone; sessions take its place", () => {
  assert.doesNotMatch(html, /Recent turns on this phone/);
  assert.doesNotMatch(html, /id="turn-history"/);
  assert.doesNotMatch(html, /id="history"/);
  assert.doesNotMatch(html, /conv-copy-btn/);
  assert.doesNotMatch(html, /conv-search-form/);
  assert.match(html, /id="session-list"/);
  assert.match(html, /id="session-sheet"/);
  assert.match(html, /id="session-back-btn"/);
  assert.match(html, /id="session-refresh-btn"/);
  assert.match(html, /id="session-copy-btn"/);
  assert.match(html, /id="session-export-meta"/);
  assert.match(css, /\.session-card \{/);
  assert.match(css, /\.session-reply\.missing \{/);
});

test("the surface reads the sessions endpoints, never the flat history", () => {
  assert.match(source, /\/v1\/device-gateway\/conversations\?limit=30/);
  assert.match(source, /\/v1\/device-gateway\/conversations\/" \+ encodeURIComponent\(sessionId\)/);
  assert.doesNotMatch(source, /loadTurnHistory/);
  assert.doesNotMatch(source, /device-gateway\/history\?limit=20/);
  assert.doesNotMatch(source, /pushHistory/);
  assert.doesNotMatch(source, /paintConversation/);
  assert.doesNotMatch(source, /conv-export-meta/);
});

test("the session sheet is a navigable surface with a way back", () => {
  assert.match(source, /session: "session-sheet"/);
  assert.match(source, /"session-sheet"[\s\S]*?conversation-sheet[\s\S]*?activity-sheet/);
  assert.match(source, /\$\("session-back-btn"\)[\s\S]*?addEventListener\("click", \(\) => openSurface\("conversation"/);
  assert.match(source, /dialog\.id === "session-sheet"[\s\S]*?session-back-btn/);
});

test("an empty phone says so; a session list renders one tappable card per session", async () => {
  const { nodes, $ } = makeHost();
  const api = async (path_) => {
    assert.match(path_, /^\/v1\/device-gateway\/conversations\?limit=30$/);
    return { ok: true, gap_seconds: 2700, sessions: [] };
  };
  const context = { $, api, textOf() {}, document: documentStub };
  vm.createContext(context);
  vm.runInContext(extract("loadSessionList"), context);
  await context.loadSessionList();
  assert.equal($("session-list").children.length, 1);
  assert.equal($("session-list").children[0].textContent, "No conversations recorded yet on this phone.");

  let opened = null;
  const openSurface = () => {};
  const detail = { ok: true, gap_seconds: 2700, sessions: [
    { id: "sess-a", title: "what does Priya prefer for coffee", preview: "what does Priya prefer for coffee", turns: 3, live: false, started_at: "2026-10-08T09:00:00+00:00", last_at: "2026-10-08T09:20:00+00:00" },
    { id: "sess-b", title: "hey Evie", preview: "hey Evie", turns: 2, live: true, started_at: "2026-10-07T22:00:00+00:00", last_at: "2026-10-07T22:30:00+00:00" },
  ] };
  const api2 = async () => detail;
  const context2 = { $, api: api2, textOf(id, text) { if (id) id.textContent = text; }, document: documentStub };
  vm.createContext(context2);
  vm.runInContext(extract("loadSessionList") + extract("openSession"), context2);
  await context2.loadSessionList();
  const cards = $("session-list").children;
  assert.equal(cards.length, 2);
  assert.equal(cards[0].getAttribute("data-session-id"), "sess-a");
  const titles = cards.map(c => c.children[0].textContent);
  assert.deepEqual(titles, ["what does Priya prefer for coffee", "hey Evie"]);
  // Newest session first, with its own time range, turn count, and a voice badge.
  assert.match(cards[0].children[1].textContent, /3 turns/);
  assert.doesNotMatch(cards[0].children[1].textContent, /voice/);
  assert.match(cards[1].children[1].textContent, /2 turns · voice/);
  assert.match($("session-list-meta").textContent, /2 conversations/);
  assert.match($("session-list-meta").textContent, /45 min/);
});

test("opening a session reviews that session's exchange, in order", async () => {
  const { nodes, $ } = makeHost();
  const payload = {
    ok: true,
    session: { id: "sess-a", title: "calendar", turns: 3, started_at: "2026-10-08T09:00:00+00:00", last_at: "2026-10-08T09:10:00+00:00" },
    turns: [
      { at: "2026-10-08T09:00:00+00:00", kind: "typed", origin: "owner", owner_text: "what time is my flight", reply_text: "6:40 AM to Denver.", reply_recorded: true, chips: [] },
      { at: "2026-10-08T09:10:00+00:00", kind: "typed", origin: "owner", owner_text: "move it later", reply_text: null, reply_recorded: false, reply_note: "No reply was recorded for this turn.", chips: [{ tool: "send_message", route: "HOME_STATION", executed: false }] },
      { at: "2026-10-08T09:11:00+00:00", kind: "typed", origin: "evie", owner_text: "", reply_text: "Told Priya.", reply_recorded: true, chips: [] },
    ],
  };
  const api = async (path_) => {
    assert.equal(path_, "/v1/device-gateway/conversations/sess-a");
    return payload;
  };
  const opened = [];
  const context = {
    $, api, document: documentStub,
    state: {},
    textOf(id, text) { if (id) id.textContent = text; },
    openSurface(surface, origin) { opened.push([surface, origin]); },
  };
  vm.createContext(context);
  vm.runInContext(extract("openSession") + extract("loadSessionDetail"), context);
  context.openSession("sess-a", "calendar");

  // The detail sheet replaces the list, and the session's own title rides along.
  assert.deepEqual(opened, [["session", "from-left"]]);
  assert.equal($("session-title").textContent, "calendar");
  await context.loadSessionDetail("sess-a");
  assert.match($("session-meta").textContent, /3 turns/);
  assert.equal(context.state.sessionDetail.title, "calendar");
  assert.equal(context.state.sessionDetail.turns.length, 3);

  const rows = $("session-turns").children;
  assert.equal(rows.length, 3);
  assert.match(rows[0].children[1].textContent, /what time is my flight/);
  // A recorded reply is shown as Evie's answer.
  assert.match(rows[0].children[2].textContent, /6:40 AM to Denver\./);
  assert.equal(rows[0].children[2].className, "session-reply");
  // A turn with no stored reply carries the server's honest note — it never
  // recycles the owner's own words.
  assert.equal(rows[1].children[2].className, "session-reply missing");
  assert.equal(rows[1].children[2].textContent, "No reply was recorded for this turn.");
  // Provenance chips still ride each turn.
  assert.match(rows[1].children[3].children[0].textContent, /send_message · HOME_STATION · not done/);
  // Evie's own line is hers: no "You" label, her words as the row body.
  const evieRow = rows[2];
  assert.doesNotMatch(JSON.stringify(evieRow.children.map((c) => c.className)), /session-who/);
  assert.match(evieRow.children[0].textContent, /Told Priya\./);
});

test("a failed session fetch says so and offers a retry, never an empty state", async () => {
  const { nodes, $ } = makeHost();
  const api = async () => { throw new Error("offline"); };
  const context = { $, api, textOf(id, text) { if (id) id.textContent = text; }, document: documentStub };
  vm.createContext(context);
  vm.runInContext(extract("loadSessionList"), context);
  await context.loadSessionList();
  const kids = $("session-list").children;
  assert.equal(kids.length, 2);
  assert.match(kids[0].textContent, /Couldn't load conversations/);
  assert.equal(kids[1].textContent, "Retry");
  assert.equal($("session-list-meta").textContent, "");
});
