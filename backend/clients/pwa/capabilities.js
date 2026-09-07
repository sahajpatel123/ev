/* Cycle 42 — Capability manifest card: an honest "what can this iPhone do"
 * surface rendered from the server-computed /v1/device-gateway/capabilities
 * manifest. Pure display: reads trust state + tool/read/memory flags and the
 * sandbox unlock hint; changes nothing about how turns execute. */
(function () {
  "use strict";

  var LABELS = {
    start_timer: "Timers",
    set_reminder: "Reminders",
    calendar_read: "Calendar",
    get_weather: "Weather",
    list_mail: "Mail",
    list_messages: "Messages",
    open_app: "Open apps",
    open_calculator: "Calculator",
    computer_action: "Mac actions",
    search_web: "Web search",
  };
  var READ_LABELS = {
    clock: "Clock",
    identity: "Identity",
    capabilities: "Devices",
    memory_history: "History recall",
    weather: "Weather",
    calendar: "Calendar",
    contacts: "Contacts",
    inbox: "Inbox",
  };
  var TRUST_LABELS = {
    PAIRED_SANDBOX: "Paired · Sandbox",
    TRUSTED_OWNER_DEVICE: "Trusted owner device",
    REVOKED: "Trust revoked",
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function flagRow(name, enabled, map) {
    var row = el("div", "cap-row");
    var dot = el("span", "cap-dot " + (enabled ? "cap-on" : "cap-off"));
    row.appendChild(dot);
    row.appendChild(el("span", "cap-name", (map && map[name]) || name));
    return row;
  }

  function renderCard(manifest, host) {
    host.textContent = "";
    var card = el("div", "cap-card cap-" + String(manifest.trust_state || "").toLowerCase());

    card.appendChild(el("h3", "cap-title", "What Evie can do here"));
    card.appendChild(
      el("p", "cap-trust", TRUST_LABELS[manifest.trust_state] || manifest.trust_state || "")
    );

    var tools = manifest.tools || {};
    var reads = manifest.reads || {};
    var anyTools = Object.keys(tools).some(function (k) { return tools[k]; });
    var grid = el("div", "cap-grid");
    Object.keys(LABELS).forEach(function (name) {
      grid.appendChild(flagRow(name, !!tools[name], LABELS));
    });
    Object.keys(READ_LABELS).forEach(function (name) {
      if (name in reads) grid.appendChild(flagRow(name, !!reads[name], READ_LABELS));
    });
    card.appendChild(grid);
    if (!anyTools) grid.classList.add("cap-muted");

    var limits = manifest.limits || [];
    if (limits.length) {
      var list = el("ul", "cap-limits");
      limits.forEach(function (line) {
        list.appendChild(el("li", "", line));
      });
      card.appendChild(list);
    }
    if (manifest.upgrade_hint) {
      card.appendChild(el("p", "cap-hint", manifest.upgrade_hint));
    }
    host.appendChild(card);
  }

  function fetchManifest(api) {
    if (api && typeof api === "function") return api("/v1/device-gateway/capabilities");
    return fetch("/v1/device-gateway/capabilities", {
      headers: { Authorization: "Bearer " + (localStorage.getItem("evie_device_token") || "") },
    }).then(function (r) {
      if (!r.ok) throw new Error("capabilities " + r.status);
      return r.json();
    });
  }

  var Capabilities = {
    refresh: function (opts) {
      opts = opts || {};
      var host = document.getElementById("capabilities-card");
      if (!host) return Promise.resolve(null);
      return fetchManifest(opts.api)
        .then(function (body) {
          if (!body || body.ok === false) return null;
          renderCard(body, host);
          window.EvieTrustRendered = window.EvieTrustRendered || {};
          window.EvieTrustRendered.capabilities = body.trust_state;
          return body;
        })
        .catch(function () {
          return null;
        });
    },
  };

  window.EvieCapabilities = Capabilities;
})();
