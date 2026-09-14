// Source-level behavior tests. All DOM, network, native, and OS operations are
// mocks; this suite does not establish live iPhone or external-service behavior.
// Run: node --test backend/clients/pwa/tests/mobile_actions_test.js
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "mobile-actions.js"), "utf8");
const success = { ok: true, executed: true, verified: true, receipt: { state: "executed" } };
const cancelled = { ok: true, executed: false, receipt: { state: "cancelled", receipt: { result: "CANCELLED" } } };

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture(options = {}) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {
      hidden: false, disabled: false, textContent: "", dataset: {},
      setAttribute(key, value) { this[key] = value; },
    });
    return elements.get(id);
  };
  const requests = [], launches = [], statuses = [], presentations = [];
  const storage = new Map();
  const location = { search: options.legacy ? "?legacy_bridge=1" : "" };
  Object.defineProperty(location, "href", {
    set(url) {
      if (options.launchError) throw new Error("Navigation blocked");
      launches.push(url);
    },
  });
  const context = {
    setTimeout, clearTimeout,
    location, navigator: options.navigator || {},
    document: { getElementById: el },
    window: { location, EvieNativeShell: options.native },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "mobile-actions.js" });
  const actions = context.window.EvieMobileActions;
  actions.configure({
    instanceId: "mock-phone",
    api: async (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      requests.push({ url, body });
      if (options.api) return options.api(url, body);
      return success;
    },
    onStatus: (message, cur) => statuses.push({ message, id: cur.action_id }),
    onPresent: cur => presentations.push(cur.action_id),
  });
  return { actions, el, requests, launches, statuses, presentations, context };
}

function action(card = {}, extra = {}) {
  return {
    ok: true, action_id: "a1", card: { title: "Action", status: "authorized", ...card }, ...extra,
  };
}

function clipboard(f, extra = {}) {
  f.actions.present(action({ pwa_kind: "clipboard", copy_text: "Example", method: "web_handoff" }, extra));
}

test("unsupported share and clipboard show capability errors instead of usable Run", async () => {
  for (const card of [
    { pwa_kind: "share", share_text: "Example" },
    { pwa_kind: "clipboard", copy_text: "Example" },
    { pwa_kind: "clipboard", copy_text: "" },
    { pwa_kind: "share", share_text: "" },
  ]) {
    const f = fixture();
    f.actions.present(action(card));
    assert.equal(f.el("ma-go").disabled, true);
    assert.match(f.el("ma-status").textContent, /unavailable|no text|no content/i);
    await f.actions.run();
    assert.equal(f.requests.length, 0);
    assert.equal(f.el("ma-cancel").disabled, false);
  }
});

test("canShare rejection and missing clipboard method are unavailable", () => {
  const f = fixture({ navigator: { share() {}, canShare: () => false, clipboard: {} } });
  f.actions.present(action({ pwa_kind: "share", share_text: "Example" }));
  assert.match(f.el("ma-status").textContent, /cannot be shared/);
  clipboard(f);
  assert.equal(f.el("ma-go").disabled, true);
});

test("unsupported native action, missing action ID, and missing executor are explained", async () => {
  const f = fixture();
  f.actions.present(action({}, { native_execute: true }));
  assert.match(f.el("ma-status").textContent, /native iPhone app/);
  f.actions.present({ ok: false, failure: "NATIVE_SHELL_REQUIRED", spoken: "Use the native app." });
  assert.equal(f.el("ma-go").disabled, true);
  assert.match(f.el("ma-status").textContent, /NATIVE_SHELL_REQUIRED/);
  await f.actions.cancel();
  assert.equal(f.actions.current, null);
  f.actions.present(action());
  assert.match(f.el("ma-status").textContent, /cannot run here/);
});

test("HTTP success with ok:false cannot launch a supplied URL", async () => {
  const f = fixture();
  f.actions.present(action({}, { ok: false, failure: "ACTION_UNAVAILABLE", open_url: "https://example.test/" }));
  await f.actions.run();
  assert.equal(f.launches.length, 0);
  assert.equal(f.el("ma-go").disabled, true);
});

