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

const OFFLINE_KEY = "ev.offlineQueue";
const QUARANTINE_KEY = "ev.quarantine";
const ONBOARDING_KEY = "ev.onboarding";

function readQueue() {
  try {
    const records = JSON.parse(localStorage.getItem(OFFLINE_KEY) || "[]");
    return Array.isArray(records) ? records : [];
  } catch {
    return [];
  }
}

function writeQueue(records) {
  localStorage.setItem(OFFLINE_KEY, JSON.stringify(records));
  updateQueueStatus();
}

function updateQueueStatus() {
  $("queue-status").textContent = readQueue().length
    ? `${readQueue().length} queued offline`
    : "";
}

function queueCapture(payload) {
  const records = readQueue();
  records.push({
    idempotency_key: crypto.randomUUID(),
    queued_at: new Date().toISOString(),
    payload,
  });
  writeQueue(records);
}

function quarantineRecord(record, reason) {
  const entries = [];
  try {
    entries.push(...JSON.parse(localStorage.getItem(QUARANTINE_KEY) || "[]"));
  } catch {
    // start fresh
  }
  entries.push({ ...record, quarantined_at: new Date().toISOString(), reason });
  localStorage.setItem(QUARANTINE_KEY, JSON.stringify(entries));
}

function readOnboardingTexts() {
  try {
    const texts = JSON.parse(localStorage.getItem(ONBOARDING_KEY) || "[]");
    return Array.isArray(texts) ? texts : [];
  } catch {
    return [];
  }
}

function renderOnboardingList() {
  const texts = readOnboardingTexts();
  $("onboarding-list").innerHTML = texts
    .map((text) => `<li>${escapeHtml(text)}</li>`)
    .join("");
}

function addOnboardingText() {
  const input = $("onboarding-text");
  const text = input.value.trim();
  if (!text) {
    return;
  }
  const texts = readOnboardingTexts();
  texts.push(text);
  localStorage.setItem(ONBOARDING_KEY, JSON.stringify(texts));
  input.value = "";
  renderOnboardingList();
}

