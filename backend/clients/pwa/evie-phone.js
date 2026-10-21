/* Evie phone panel — approvals inbox, image upload, stop, status.
 *
 * Loaded by /evie/index.html after app.js. It renders only what the server
 * returned: a pending action is "pending" until the server says otherwise, an
 * upload is "ready" only after the attach endpoint read the bytes back, and a
 * stop is reported as the cancel endpoint's own answer.
 *
 * Session access goes through window.EvieSession (exported by app.js) so this
 * file never invents its own token handling.
 */
(function () {
  "use strict";

  const state = { open: false, pending: [], attachments: [], note: "", busy: false };

  function session() {
    return window.EvieSession || null;
  }

  function headers() {
    const s = session();
    const h = { "content-type": "application/json" };
    if (s && s.token()) h.Authorization = "Bearer " + s.token();
    return h;
  }

  async function call(path, opts) {
    const res = await fetch(path, Object.assign({ headers: headers() }, opts || {}));
    let body = {};
    try {
      body = await res.json();
    } catch (_err) {
      body = {};
    }
    if (!res.ok) {
      return { ok: false, error_code: "HTTP_" + res.status, detail: body.detail || "" };
    }
    return body;
  }

  function note(line) {
    state.note = String(line || "");
    paint();
    const s = session();
    if (s && s.notify) s.notify(state.note);
  }

  /* --- approvals inbox ------------------------------------------------- */

  async function refreshInbox() {
    const body = await call("/v1/fabric/approvals/pending");
    state.pending = Array.isArray(body.approvals) ? body.approvals : [];
    state.count = Number(body.count || state.pending.length);
    paint();
    return state.pending.length;
  }

  async function decide(actionId, approve) {
    if (state.busy) return;
    state.busy = true;
    paint();
    try {
      const body = await call(
        "/v1/fabric/approvals/" + encodeURIComponent(actionId) + (approve ? "/approve" : "/deny"),
        { method: "POST", body: "{}" }
      );
      const status = String(body.status || body.error_code || "unknown");
      note((approve ? "Approved · " : "Denied · ") + status);
      await refreshInbox();
    } finally {
      state.busy = false;
      paint();
    }
  }

  /* --- image upload ---------------------------------------------------- */

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || "");
        const comma = text.indexOf(",");
        resolve(comma >= 0 ? text.slice(comma + 1) : "");
      };
      reader.onerror = () => reject(new Error("read failed"));
      reader.readAsDataURL(file);
    });
  }

  async function upload(file) {
    if (!file) return null;
    const contentBase64 = await fileToBase64(file);
    if (!contentBase64) {
      note("Upload · could not read that file");
      return null;
    }
    const body = await call("/v1/fabric/attach", {
      method: "POST",
      body: JSON.stringify({
        filename: file.name || "photo.jpg",
        content_base64: contentBase64,
        turn_id: "phone",
        kind: "screenshot",
      }),
    });
    if (!body.ok || !body.artifact_id) {
      note("Upload · failed · " + (body.error_code || body.detail || "unknown"));
      return null;
    }
    // The receipt carries the sha256 the server read back — a fake success
    // could not produce one.
    state.attachments.push({
      id: body.artifact_id,
      sha256: body.sha256,
      bytes: body.bytes,
    });
    note("Image attached · " + (body.bytes || 0) + " bytes · will ride the next message");
    paint();
    return body;
  }

  function pendingIds() {
    return state.attachments.map((a) => a.id);
  }

  function onTurnOutcome(body) {
    // Clear the queue only when the server answered the turn the images rode.
    const payload = body || {};
    if (!payload.reply && !payload.ok) return;
    if (state.attachments.length) {
      state.attachments = [];
      paint();
    }
  }

  /* --- stop + status --------------------------------------------------- */

  async function stop() {
    const body = await call("/v1/fabric/cancel", {
      method: "POST",
      body: JSON.stringify({ turn_id: null }),
    });
    const cancelled = (body.evidence && body.evidence.cancelled) || 0;
    note(body.ok ? "Stop · cancelled " + cancelled + " running turn(s)" : "Stop · failed");
    return body;
  }

  async function status() {
    const body = await call("/v1/fabric/status");
    if (body.ok === false) {
      note("Status · " + (body.error_code || "unavailable"));
      return body;
    }
    const panic = body.panic || {};
    const text = [
      "fabric " + (body.fabric_enabled ? "on" : "off"),
      "panic " + (panic.panicked ? "ON" : "off"),
      "contract " + String(body.contract || "?"),
      (body.lanes || []).length + " lanes",
    ].join(" · ");
    note("Status · " + text);
    return body;
  }

  /* --- panel ----------------------------------------------------------- */

  let panel = null;

  function build() {
    if (panel) return panel;
    panel = document.createElement("section");
    panel.id = "evie-phone-panel";
    panel.setAttribute("aria-label", "Evie phone panel");
    panel.hidden = true;
    panel.innerHTML =
      '<header style="display:flex;align-items:center;gap:8px;">' +
      '<strong style="flex:1;">Phone</strong>' +
      '<button type="button" data-act="close" aria-label="Close phone panel">×</button>' +
      "</header>" +
      '<div class="evie-phone-row">' +
      '<button type="button" data-act="inbox">Approvals</button>' +
      '<button type="button" data-act="upload">Attach image</button>' +
      '<button type="button" data-act="stop">Stop</button>' +
      '<button type="button" data-act="status">Status</button>' +
      "</div>" +
      '<input type="file" accept="image/*" data-act="file" hidden>' +
      '<div data-role="note" role="status" aria-live="polite"></div>' +
      '<ul data-role="pending"></ul>';
    Object.assign(panel.style, {
      position: "fixed",
      left: "12px",
      bottom: "12px",
      zIndex: "40",
      maxWidth: "min(92vw, 360px)",
      padding: "12px 14px",
      borderRadius: "14px",
      background: "rgba(20,20,24,0.92)",
      color: "#f4f4f6",
      font: "14px/1.35 -apple-system, system-ui, sans-serif",
      boxShadow: "0 10px 30px rgba(0,0,0,0.35)",
    });
    const button = document.createElement("button");
    button.type = "button";
    button.id = "evie-phone-toggle";
    button.textContent = "Phone";
    button.setAttribute("aria-expanded", "false");
    Object.assign(button.style, {
      position: "fixed",
      left: "12px",
      bottom: "12px",
      zIndex: "39",
      padding: "8px 12px",
      borderRadius: "999px",
      border: "none",
      background: "#1d1d21",
      color: "#f4f4f6",
      font: "13px/1 -apple-system, system-ui, sans-serif",
    });
    document.body.appendChild(button);
    document.body.appendChild(panel);
    panel.addEventListener("click", (event) => {
      const target = event.target.closest("[data-act]");
      if (!target) return;
      const act = target.getAttribute("data-act");
      if (act === "close") return toggle(false);
      if (act === "inbox") return void refreshInbox();
      if (act === "upload") return void panel.querySelector('[data-act="file"]').click();
      if (act === "stop") return void stop();
      if (act === "status") return void status();
      if (act === "approve" || act === "deny") {
        return void decide(target.getAttribute("data-id") || "", act === "approve");
      }
      return undefined;
    });
    panel.querySelector('[data-act="file"]').addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      event.target.value = "";
      void upload(file);
    });
    button.addEventListener("click", () => toggle(!state.open));
    return panel;
  }

  function toggle(open) {
    build();
    state.open = !!open;
    panel.hidden = !state.open;
    const button = document.getElementById("evie-phone-toggle");
    if (button) button.setAttribute("aria-expanded", state.open ? "true" : "false");
    if (state.open) void refreshInbox();
  }

  function paint() {
    if (!panel || panel.hidden) return;
    const noteEl = panel.querySelector('[data-role="note"]');
    if (noteEl) noteEl.textContent = state.note;
    const list = panel.querySelector('[data-role="pending"]');
    if (!list) return;
    list.textContent = "";
    Object.assign(list.style, { listStyle: "none", margin: "8px 0 0", padding: "0" });
    if (!state.pending.length) {
      const li = document.createElement("li");
      li.textContent = "Nothing waiting for you.";
      Object.assign(li.style, { opacity: "0.7" });
      list.appendChild(li);
      return;
    }
    state.pending.forEach((item) => {
      const li = document.createElement("li");
      Object.assign(li.style, { margin: "6px 0", display: "flex", gap: "6px", alignItems: "center" });
      const label = document.createElement("span");
      label.textContent =
        String(item.title || item.action_type || "action") +
        " · " + String(item.status || "pending") +
        (item.requires_approval === false ? " · auto" : "");
      Object.assign(label.style, { flex: "1" });
      const approve = document.createElement("button");
      approve.type = "button";
      approve.setAttribute("data-act", "approve");
      approve.setAttribute("data-id", String(item.id || ""));
      approve.textContent = "Approve";
      const deny = document.createElement("button");
      deny.type = "button";
      deny.setAttribute("data-act", "deny");
      deny.setAttribute("data-id", String(item.id || ""));
      deny.textContent = "Deny";
      li.appendChild(label);
      li.appendChild(approve);
      li.appendChild(deny);
      list.appendChild(li);
    });
  }

  window.EviePhone = {
    mount: () => build(),
    open: () => toggle(true),
    close: () => toggle(false),
    refreshInbox,
    decide,
    upload,
    stop,
    status,
    pendingIds,
    onTurnOutcome,
    state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => build());
  } else {
    build();
  }
})();