test("expired action cannot execute or confirm; dismissal makes no cancellation claim", async () => {
  for (const payload of [
    action({}, { expires_at: Date.now() - 1, confirmation_required: true }),
    action({ status: "expired" }),
    action({}, { receipt: { exp: Math.floor(Date.now() / 1000) - 5 } }),
  ]) {
    const f = fixture();
    f.actions.present(payload);
    await f.actions.run();
    assert.match(f.el("ma-status").textContent, /expir/i);
    assert.equal(f.requests.length, 0);
    assert.equal(f.el("ma-cancel").textContent, "Dismiss");
    await f.actions.cancel();
    assert.equal(f.requests.length, 0);
  }
});

test("draft needs server authorization and a second gesture before clipboard execution", async () => {
  let copied = 0;
  const f = fixture({
    navigator: { clipboard: { writeText: async () => { copied++; } } },
    api: async url => url.endsWith("/confirm") ? action({ pwa_kind: "clipboard", copy_text: "Approved text" }) : success,
  });
  f.actions.present(action({ status: "draft", pwa_kind: "clipboard", copy_text: "Draft text" }));
  assert.equal(f.el("ma-go").textContent, "Confirm");
  await f.actions.run();
  assert.equal(copied, 0);
  assert.match(f.requests[0].url, /\/a1\/confirm$/);
  await f.actions.run();
  assert.equal(copied, 1);
  assert.equal(f.actions.current.done, true);
});

test("confirmation network failure remains visible and retryable", async () => {
  const f = fixture({ api: async () => { throw new Error("Home Station offline"); } });
  f.actions.present(action({}, { confirmation_required: true }));
  await assert.doesNotReject(f.actions.run());
  assert.match(f.el("ma-status").textContent, /offline/);
  assert.equal(f.el("ma-go").disabled, false);
  assert.equal(f.el("ma-cancel").disabled, false);
  assert.equal(f.actions.current.action_id, "a1");
  assert.match(f.statuses.at(-1).message, /offline/);
});

test("expired confirmation response keeps original card and offers new-action recovery", async () => {
  const f = fixture({ api: async () => ({ ok: false, failure: "EXPIRED" }) });
  f.actions.present(action({}, { confirmation_required: true }));
  await f.actions.run();
  assert.equal(f.actions.current.action_id, "a1");
  assert.match(f.el("ma-status").textContent, /new action/);
  assert.equal(f.el("ma-go").disabled, true);
  assert.equal(f.el("mobile-action-card").hidden, false);
});

test("repeated taps do not duplicate a pending confirmation", async () => {
  const wait = deferred();
  const f = fixture({ api: () => wait.promise });
  f.actions.present(action({}, { confirmation_required: true }));
  const first = f.actions.run();
  await f.actions.run();
  assert.equal(f.requests.length, 1);
  wait.resolve({ ok: false, failure: "EXPIRED" });
  await first;
});

test("touch and spoken native confirmations each execute exactly once", async () => {
  for (const spoken of [false, true]) {
    let executions = 0;
    const f = fixture({
      native: { post: async body => {
        if (body.type === "execute") executions++;
        return success;
      } },
      api: async () => action({}, { native_execute: true, method: "native_broker" }),
    });
    f.actions.present(action({}, { confirmation_required: true }));
    if (spoken) await f.actions.onTranscript("yes");
    else await f.actions.run();
    assert.equal(executions, 1);
    assert.equal(f.actions.current.done, true);
    assert.equal(f.el("mobile-action-card").dataset.actionState, "success");
    f.actions.present(action({}, { action_id: "other" }));
    f.actions.present(action({}, { native_execute: true, method: "native_broker" }));
    await f.actions.run();
    assert.equal(executions, 1);
    assert.equal(f.actions.current.done, true);
  }
});

test("recovered native card waits for a deliberate tap, including a later duplicate HUD", async () => {
  let executions = 0;
  const f = fixture({ native: { post: async body => {
    if (body.type === "execute") executions++;
    return success;
  } } });
  const payload = action({}, { native_execute: true, recovered: true });
  f.actions.present(payload);
  f.actions.present(action({}, { native_execute: true }));
  await Promise.resolve();
  assert.equal(executions, 0);
  await f.actions.run();
  assert.equal(executions, 1);
});

