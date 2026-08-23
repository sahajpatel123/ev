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
    return !!(window.EvieNativeShell && window.EvieNativeShell.post);
  }

  function haptic(event) {
    if (nativeShell()) {
      window.EvieNativeShell.post({ type: "haptic", event: event || "selection" });
      return;
    }
    if (window.EvieFeedback && window.EvieFeedback.haptic) {
      window.EvieFeedback.haptic(event === "action_failure" ? [40, 40, 40] : 10);
    }
  }

  function launchUrl(url) {
    if (!url) return false;
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

  const MobileActions = {
    current: null,
    api: null,
    instanceId: "",
    sessionId: "",
    onActivity: function () {},
    gestureRequired: true,

    configure: function (opts) {
      this.api = opts.api;
      this.instanceId = opts.instanceId;
      this.onActivity = opts.onActivity || function () {};
    },

    setSession: function (sessionId) {
      this.sessionId = sessionId || "";
    },

    present: function (payload) {
      if (!payload) return;
      const card = payload.card || payload;
      this.current = {
        action_id: payload.action_id || card.action_id,
        launch_url: payload.launch_url || card.launch_url,
        open_url: payload.open_url || card.open_url,
        confirmation_required: !!payload.confirmation_required,
        confirmation_id: payload.confirmation_id || card.confirmation_id,
        method: payload.method || card.method,
        native_execute: !!(payload.native_execute || card.native_execute),
        pwa_kind: card.pwa_kind,
        share_text: card.share_text,
        copy_text: card.copy_text,
        card: card,
        spoken: payload.spoken,
      };
      this.render();
      if (payload.confirmation_required) haptic("confirmation_requested");
      const line =
        (card.device_label || "This iPhone") +
        " · " +
        (card.title || "Action") +
        (card.target ? " · " + card.target : "");
      this.onActivity(line);
      if (this.current.native_execute && nativeShell() && !this.current.confirmation_required) {
        this._executeNative(this.current);
      }
    },

    presentFromHud: function (ev) {
      const card = (ev && ev.hud) || ev || {};
      if (card.kind !== "phone_action" && (ev && ev.kind) !== "phone_action") return false;
      if (card.receipt && this.current && card.action_id === this.current.action_id) {
        this.current.card = Object.assign({}, this.current.card, card);
        this.current.done = true;
        this.render();
        if (card.spoken) this.onActivity(card.spoken);
        return true;
      }
      this.present({ card: card, action_id: card.action_id });
      return true;
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
      let status = "Preparing";
      if (cur.done) status = card.spoken || card.status || "Done";
      else if (card.status === "awaiting_confirmation" || cur.confirmation_required) status = "Waiting for confirmation";
      else if (card.status === "draft") status = "Prepared — not sending yet";
      else if (cur.native_execute) status = "Executing on this iPhone";
      else if (card.method === "web_handoff") status = "Waiting for iOS";
      textOf($("ma-status"), status);
      const go = $("ma-go");
      go.textContent = cur.confirmation_required ? "Confirm" : card.go_label || "Run";
      go.hidden = !!cur.done || (!!cur.native_execute && nativeShell() && !cur.confirmation_required);
      $("ma-cancel").hidden = !!cur.done;
    },

    onTranscript: async function (text) {
      if (!this.current || !this.api) return false;
      if (!this.current.confirmation_required && (this.current.card || {}).status !== "draft") return false;
      try {
        const body = await this.api("/v1/device-gateway/mobile-actions/confirm-utterance", {
          method: "POST",
          body: JSON.stringify({
            text: text,
            instance_id: this.instanceId,
            session_id: this.sessionId,
          }),
        });
        if (!body || body.unrelated) return false;
        haptic(body.confirmation_required ? "confirmation_requested" : "confirmation_accepted");
        this.present(body);
        if (body.native_execute && nativeShell()) {
          await this._executeNative(this.current);
        }
        return true;
      } catch (_err) {
        return false;
      }
    },

    run: async function () {
      const cur = this.current;
      if (!cur || !this.api) return;
      if (cur.confirmation_required && cur.action_id) {
        const confirmed = await this.api("/v1/device-gateway/mobile-actions/" + cur.action_id + "/confirm", {
          method: "POST",
          body: JSON.stringify({ instance_id: this.instanceId }),
        });
        haptic("confirmation_accepted");
        this.present(confirmed);
        await this._execute(this.current);
        return;
      }
      await this._execute(cur);
    },

    _executeNative: async function (cur) {
      if (!cur || !nativeShell() || !cur.action_id) return;
      this._checkpoint();
      haptic("action_understood");
      try {
        const receipt = await window.EvieNativeShell.post({
          type: "execute",
          action_id: cur.action_id,
        });
        if (receipt && receipt.ok) {
          haptic(receipt.verified || receipt.executed ? "action_success" : "selection");
          cur.done = receipt.executed || receipt.system_ui_presented;
          if (receipt.spoken) this.onActivity(receipt.spoken);
        } else {
          haptic("action_failure");
        }
        this.render();
      } catch (_err) {
        haptic("action_failure");
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
          await this._completeLocal(cur, "EXECUTED", true);
        } catch (_err) {
          await this._completeLocal(cur, "USER_CANCELLED", true);
        }
        return;
      }
      if (cur.pwa_kind === "clipboard" && cur.copy_text && navigator.clipboard) {
        await navigator.clipboard.writeText(cur.copy_text);
        await this._completeLocal(cur, "EXECUTED", true);
        return;
      }
      const url = cur.open_url || (LEGACY ? cur.launch_url : null);
      if (url) {
        this._checkpoint();
        launchUrl(url);
        this.gestureRequired = true;
        if (cur.open_url && !cur.launch_url) {
          await this._completeLocal(cur, "SYSTEM_UI_OPENED", false);
        }
      }
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
      if (!cur.action_id) return;
      try {
        await this.api("/v1/device-gateway/mobile-actions/" + cur.action_id + "/client-complete", {
          method: "POST",
          body: JSON.stringify({
            status: "executed",
            result: result,
            verified: verified,
          }),
        });
      } catch (_err) {}
      cur.done = true;
      this.render();
    },

    cancel: async function () {
      const cur = this.current;
      if (!cur || !cur.action_id || !this.api) {
        this.current = null;
        this.render();
        return;
      }
      await this.api("/v1/device-gateway/mobile-actions/" + cur.action_id + "/cancel", {
        method: "POST",
        body: JSON.stringify({ instance_id: this.instanceId }),
      }).catch(function () {});
      this.current = null;
      this.render();
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
