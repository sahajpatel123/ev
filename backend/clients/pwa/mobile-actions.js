(function () {
  const SHORTCUTS_SCHEME = "shortcuts://";
  const LEGACY = /legacy_bridge=1/.test(location.search);
  const BRIDGE_KEY = "evie_bridge_installed";
  const BRIDGE_VER = "evie_bridge_version";
  const BRIDGE_CAPS = "evie_bridge_caps";

  function $(id) {
    return document.getElementById(id);
  }

  function textOf(el, value) {
    if (el) el.textContent = value || "";
  }

  function nativeShell() {
    return !!(window.EvieNativeShell && typeof window.EvieNativeShell.post === "function");
  }

  function haptic(event) {
    if (nativeShell()) {
      try {
        Promise.resolve(window.EvieNativeShell.post({ type: "haptic", event: event || "selection" })).catch(function () {});
      } catch (_err) { /* Haptics must not prevent an action or its recovery. */ }
      return;
    }
    if (window.EvieFeedback && window.EvieFeedback.haptic) {
      window.EvieFeedback.haptic(event === "action_failure" ? [40, 40, 40] : 10);
    }
  }

  function launchUrl(url) {
    if (typeof url !== "string" || !url) return false;
    const ok =
      url.indexOf(SHORTCUTS_SCHEME) === 0 ||
      url.indexOf("https://") === 0 ||
      url.indexOf("http://") === 0 ||
      url.indexOf("tel:") === 0 ||
      url.indexOf("sms:") === 0 ||
      url.indexOf("facetime:") === 0 ||
      url.indexOf("maps:") === 0;
    if (!ok) return false;
    if (url.indexOf(SHORTCUTS_SCHEME) === 0 && !LEGACY) return false;
    try {
      window.location.href = url;
      return true;
    } catch (_err) {
      return false;
    }
  }

  function errorText(value) {
    if (!value) return "No result was received.";
    const body = value.body || value;
    const detail = body.detail || {};
    return String(body.failure || body.error_code || body.error || detail.error_code ||
      body.spoken || detail.message || value.message || (typeof body.detail === "string" && body.detail) || body.status || "Action failed.");
  }

  function expired(cur) {
    const raw = cur.expires_at;
    if (!raw) return false;
    const time = typeof raw === "number" ? (raw < 1e12 ? raw * 1000 : raw) : Date.parse(raw);
    return Number.isFinite(time) && time <= Date.now();
  }

  function bounded(promise, milliseconds) {
    let timer;
    return Promise.race([
      promise,
      new Promise((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("Request timed out; its outcome is not confirmed.")), milliseconds);
      }),
    ]).finally(() => clearTimeout(timer));
  }

  const MobileActions = {
    current: null,
    api: null,
    instanceId: "",
    sessionId: "",
    onActivity: function () {},
    onPresent: function () {},
    onStatus: function () {},
    _nativeRuns: new Map(),
    _nativeCompleted: new Map(),
    _autoStarted: new Set(),
    requestTimeoutMs: 15000,
    nativeTimeoutMs: 60000,
    gestureRequired: true,

    configure: function (opts) {
      this.api = opts.api;
      this.instanceId = opts.instanceId;
      this.onActivity = opts.onActivity || function () {};
      if (typeof opts.onPresent === "function") this.onPresent = opts.onPresent;
      if (typeof opts.onStatus === "function") this.onStatus = opts.onStatus;
    },

    setSession: function (sessionId) {
      this.sessionId = sessionId || "";
    },

    _request: function (path, options) {
      return bounded(Promise.resolve().then(() => this.api(path, options)), this.requestTimeoutMs);
    },

    present: function (payload, options) {
      if (!payload) return;
      const card = payload.card || payload;
      const id = payload.action_id || card.action_id;
      const previous = this.current && id && this.current.action_id === id ? this.current : {};
      this.current = Object.assign(previous, {
        action_id: payload.action_id || card.action_id,
        launch_url: payload.launch_url || card.launch_url,
        open_url: payload.open_url || card.open_url,
        confirmation_required: !!(payload.confirmation_required || card.confirmation_required || card.status === "awaiting_confirmation" || card.status === "draft"),
        confirmation_id: payload.confirmation_id || card.confirmation_id,
        method: payload.method || card.method,
        native_execute: !!(payload.native_execute || card.native_execute),
        pwa_kind: payload.pwa_kind || card.pwa_kind,
        share_text: payload.share_text || card.share_text,
        copy_text: payload.copy_text || card.copy_text,
        duration_seconds: payload.duration_seconds || card.duration_seconds,
        when_iso: payload.when_iso || card.when_iso,
        expires_at: payload.expires_at || card.expires_at || (payload.receipt && (payload.receipt.exp || payload.receipt.expires_at)),
        card: card,
        spoken: payload.spoken,
      });
      if (payload.ok === false || card.status === "expired") {
        this.current.blocked = true;
        this._failure(this.current, card.status === "expired" ? { failure: "EXPIRED" } : payload);
      } else if (!previous.busy) {
        this._applyReceipt(this.current, this._nativeCompleted.get(id) || payload);
      }
      this.render();
      try { this.onPresent(this.current); } catch (_err) { /* Observer only. */ }
      if (payload.confirmation_required) haptic("confirmation_requested");
      const line =
        (card.device_label || "This iPhone") +
        " · " +
        (card.title || "Action") +
        (card.target ? " · " + card.target : "");
      this.onActivity(line);
      if (!(options && options.manual) && this.current.native_execute && !this.current.confirmation_required &&
          !this.current.error && !this.current.done && !this._autoStarted.has(id) && !this._availability(this.current)) {
        this._autoStarted.add(id);
        void this.run();
      }
    },

    presentFromHud: function (ev) {
      const card = (ev && ev.hud) || ev || {};
      if (card.kind !== "phone_action" && (ev && ev.kind) !== "phone_action") return false;
      if (card.receipt && this.current && card.action_id === this.current.action_id) {
        this.current.card = Object.assign({}, this.current.card, card);
        if (card.ok === false || ["failed", "expired"].includes(card.status)) this._failure(this.current, card);
        else this._applyReceipt(this.current, card);
        this.render();
        if (card.spoken) this.onActivity(card.spoken);
        return true;
      }
      this.present(Object.assign({}, card, { card: card, action_id: card.action_id }));
      return true;
    },

    _failure: function (cur, err) {
      cur.error = errorText(err);
      if (/expir|invalid_token|already_executed|replay/i.test(cur.error)) {
        cur.blocked = true;
        cur.error += " Ask Evie for a new action or dismiss this card.";
      }
      this.render();
      return { ok: false, error: cur.error };
    },

    _availability: function (cur) {
      if (cur.done || cur.pendingCompletion || cur.handoff) return "";
      if (cur.blocked) return cur.error || "This action is unavailable. Ask Evie for a new action.";
      if (expired(cur)) return "This action expired. Ask Evie for a new action.";
      if (!cur.action_id) return "No executable action was supplied. Ask Evie for a new action.";
      if (!this.api) return "Reconnect to Home Station before running this action.";
      if (cur.confirmation_required) return ""; // Confirmation supplies the authorized payload.
      if (cur.native_execute || cur.method === "native_broker") {
        return nativeShell() && cur.native_execute ? "" : "This action needs the native iPhone app.";
      }
      if (cur.pwa_kind === "share") {
        if (!cur.share_text) return "There is no content to share. Ask Evie to prepare it again.";
        if (typeof navigator.share !== "function") return "Sharing is unavailable in this browser.";
        try {
          if (navigator.canShare && !navigator.canShare({ text: cur.share_text })) return "This content cannot be shared here.";
        } catch (_err) { return "This content cannot be shared here."; }
        return "";
      }
      if (cur.pwa_kind === "clipboard") {
        if (!cur.copy_text) return "There is no text to copy. Ask Evie to prepare it again.";
        return navigator.clipboard && typeof navigator.clipboard.writeText === "function" ? "" : "Clipboard access is unavailable in this browser.";
      }
      if (cur.pwa_kind === "local_timer" || cur.pwa_kind === "local_reminder") {
        return "";
      }
      const url = cur.open_url || (LEGACY ? cur.launch_url : null);
      if (!url) return "This action cannot run here. Ask Evie for a supported action.";
      if (typeof url !== "string" || !/^(https?:|tel:|sms:|facetime:|maps:|shortcuts:)/.test(url) ||
          (url.startsWith(SHORTCUTS_SCHEME) && !LEGACY)) return "This link is unsupported. Ask Evie for another link.";
      return "";
    },

    _applyReceipt: function (cur, result) {
      const receipt = result.receipt || {};
      const details = receipt.receipt || receipt;
      const status = result.status || receipt.state;
      if (status === "cancelled" || details.result === "CANCELLED") {
        cur.done = true;
        cur.outcome = "cancelled";
        cur.statusText = "Cancelled.";
      } else if (status === "failed" || status === "expired") {
        cur.done = false;
        this._failure(cur, { failure: details.failure || status });
        return false;
      } else if (result.system_ui_presented || details.system_ui_presented || details.result === "SYSTEM_UI_OPENED") {
        cur.done = true;
        cur.outcome = "unverified";
        cur.statusText = "System UI opened; completion is not verified.";
      } else if (result.executed === true || status === "executed") {
        cur.done = true;
        cur.outcome = (result.verified === true || details.verified === true) ? "success" : "unverified";
        cur.statusText = cur.outcome === "success" ? "Completed." : "Execution reported; verification unavailable.";
      }
      if (cur.done) { cur.error = ""; cur.blocked = false; }
      return !!cur.done;
    },

    render: function () {
      const root = $("mobile-action-card");
      if (!root) return;
      const cur = this.current;
      if (!cur) {
        root.hidden = true;
        return;
      }
      const card = cur.card || {};
      root.hidden = false;
      root.setAttribute("role", "status");
      root.setAttribute("aria-live", "polite");
      root.setAttribute("aria-atomic", "true");
      textOf($("ma-kicker"), card.device_label || "This iPhone");
      textOf($("ma-op"), card.title || "ACTION");
      textOf($("ma-target"), card.target || "");
      const body = $("ma-body");
      if (card.body) {
        body.hidden = false;
        textOf(body, card.body);
      } else {
        body.hidden = true;
        textOf(body, "");
      }
      const unavailable = this._availability(cur);
      let status = cur.error || unavailable || cur.statusText || (cur.confirmation_required ? "Prepared — confirm to authorize this action." : "Ready to run.");
      if (cur.busy) status = cur.busy;
      textOf($("ma-status"), status);
      root.setAttribute("aria-busy", cur.busy ? "true" : "false");
      const actionState = cur.error || unavailable ? "failure" : cur.outcome || "pending";
      root.dataset.actionState = actionState;
      root.setAttribute("data-action-state", actionState);
      const go = $("ma-go");
      go.textContent = cur.pendingCompletion ? "Retry receipt" : cur.handoff ? "Open again" : cur.confirmation_required ? "Confirm" : cur.error ? "Retry" : card.go_label || "Run";
      go.setAttribute("aria-label", go.textContent);
      go.hidden = !!cur.done;
      go.disabled = !!cur.busy || !!unavailable;
      const cancel = $("ma-cancel");
      cancel.hidden = false;
      cancel.disabled = !!cur.busy;
      cancel.textContent = cur.done || cur.handoff || cur.pendingCompletion || cur.blocked || expired(cur) || !cur.action_id ? "Dismiss" : "Cancel";
      try { this.onStatus(status, cur); } catch (_err) { /* Observer only. */ }
    },

    onTranscript: async function (text) {
      const cur = this.current;
      if (!cur || !this.api || cur.busy || cur.done || cur.blocked) return false;
      if (!cur.confirmation_required) return false;
      if (expired(cur)) { this._failure(cur, { failure: "EXPIRED" }); return true; }
      cur.busy = "Checking confirmation…";
      this.render();
      try {
        const body = await this._request("/v1/device-gateway/mobile-actions/confirm-utterance", {
          method: "POST",
          body: JSON.stringify({
            text: text,
            instance_id: this.instanceId,
            session_id: this.sessionId,
          }),
        });
        if (body && body.unrelated) return false;
        if (!body || body.ok !== true) { this._failure(cur, body); return true; }
        if (this.current !== cur) return true;
        haptic(body.confirmation_required ? "confirmation_requested" : "confirmation_accepted");
        cur.busy = "";
        this.present(body, { manual: true });
        // Web APIs and URL launches still require a fresh user gesture.
        if (this.current.native_execute && !this.current.confirmation_required && !this.current.done) await this.run();
        return true;
      } catch (err) {
        this._failure(cur, err);
        return true;
      } finally {
        cur.busy = "";
        this.render();
      }
    },

    run: async function () {
      const cur = this.current;
      if (!cur || cur.busy || cur.done) return;
      const unavailable = this._availability(cur);
      if (unavailable) return this._failure(cur, { message: unavailable });
      cur.error = "";
      cur.busy = cur.confirmation_required ? "Confirming…" : "Running…";
      this.render();
      try {
        if (cur.pendingCompletion) return await this._completeLocal(cur);
        if (cur.confirmation_required) {
          const confirmed = await this._request("/v1/device-gateway/mobile-actions/" + cur.action_id + "/confirm", {
            method: "POST",
            body: JSON.stringify({ instance_id: this.instanceId }),
          });
          if (!confirmed || confirmed.ok !== true) return this._failure(cur, confirmed);
          if (this.current !== cur) return;
          cur.busy = "";
          this.present(confirmed, { manual: true });
          haptic("confirmation_accepted");
          // Confirmation may have used up Safari's user activation. Require a
          // separate tap for share, clipboard, and URL handoff.
          if (!this.current.native_execute || this.current.confirmation_required || this.current.done) return;
          const unavailable = this._availability(this.current);
          if (unavailable) return this._failure(this.current, { message: unavailable });
          return await this._executeNative(this.current);
        }
        return await this._execute(cur);
      } catch (err) {
        return this._failure(cur, err);
      } finally {
        cur.busy = "";
        this.render();
      }
    },

    _executeNative: async function (cur) {
      if (!cur) return;
      if (!nativeShell() || !cur.action_id) return this._failure(cur, { message: "Native execution is unavailable here." });
      this._autoStarted.add(cur.action_id);
      if (this._nativeCompleted.has(cur.action_id)) {
        this._applyReceipt(cur, this._nativeCompleted.get(cur.action_id));
        this.render();
        return;
      }
      if (this._nativeRuns.has(cur.action_id)) return this._nativeRuns.get(cur.action_id);
      const pending = this._runNative(cur);
      this._nativeRuns.set(cur.action_id, pending);
      try { return await pending; } finally { this._nativeRuns.delete(cur.action_id); }
    },

    _runNative: async function (cur) {
      cur.busy = "Executing on this iPhone…";
      this.render();
      this._checkpoint();
      haptic("action_understood");
      try {
        const receipt = await bounded(window.EvieNativeShell.post({
          type: "execute",
          action_id: cur.action_id,
        }), this.nativeTimeoutMs);
        if (receipt && receipt.ok === true && this._applyReceipt(cur, receipt)) {
          this._nativeCompleted.set(cur.action_id, receipt);
          haptic(cur.outcome === "success" ? "action_success" : "selection");
          if (receipt.spoken) this.onActivity(receipt.spoken);
        } else {
          if (!receipt || receipt.ok !== false) cur.blocked = true;
          this._failure(cur, receipt && receipt.ok === false ? receipt : { message: "Execution is not confirmed. Check the native app; dismiss this card when resolved." });
        }
        this.render();
      } catch (err) {
        // The native app may have executed before the reply was lost. Do not
        // offer another execution for an unknown outcome.
        cur.blocked = true;
        this._failure(cur, { message: "Native execution is not confirmed. Check the native app; dismiss this card when resolved. " + errorText(err) });
      } finally {
        cur.busy = "";
        this.render();
      }
    },

    _execute: async function (cur) {
      if (!cur) return;
      if (cur.native_execute && nativeShell()) {
        await this._executeNative(cur);
        return;
      }
      if (cur.pwa_kind === "share" && navigator.share && cur.share_text) {
        try {
          await navigator.share({ text: cur.share_text });
        } catch (err) {
          if (err && err.name === "AbortError") return await this._completeLocal(cur, "USER_CANCELLED", true);
          throw err;
        }
        return await this._completeLocal(cur, "EXECUTED", true);
      }
      if (cur.pwa_kind === "clipboard" && cur.copy_text && navigator.clipboard) {
        await navigator.clipboard.writeText(cur.copy_text);
        await this._completeLocal(cur, "EXECUTED", true);
        return;
      }
      if (cur.pwa_kind === "local_timer" || cur.pwa_kind === "local_reminder") {
        const seconds = Number(cur.duration_seconds || 0);
        const whenIso = cur.when_iso;
        let fireAt = Date.now() + (seconds > 0 ? seconds * 1000 : 0);
        if (whenIso) {
          const parsed = Date.parse(whenIso);
          if (Number.isFinite(parsed)) fireAt = parsed;
        }
        if (fireAt <= Date.now() + 250) fireAt = Date.now() + 1000;
        if (typeof Notification !== "undefined" && Notification.permission !== "granted") {
          try { await Notification.requestPermission(); } catch (_err) {}
        }
        const title = cur.pwa_kind === "local_reminder" ? "Evie reminder" : "Evie timer";
        const body = (cur.card && (cur.card.target || cur.card.body || cur.card.title)) || "Time's up.";
        if (window.EviePhoneAlerts && typeof window.EviePhoneAlerts.arm === "function") {
          window.EviePhoneAlerts.arm({ id: cur.action_id, fireAt: fireAt, title: title, body: body });
        } else {
          setTimeout(function () {
            try { new Notification(title, { body: body }); } catch (_err) {}
          }, Math.min(Math.max(0, fireAt - Date.now()), 2147483647));
        }
        await this._completeLocal(cur, "CREATED", true);
        return;
      }
      const url = cur.open_url || (LEGACY ? cur.launch_url : null);
      if (url) {
        this._checkpoint();
        if (!launchUrl(url)) throw new Error("The link could not be opened. Try again or ask Evie for another link.");
        this.gestureRequired = true;
        cur.handoff = true;
        cur.outcome = "unverified";
        cur.statusText = "Link handed to iOS; opening and completion cannot be verified here.";
        // Assigning location is not evidence that an external app opened.
        // Do not send a fabricated SYSTEM_UI_OPENED completion receipt.
        this.render();
        return;
      }
      throw new Error("This action cannot run here. Ask Evie for a supported action.");
    },

    _checkpoint: function () {
      try {
        sessionStorage.setItem(
          "evie_action_checkpoint",
          JSON.stringify({
            at: Date.now(),
            action_id: this.current && this.current.action_id,
            session_id: this.sessionId,
          })
        );
      } catch (_err) {}
    },

    _completeLocal: async function (cur, result, verified) {
      if (result) cur.pendingCompletion = { result: result, verified: verified };
      const completion = cur.pendingCompletion;
      if (!completion) return;
      const outcome = completion.result === "USER_CANCELLED" ? "Share cancelled."
        : cur.pwa_kind === "clipboard" ? "Copied to clipboard."
        : cur.pwa_kind === "local_timer" ? "Evie timer armed on this iPhone — not Clock."
        : cur.pwa_kind === "local_reminder" ? "Evie reminder armed on this iPhone — not Reminders.app."
        : "Share sheet completed; delivery is not verified.";
      try {
        const receipt = await this._request("/v1/device-gateway/mobile-actions/" + cur.action_id + "/client-complete", {
          method: "POST",
          body: JSON.stringify({
            status: completion.result === "USER_CANCELLED" ? "cancelled" : "executed",
            result: completion.result,
            verified: completion.verified,
          }),
        });
        if (!receipt || receipt.ok !== true || !this._applyReceipt(cur, receipt)) throw new Error(errorText(receipt));
        cur.pendingCompletion = null;
        cur.error = "";
        cur.statusText = outcome;
      } catch (err) {
        cur.done = false;
        cur.error = outcome + " Home Station has not acknowledged the receipt: " + errorText(err) + " Retry receipt without repeating the action.";
      }
      this.render();
    },

    cancel: async function () {
      const cur = this.current;
      if (!cur || cur.busy) return;
      if (cur.done || cur.handoff || cur.pendingCompletion || cur.blocked || expired(cur) || !cur.action_id) {
        this.current = null;
        this.render();
        return;
      }
      cur.busy = "Cancelling…";
      cur.error = "";
      this.render();
      try {
        if (!this.api) throw new Error("Reconnect to Home Station to cancel this action.");
        const result = await this._request("/v1/device-gateway/mobile-actions/" + cur.action_id + "/cancel", {
          method: "POST",
          body: JSON.stringify({ instance_id: this.instanceId }),
        });
        if (!result || result.ok !== true) return this._failure(cur, result);
        if (!this._applyReceipt(cur, result)) return this._failure(cur, { message: "Cancellation is not confirmed. Retry cancellation." });
      } catch (err) {
        return this._failure(cur, { message: "Cancellation failed. Retry Cancel. " + errorText(err) });
      } finally {
        cur.busy = "";
        this.render();
      }
    },

    handshake: async function () {
      if (!this.api) return null;
      const native = nativeShell() ? window.EvieNativeShell : null;
      let caps = (native && native.capabilities) || [];
      if (!caps.length) {
        try {
          caps = JSON.parse(localStorage.getItem(BRIDGE_CAPS) || "[]");
        } catch (_err) {
          caps = [];
        }
      }
      return this.api("/v1/device-gateway/mobile-actions/handshake", {
        method: "POST",
        body: JSON.stringify({
          instance_id: this.instanceId,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          locale: navigator.language,
          native_shell: !!native,
          broker_version: native && native.version,
          os_version: native && native.osVersion,
          permissions: (native && native.permissions) || {},
          legacy_bridge: LEGACY && localStorage.getItem(BRIDGE_KEY) === "1",
          bridge_installed: LEGACY && localStorage.getItem(BRIDGE_KEY) === "1",
          bridge_version: localStorage.getItem(BRIDGE_VER) || "",
          protocol: 1,
          capabilities: caps,
        }),
      });
    },

    installBridge: async function () {
      const body = await this.api("/v1/device-gateway/mobile-actions/bridge-link", { method: "POST", body: "{}" });
      const url = body.import_url || body.download_url;
      if (url) launchUrl(url);
      return body;
    },

    markInstalled: function (capabilities) {
      localStorage.setItem(BRIDGE_KEY, "1");
      localStorage.setItem(BRIDGE_VER, "1.0.0");
      if (capabilities) localStorage.setItem(BRIDGE_CAPS, JSON.stringify(capabilities));
    },

    status: async function () {
      if (!this.api) return null;
      return this.api("/v1/device-gateway/mobile-actions/status");
    },

    onForeground: function () {
      const raw = sessionStorage.getItem("evie_action_checkpoint");
      if (!raw) return null;
      try {
        return JSON.parse(raw);
      } catch (_err) {
        return null;
      }
    },
  };

  window.EvieMobileActions = MobileActions;
})();
/* Cycle 13 — iPhone-only additive HUD-card render-text helper; backward compat: pure, mirrors ev.hud.card.v1 minimal. */
(function (root) {
  "use strict";
  function EvieHudCardText(card) {
    var c = card || {};
    var lines = [];
    if (c.title != null && String(c.title) !== "") lines.push(String(c.title));
    if (c.body != null && String(c.body) !== "") lines.push(String(c.body));
    var action = c.actionLabel != null ? c.actionLabel : c.action;
    if (action != null && String(action) !== "") lines.push("› " + String(action));
    return lines.join("\n");
  }
  EvieHudCardText.textFor = EvieHudCardText;
  root.EvieHudCardText = EvieHudCardText;
})(typeof window !== "undefined" ? window : globalThis);