test("duplicate presentations and taps share one native execution", async () => {
  const wait = deferred();
  let executions = 0;
  const f = fixture({ native: { post: body => {
    if (body.type !== "execute") return Promise.resolve();
    executions++;
    return wait.promise;
  } } });
  const payload = action({}, { native_execute: true });
  f.actions.present(payload);
  f.actions.present(payload);
  await f.actions.run();
  assert.equal(executions, 1);
  wait.resolve(success);
  await f.actions._nativeRuns.get("a1");
  assert.equal(f.actions.current.done, true);
});

test("native failure is visible, does not auto-retry, and permits intentional retry", async () => {
  let executions = 0;
  const f = fixture({ native: { post: async body => {
    if (body.type === "haptic") throw new Error("Haptic unavailable");
    executions++;
    return { ok: false, failure: "PERMISSION_DENIED" };
  } } });
  const payload = action({}, { native_execute: true });
  f.actions.present(payload);
  await f.actions._nativeRuns.get("a1");
  assert.match(f.el("ma-status").textContent, /PERMISSION_DENIED/);
  assert.equal(f.el("ma-go").hidden, false);
  f.actions.present(payload);
  assert.equal(executions, 1);
  await f.actions.run();
  assert.equal(executions, 2);
});

test("spoken confirmation errors remain actionable and unrelated speech does nothing", async () => {
  const f = fixture({ api: async () => { throw new Error("Confirmation unavailable"); } });
  f.actions.present(action({}, { confirmation_required: true }));
  assert.equal(await f.actions.onTranscript("yes"), true);
  assert.match(f.el("ma-status").textContent, /Confirmation unavailable/);
  assert.equal(f.el("ma-go").disabled, false);
  f.actions.api = async () => ({ ok: false, unrelated: true });
  assert.equal(await f.actions.onTranscript("weather"), false);
  assert.equal(f.actions.current.action_id, "a1");
});

test("clipboard rejection is not completion and retry writes once successfully", async () => {
  let writes = 0;
  const f = fixture({ navigator: { clipboard: { writeText: async () => {
    if (++writes === 1) throw new Error("Clipboard permission denied");
  } } } });
  clipboard(f);
  await f.actions.run();
  assert.equal(f.requests.length, 0);
  assert.match(f.el("ma-status").textContent, /permission denied/);
  assert.equal(f.el("ma-go").textContent, "Retry");
  await f.actions.run();
  assert.equal(writes, 2);
  assert.equal(f.el("ma-status").textContent, "Copied to clipboard.");
});

test("failed completion receipt retries persistence without repeating local operation", async () => {
  for (const responseFailure of [false, true]) {
    let writes = 0, calls = 0;
    const f = fixture({
      navigator: { clipboard: { writeText: async () => { writes++; } } },
      api: async () => {
        if (++calls > 1) return success;
        if (responseFailure) return { ok: false, error: "INVALID_TOKEN" };
        throw new Error("Connection lost");
      },
    });
    clipboard(f);
    await f.actions.run();
    assert.equal(writes, 1);
    assert.equal(f.actions.current.done, false);
    assert.equal(f.el("ma-go").textContent, "Retry receipt");
    assert.equal(f.el("ma-go").disabled, false);
    assert.match(f.el("ma-status").textContent, /not acknowledged/);
    await f.actions.run();
    assert.equal(writes, 1);
    assert.equal(calls, 2);
    assert.equal(f.actions.current.done, true);
    assert.equal(f.el("mobile-action-card").dataset.actionState, "success");
  }
});

test("share cancellation is neutral; other share failures remain retryable", async () => {
  for (const name of ["AbortError", "NotAllowedError", "TypeError"]) {
    const f = fixture({
      navigator: { share: async () => { const err = new Error(name); err.name = name; throw err; } },
      api: async () => cancelled,
    });
    f.actions.present(action({ pwa_kind: "share", share_text: "Example" }));
    await f.actions.run();
    if (name === "AbortError") {
      assert.equal(f.requests[0].body.status, "cancelled");
      assert.equal(f.el("mobile-action-card").dataset.actionState, "cancelled");
      assert.match(f.el("ma-status").textContent, /Share cancelled/);
    } else {
      assert.equal(f.requests.length, 0);
      assert.equal(f.el("ma-go").disabled, false);
      assert.match(f.el("ma-status").textContent, new RegExp(name));
    }
  }
});