async function finishOnboarding() {
  const result = $("onboarding-result");
  const texts = readOnboardingTexts();
  if (!texts.length) {
    result.textContent = "add at least one thing first";
    return;
  }
  const events = [];
  for (const text of texts) {
    const response = await api("/v1/events", {
      method: "POST",
      body: JSON.stringify({
        source: "web",
        event_type: "note",
        text,
        privacy_level: "normal",
      }),
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    events.push(response.event);
  }
  localStorage.removeItem(ONBOARDING_KEY);
  renderOnboardingList();

  let auditLine = "";
  try {
    const search = await api(
      "/v1/memories?q=" + encodeURIComponent(texts[0]) + "&limit=1"
    );
    const memory = (search.memories || [])[0];
    if (memory) {
      const audit = await api("/v1/audit/" + memory.id);
      auditLine = ` First audit: ${audit.memory.text} (${audit.source_events.length} source events)`;
    }
  } catch {
    auditLine = "";
  }
  result.textContent = `EV remembers ${events.length} things.` + auditLine;
  refreshTimeline();
  refreshHud();
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
    if (error instanceof TypeError) {
      queueCapture(payload);
      result.textContent = `offline — queued for sync (${readQueue().length} pending)`;
      return;
    }
    showError(result, error);
  }
}

async function syncQueue() {
  const result = $("capture-result");
  const records = readQueue();
  if (!records.length) {
    result.textContent = "queue is empty";
    return;
  }
  const remaining = [];
  let synced = 0;
  let dropped = 0;
  let quarantined = 0;
  for (const record of records) {
    const headers = { "Content-Type": "application/json", "Idempotency-Key": record.idempotency_key };
    if (store.key) {
      headers["Authorization"] = "Bearer " + store.key;
    }
    try {
      const response = await fetch(baseUrl() + "/v1/events", {
        method: "POST",
        headers,
        body: JSON.stringify(record.payload),
      });
      if (response.status === 201) {
        synced += 1;
      } else if (response.status === 409) {
        dropped += 1;
      } else if (response.status === 400 || response.status === 422) {
        quarantined += 1;
        quarantineRecord(record, (await response.text()).slice(0, 500));
      } else {
        remaining.push(record);
      }
    } catch {
      remaining.push(record);
      break;
    }
  }
  writeQueue(remaining);
  result.textContent =
    `synced ${synced}, duplicates ${dropped}, quarantined ${quarantined}, remaining ${remaining.length}`;
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
          `<button class="edit-btn" data-action="correct" data-id="${memory.id}" ` +
          `data-text="${escapeHtml(memory.text)}">correct</button> ` +
          `<button class="edit-btn" data-action="forget" data-id="${memory.id}">forget</button> ` +
          `<button class="edit-btn" data-action="restore" data-id="${memory.id}">restore</button> ` +
          `<strong>${escapeHtml(memory.memory_type)} v${memory.version}</strong> ` +
          `${escapeHtml(memory.text.slice(0, 120))} ` +
          `<span class="muted">conf ${memory.confidence}</span></li>`
      )
      .join("");
    list.querySelectorAll(".audit-btn").forEach((button) => {
      button.addEventListener("click", () => showAudit(button.dataset.id));
    });
    if (!list.dataset.editBound) {
      list.dataset.editBound = "1";
      list.addEventListener("click", async (event) => {
        const button = event.target.closest(".edit-btn");
        if (!button) {
          return;
        }
        const id = button.dataset.id;
        const action = button.dataset.action;
        try {
          if (action === "correct") {
            const corrected = window.prompt("Corrected text", button.dataset.text || "");
            if (!corrected) {
              return;
            }
            await api(`/v1/memories/${id}/correct`, {
              method: "POST",
              body: JSON.stringify({ corrected_text: corrected, reason: "web correction" }),
            });
          } else if (action === "forget") {
            if (!window.confirm("Forget this memory? History stays auditable.")) {
              return;
            }
            await api(`/v1/memories/${id}/forget`, {
              method: "POST",
              body: JSON.stringify({ reason: "web requested" }),
            });
          } else if (action === "restore") {
            await api(`/v1/memories/${id}/restore`, { method: "POST" });
          }
          $("memory-result").textContent = "memory updated";
          browseMemories();
        } catch (error) {
          showError($("memory-result"), error);
        }
      });
    }
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

function transparencyRows(items) {
  return (items || [])
    .map(
      (item) =>
        "<tr><td>" +
        Object.entries(item)
          .map(
            ([key, value]) =>
              `<span class="muted">${escapeHtml(key)}</span> ` +
              escapeHtml(Array.isArray(value) ? value.join(", ") : value ?? "")
          )
          .join("<br>") +
        "</td></tr>"
    )
    .join("");
}

async function loadTransparency() {
  const policy = $("transparency-policy");
  const report = $("transparency-report");
  try {
    const [policyData, transparency] = await Promise.all([
      api("/v1/compliance/policy"),
      api("/v1/compliance/transparency"),
    ]);
    policy.innerHTML =
      `<p><strong>region</strong> ${escapeHtml(policyData.region)} · ` +
      `<strong>residency</strong> ${escapeHtml(policyData.residency_mode)} · ` +
      `<strong>local only</strong> ${policyData.local_residency_required}</p>` +
      `<p class="muted">retention days: ${Object.entries(policyData.retention_days)
        .map(([key, value]) => `${key}=${value}`)
        .join(", ")}</p>` +
      `<ul>${(policyData.disclosures || [])
        .map((item) => `<li>${escapeHtml(item)}</li>`)
        .join("")}</ul>`;
    report.innerHTML =
      `<h3>Stored</h3><table class="transparency-table"><tbody>` +
      transparencyRows(transparency.stored) +
      "</tbody></table>" +
      `<h3>Trained</h3><table class="transparency-table"><tbody>` +
      transparencyRows(transparency.trained) +
      "</tbody></table>" +
      `<h3>Processed</h3><table class="transparency-table"><tbody>` +
      transparencyRows(transparency.processed) +
      "</tbody></table>" +
      `<h3>Transmitted</h3><table class="transparency-table"><tbody>` +
      transparencyRows(transparency.transmitted) +
      "</tbody></table>";
  } catch (error) {
    showError(report, error);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("api-url").value = store.url;
  $("api-key").value = store.key;
  $("connection-form").addEventListener("submit", connect);
  $("onboarding-add").addEventListener("click", addOnboardingText);
  $("onboarding-finish").addEventListener("click", finishOnboarding);
  $("capture-form").addEventListener("submit", capture);
  $("sync-queue").addEventListener("click", syncQueue);
  $("ask-form").addEventListener("submit", ask);
  $("memory-load").addEventListener("click", browseMemories);
  $("timeline-load").addEventListener("click", refreshTimeline);
  $("transparency-load").addEventListener("click", loadTransparency);
  refreshHud();
  refreshTimeline();
  updateQueueStatus();
  renderOnboardingList();
});

window.EV = {
  queueCapture,
  syncQueue,
  readQueue,
  addOnboardingText,
  finishOnboarding,
  readOnboardingTexts,
};