/* Cycle 14 — iPhone-only additive share-target intake model; backward compat: pure draft capture. */
(function (root) {
  "use strict";
  var URL_RE = /https?:\/\/[^\s"<>]+/;
  function EvieShareTargetDraft(input) {
    var s = input || {};
    var text = String(s.text == null ? "" : s.text);
    var url = String(s.url == null ? "" : s.url).trim();
    var title = String(s.title == null ? "" : s.title).trim();
    if (!url) {
      var m = URL_RE.exec(text);
      if (m) url = m[0];
    }
    return { text: text, url: url, title: title, hasContent: !!(text.trim() || url) };
  }
  EvieShareTargetDraft.draftFor = EvieShareTargetDraft;
  root.EvieShareTargetDraft = EvieShareTargetDraft;
})(typeof window !== "undefined" ? window : globalThis);

/* Cycle 19 — iPhone-only additive Today-widget view model; backward compat: pure HUD+next action to lines. */
(function (root) {
  "use strict";
  function EvieTodayWidgetLines(input) {
    var s = input || {};
    var lines = [];
    var hud = s.hud == null ? "" : String(s.hud);
    var next = s.nextAction == null ? "" : String(s.nextAction);
    if (hud.trim() !== "") lines.push(hud.trim());
    if (next.trim() !== "") lines.push("Next: " + next.trim());
    return lines.slice(0, 3);
  }
  EvieTodayWidgetLines.linesFor = EvieTodayWidgetLines;
  root.EvieTodayWidgetLines = EvieTodayWidgetLines;
})(typeof window !== "undefined" ? window : globalThis);

/* Cycle 20 — iPhone-only additive camera-sheet upgrade model; backward compat: pure role-aware hints. */
(function (root) {
  "use strict";
  var HINTS = {
    default: ["Center the subject", "Hold steady"],
    receipt: ["Fit the full receipt in frame", "Avoid glare on paper"],
    document: ["Align edges with the guides", "Use good lighting"],
    face: ["Face the light", "Keep your face in the oval"],
  };
  function EvieCameraSheetHints(role) {
    var key = String(role == null ? "default" : role).toLowerCase();
    return (HINTS[key] || HINTS.default).slice();
  }
  EvieCameraSheetHints.hintsFor = EvieCameraSheetHints;
  root.EvieCameraSheetHints = EvieCameraSheetHints;
})(typeof window !== "undefined" ? window : globalThis);

/* Cycle 45 — iPhone-only additive feedback-thumbs model; backward compat: pure turn_id to +1/-1 draft. */
(function (root) {
  "use strict";
  function EvieFeedbackThumbsDraft(input) {
    var s = input || {};
    var turnId = String(s.turn_id == null ? s.turnId : s.turn_id);
    var raw = Number(s.value);
    var value = raw >= 0 ? 1 : -1;
    if (raw !== 1 && raw !== -1) value = raw > 0 ? 1 : -1;
    return { turn_id: turnId, value: value };
  }
  EvieFeedbackThumbsDraft.draftFor = EvieFeedbackThumbsDraft;
  root.EvieFeedbackThumbsDraft = EvieFeedbackThumbsDraft;
})(typeof window !== "undefined" ? window : globalThis);