test("resolved share does not claim verified delivery and receipt retry does not share again", async () => {
  let shares = 0, receipts = 0;
  const f = fixture({
    navigator: { share: async () => { shares++; } },
    api: async () => { if (++receipts === 1) throw new Error("Offline"); return success; },
  });
  f.actions.present(action({ pwa_kind: "share", share_text: "Example" }));
  await f.actions.run();
  await f.actions.run();
  assert.equal(shares, 1);
  assert.match(f.el("ma-status").textContent, /delivery is not verified/);
});

test("unsupported and blocked URLs never report opened or completed", async () => {
  for (const [url, launchError] of [["javascript:alert(1)", false], ["shortcuts://run-shortcut", false], ["https://example.test/", true]]) {
    const f = fixture({ launchError });
    f.actions.present(action({}, { open_url: url }));
    await f.actions.run();
    assert.equal(f.launches.length, 0);
    assert.equal(f.requests.length, 0);
    assert.equal(!!f.actions.current.done, false);
    assert.match(f.el("ma-status").textContent, /unsupported|could not be opened/);
  }
});

test("valid URL and legacy launch are unverified handoffs, with no completion receipt", async () => {
  for (const legacy of [false, true]) {
    const f = fixture({ legacy });
    const url = legacy ? "shortcuts://run-shortcut?name=Example" : "tel:+15555550123";
    f.actions.present(action({}, legacy ? { launch_url: url } : { open_url: url }));
    await f.actions.run();
    assert.deepEqual(f.launches, [url]);
    assert.equal(f.requests.length, 0);
    assert.equal(f.el("mobile-action-card").dataset.actionState, "unverified");
    assert.match(f.el("ma-status").textContent, /cannot be verified/);
    assert.equal(f.el("ma-cancel").textContent, "Dismiss");
  }
});

test("cancellation HTTP and application failures keep the card actionable", async () => {
  for (const failure of ["network", "application"]) {
    let attempts = 0;
    const f = fixture({ api: async () => {
      if (++attempts > 1) return cancelled;
      if (failure === "network") throw new Error("Offline");
      return { ok: false, failure: "TRY_AGAIN" };
    } });
    f.actions.present(action({}, { confirmation_required: true }));
    await f.actions.cancel();
    assert.equal(f.el("mobile-action-card").hidden, false);
    assert.equal(f.actions.current.action_id, "a1");
    assert.equal(f.el("ma-cancel").disabled, false);
    assert.equal(f.el("ma-cancel").textContent, "Cancel");
    await f.actions.cancel();
    assert.equal(f.el("ma-status").textContent, "Cancelled.");
    assert.equal(f.el("mobile-action-card").hidden, false);
    await f.actions.cancel(); // Explicit dismissal after acknowledged cancellation.
    assert.equal(f.actions.current, null);
    assert.equal(attempts, 2);
  }
});

test("already-executed cancellation response is displayed without claiming cancellation", async () => {
  const f = fixture({ api: async () => ({ ok: false, failure: "ALREADY_EXECUTED" }) });
  f.actions.present(action({}, { confirmation_required: true }));
  await f.actions.cancel();
  assert.match(f.el("ma-status").textContent, /ALREADY_EXECUTED/);
  assert.equal(f.el("mobile-action-card").hidden, false);
  assert.equal(f.el("ma-go").disabled, true);
});

test("HUD receipt distinguishes pending, executed-unverified, and failed actions", () => {
  const f = fixture();
  f.actions.present(action({}, { confirmation_required: true }));
  f.actions.presentFromHud({ kind: "phone_action", action_id: "a1", receipt: { state: "authorized" } });
  assert.equal(!!f.actions.current.done, false);
  f.actions.presentFromHud({ kind: "phone_action", action_id: "a1", receipt: { state: "executed", receipt: { verified: false } } });
  assert.equal(f.actions.current.done, true);
  assert.equal(f.el("mobile-action-card").dataset.actionState, "unverified");
  f.actions.present(action({}, { action_id: "a2", confirmation_required: true }));
  f.actions.presentFromHud({ kind: "phone_action", action_id: "a2", status: "failed", receipt: { state: "failed" }, failure: "PERMISSION_DENIED" });
  assert.match(f.el("ma-status").textContent, /PERMISSION_DENIED/);
  assert.equal(f.el("mobile-action-card").hidden, false);
});

