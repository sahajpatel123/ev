(function () {
  const BRIDGE_KEY = "evie_bridge_installed";
  const BRIDGE_VER = "evie_bridge_version";
  const BRIDGE_CAPS = "evie_bridge_caps";
  const SHORTCUTS_SCHEME = "shortcuts://";

  function $(id) {
    return document.getElementById(id);
  }

  function textOf(el, value) {
    if (el) el.textContent = value || "";
  }

  function launchUrl(url) {
    if (!url) return false;
    if (url.indexOf(SHORTCUTS_SCHEME) !== 0 && url.indexOf("https://") !== 0 && url.indexOf("http://") !== 0 && url.indexOf("tel:") !== 0 && url.indexOf("sms:") !== 0 && url.indexOf("facetime:") !== 0 && url.indexOf("maps:") !== 0) {
      return false;
    }
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
        method: payload.method || card.method,
        pwa_kind: card.pwa_kind,
        share_text: card.share_text,
        copy_text: card.copy_text,
        card: card,
        spoken: payload.spoken,
      };
      this.render();
      const line =
        (card.device_label || "This iPhone") +
        " · " +
        (card.title || "Action") +
        (card.target ? " · " + card.target : "");
      this.onActivity(line);
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
      const status = cur.done
        ? (card.spoken || card.status || "Done")
        : card.status === "awaiting_confirmation"
          ? "Waiting for confirmation"
          : "Tap to run on this iPhone";
      textOf($("ma-status"), status);
      const go = $("ma-go");
      go.textContent = card.go_label || "Run";
      go.hidden = !!cur.done;
      $("ma-cancel").hidden = !!cur.done;
    },

    run: async function () {
      const cur = this.current;
      if (!cur || !this.api) return;
      if (cur.confirmation_required && cur.action_id) {
        const confirmed = await this.api("/v1/device-gateway/mobile-actions/" + cur.action_id + "/confirm", {
          method: "POST",
          body: JSON.stringify({ instance_id: this.instanceId }),
        });
        this.present(confirmed);
        await this._execute(this.current);
        return;
      }
      await this._execute(cur);
    },

    _execute: async function (cur) {
      if (!cur) return;
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
      const url = cur.open_url || cur.launch_url;
      if (url) {
        this._checkpoint();
        launchUrl(url);
        this.gestureRequired = true;
        if (cur.open_url && !cur.launch_url) {
          await this._completeLocal(cur, "SYSTEM_UI_OPENED", false);
        }
        return;
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
      let caps = [];
      try {
        caps = JSON.parse(localStorage.getItem(BRIDGE_CAPS) || "[]");
      } catch (_err) {
        caps = [];
      }
      return this.api("/v1/device-gateway/mobile-actions/handshake", {
        method: "POST",
        body: JSON.stringify({
          instance_id: this.instanceId,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          locale: navigator.language,
          bridge_installed: localStorage.getItem(BRIDGE_KEY) === "1",
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
