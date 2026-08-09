"use strict";

const $ = (id) => document.getElementById(id);

const store = {
  get url() {
    return localStorage.getItem("ev.apiUrl") || "";
  },
  set url(value) {
    localStorage.setItem("ev.apiUrl", value);
  },
  get key() {
    return localStorage.getItem("ev.apiKey") || "";
  },
  set key(value) {
    localStorage.setItem("ev.apiKey", value);
  },
};

function baseUrl() {
  return store.url.replace(/\/+$/, "");
}

async function api(path, options = {}) {
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    options.headers || {}
  );
  if (store.key) {
    headers["Authorization"] = "Bearer " + store.key;
  }
  const response = await fetch(baseUrl() + path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    throw new Error(`${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setStatus(text, ok) {
  const status = $("ev-status");
  status.textContent = text;
  status.className = "status " + (ok ? "connected" : "disconnected");
}

function showError(target, error) {
  target.textContent = "error: " + error.message;
}

async function connect(event) {
  event.preventDefault();
  store.url = $("api-url").value.trim();
  store.key = $("api-key").value.trim();
  try {
    const health = await api("/v1/health");
    setStatus(health.status + " · " + health.app, true);
  } catch (error) {
    setStatus("unreachable", false);
    showError($("capture-result"), error);
  }
}

async function refreshHud() {
  try {
    const hud = await api("/v1/hud/card");
    $("hud-schema").textContent = hud.schema_version;
    const body = $("hud-body");
    body.innerHTML =
      `<div class="hud-title">${escapeHtml(hud.title)}</div>` +
      `<div class="hud-body-text">${escapeHtml(hud.body)}</div>` +
      `<div class="hud-meta">priority ${escapeHtml(hud.priority)}</div>`;
  } catch (error) {
    showError($("hud-body"), error);
  }
}

async function capture(event) {
  event.preventDefault();
  const text = $("capture-text").value.trim();
  const result = $("capture-result");
  if (!text) {
    result.textContent = "nothing to capture";
    return;
  }
  try {
    const payload = {
      source: "web",
      event_type: "note",
      text,
      privacy_level: "normal",
    };
    const response = await api("/v1/events", {
      method: "POST",
      body: JSON.stringify(payload),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    $("capture-text").value = "";
    const deltas = (response.memory_delta || [])
      .map((d) => `${d.action} ${d.memory_type}`)
      .join(", ");
    result.textContent = `captured ${response.event.id}${deltas ? " · " + deltas : ""}`;
    refreshTimeline();
    refreshHud();
  } catch (error) {
    showError(result, error);
  }
}

async function ask(event) {
  event.preventDefault();
  const question = $("ask-question").value.trim();
  const reply = $("ask-reply");
  const provenance = $("ask-provenance");
  if (!question) {
    reply.textContent = "ask something";
    return;
  }
  try {
    const response = await api("/v1/chat", {
      method: "POST",
      body: JSON.stringify({ message: question, stream: false }),
    });
    reply.textContent = response.reply;
    provenance.innerHTML = (response.provenance || [])
      .map(
        (item) =>
          `<span class="chip" title="score ${item.score}">${escapeHtml(item.memory_type)} · ${escapeHtml(item.text.slice(0, 80))}</span>`
      )
      .join("");
  } catch (error) {
    showError(reply, error);
  }
}

async function browseMemories() {
  const list = $("memory-list");
  const params = new URLSearchParams({ limit: "50" });
  const memoryType = $("memory-type").value;
  const search = $("memory-search").value.trim();
  if (memoryType) {
    params.set("memory_type", memoryType);
  }
  if (search) {
    params.set("q", search);
  }
  try {
    const data = await api("/v1/memories?" + params.toString());
    list.innerHTML = data.memories
      .map(
        (memory) =>
          `<li><button class="audit-btn" data-id="${memory.id}">audit</button> ` +
          `<strong>${escapeHtml(memory.memory_type)} v${memory.version}</strong> ` +
          `${escapeHtml(memory.text.slice(0, 120))} ` +
          `<span class="muted">conf ${memory.confidence}</span></li>`
      )
      .join("");
    list.querySelectorAll(".audit-btn").forEach((button) => {
      button.addEventListener("click", () => showAudit(button.dataset.id));
    });
  } catch (error) {
    showError(list, error);
  }
}

async function showAudit(memoryId) {
  const panel = $("audit-panel");
  try {
    const data = await api("/v1/audit/" + memoryId);
    const memory = data.memory;
    const versions = data.versions.map((v) => "v" + v.version).join(" → ");
    const sources = data.source_events
      .map(
        (event) =>
          `<li>${escapeHtml(event.occurred_at)} [${escapeHtml(event.source)}/${escapeHtml(event.event_type)}] ${escapeHtml((event.content || {}).text || "").slice(0, 100)}</li>`
      )
      .join("");
    panel.innerHTML =
      `<h3>Audit — ${escapeHtml(memory.memory_type)} v${memory.version}</h3>` +
      `<p>${escapeHtml(memory.text)}</p>` +
      `<p class="muted">versions: ${escapeHtml(versions)}</p>` +
      `<ul>${sources}</ul>`;
  } catch (error) {
    showError(panel, error);
  }
}

async function refreshTimeline() {
  const list = $("timeline-list");
  try {
    const data = await api("/v1/timeline?limit=20");
    list.innerHTML = data.events
      .map((event) => {
        const text = (event.content || {}).text || "";
        return (
          `<li><span class="muted">${escapeHtml(event.occurred_at)}</span> ` +
          `[${escapeHtml(event.source)}/${escapeHtml(event.event_type)}] ` +
          `${escapeHtml(text.slice(0, 120))} <span class="muted">${event.id}</span></li>`
        );
      })
      .join("");
  } catch (error) {
    showError(list, error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("api-url").value = store.url;
  $("api-key").value = store.key;
  $("connection-form").addEventListener("submit", connect);
  $("capture-form").addEventListener("submit", capture);
  $("ask-form").addEventListener("submit", ask);
  $("memory-load").addEventListener("click", browseMemories);
  $("timeline-load").addEventListener("click", refreshTimeline);
  refreshHud();
  refreshTimeline();
});