test("late confirmation cannot replace a newer action", async () => {
  const wait = deferred();
  const f = fixture({ api: () => wait.promise });
  f.actions.present(action({}, { confirmation_required: true }));
  const pending = f.actions.run();
  f.actions.present(action({}, { action_id: "newer", confirmation_required: true }));
  wait.resolve(action({}, { open_url: "https://example.test/" }));
  await pending;
  assert.equal(f.actions.current.action_id, "newer");
  assert.equal(f.launches.length, 0);
});

test("presentation and status hooks survive configuration refresh", () => {
  const f = fixture();
  f.actions.configure({ api: f.actions.api, instanceId: "refreshed" });
  f.actions.present(action({ pwa_kind: "share", share_text: "Example" }));
  assert.deepEqual(f.presentations, ["a1"]);
  assert.match(f.statuses.at(-1).message, /unavailable/);
  assert.equal(f.statuses.at(-1).id, "a1");
});

test("unanswered confirmation and cancellation release busy state with visible recovery", async () => {
  for (const operation of ["run", "cancel", "onTranscript"]) {
    const f = fixture({ api: () => new Promise(() => {}) });
    f.actions.requestTimeoutMs = 5;
    f.actions.present(action({}, { confirmation_required: true }));
    await f.actions[operation]("yes");
    assert.match(f.el("ma-status").textContent, /timed out/);
    assert.equal(f.el("ma-go").disabled, false);
    assert.equal(f.el("ma-cancel").disabled, false);
    assert.equal(f.el("mobile-action-card").hidden, false);
  }
});

test("timed-out native execution requires checking the app, never repeats automatically", async () => {
  let executions = 0;
  const f = fixture({ native: { post: body => {
    if (body.type === "haptic") return Promise.resolve();
    executions++;
    return new Promise(() => {});
  } } });
  f.actions.nativeTimeoutMs = 5;
  const payload = action({}, { native_execute: true });
  f.actions.present(payload);
  await f.actions._nativeRuns.get("a1");
  assert.match(f.el("ma-status").textContent, /Check the native app/);
  assert.equal(f.el("ma-go").disabled, true);
  assert.equal(f.el("ma-cancel").disabled, false);
  f.actions.present(payload);
  await f.actions.run();
  assert.equal(executions, 1);
});

test("late cancellation does not hide the newer action", async () => {
  const wait = deferred();
  const f = fixture({ api: () => wait.promise });
  f.actions.present(action({}, { confirmation_required: true }));
  const pending = f.actions.cancel();
  f.actions.present(action({}, { action_id: "newer", confirmation_required: true }));
  wait.resolve(cancelled);
  await pending;
  assert.equal(f.actions.current.action_id, "newer");
  assert.equal(f.el("mobile-action-card").hidden, false);
});

test("pwa local timer does not need a url", () => {
  const f = fixture();
  f.actions.present(action({
    pwa_kind: "local_timer",
    duration_seconds: 60,
    title: "TIMER",
    target: "1 minute",
    method: "pwa_local",
  }));
  assert.equal(f.el("ma-go").disabled, false);
  assert.match(f.el("ma-status").textContent, /Ready|Prepared|timer/i);
});

test("native acknowledgment without an execution result cannot be mistaken for success or replayed", async () => {
  let executions = 0;
  const f = fixture({ native: { post: async body => {
    if (body.type === "execute") executions++;
    return { ok: true, executed: false };
  } } });
  f.actions.present(action({}, { native_execute: true }));
  await f.actions._nativeRuns.get("a1");
  assert.equal(!!f.actions.current.done, false);
  assert.equal(f.el("ma-go").disabled, true);
  assert.match(f.el("ma-status").textContent, /not confirmed/);
  await f.actions.run();
  assert.equal(executions, 1);
});
