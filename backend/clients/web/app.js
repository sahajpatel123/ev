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
  const headers = authHeaders(options.headers || {});
  const response = await fetch(baseUrl() + path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    throw new Error(`${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  return response.json();
}

function authHeaders(extra = {}) {
  const headers = Object.assign(
    { "Content-Type": "application/json" },
    extra
  );
  if (store.key) {
    headers["Authorization"] = "Bearer " + store.key;
  }
  return headers;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function postSse(path, body, handlers) {
  const controller = new AbortController();
  (async () => {
    try {
      const response = await fetch(baseUrl() + path, {
        method: "POST",
        headers: authHeaders({ Accept: "text/event-stream" }),
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const text = await response.text();
        throw new Error(`${response.status}: ${text.slice(0, 300)}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventName = null;
      let dataLines = [];
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) {
              eventName = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trim());
            }
          }
          if (dataLines.length) {
            let data = {};
            try {
              data = JSON.parse(dataLines.join("\n"));
            } catch {
              data = {};
            }
            const name = eventName || "message";
            eventName = null;
            dataLines = [];
            if (name === "done") {
              if (handlers.onDone) handlers.onDone(data);
              return;
            }
            if (name === "error") {
              if (handlers.onError) {
                handlers.onError(new Error(data.message || "stream error"), data);
              }
              return;
            }
            if (handlers.onEvent) handlers.onEvent(name, data);
          }
        }
      }
      if (handlers.onError) {
        handlers.onError(new Error("stream ended before done"));
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        if (handlers.onCancel) handlers.onCancel();
        return;
      }
      if (handlers.onError) handlers.onError(error);
    }
  })();
  return controller;
}

function renderStreamingText(element, text) {
  element.textContent = text;
}

function provenanceChips(items, container) {
  container.innerHTML = (items || [])
    .map(
      (item) =>
        `<button type="button" class="chip${item.memory_id ? " audit-chip" : ""}"` +
        `${item.memory_id ? ` data-memory-id="${item.memory_id}"` : ""}` +
        ` title="${escapeHtml(String(item.text || "").slice(0, 120))}">` +
        `${escapeHtml(item.memory_type || "source")} · ` +
        `${escapeHtml(String(item.text || "").slice(0, 80))}</button>`
    )
    .join("");
  container.querySelectorAll(".audit-chip").forEach((button) => {
    button.addEventListener("click", () => showAudit(button.dataset.memoryId));
  });
}

const HUD_SCHEMAS = {
  "ev.hud.card.v1": {
    required: ["schema_version", "generated_at", "title", "body", "priority"],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      title: { type: "string" },
      body: { type: "string" },
      priority: { type: "number" },
      meta: { type: "object" },
    },
  },
  "ev.hud.briefing.v1": {
    required: ["schema_version", "generated_at", "objective", "context"],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      objective: { type: "string" },
      context: { type: "string" },
      people: { type: "array" },
      risks: { type: "array" },
      options: { type: "array" },
      recommendation: { type: ["string", "null"] },
      decision_history: { type: "array" },
      talking_points: { type: "array" },
      provenance: { type: "array" },
    },
  },
  "ev.hud.focus.v1": {
    required: ["schema_version", "generated_at", "locked", "context"],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      focus: { type: ["object", "null"] },
      locked: { type: "boolean" },
      context: { type: "string" },
      next_action: { type: ["string", "null"] },
      meta: { type: "object" },
    },
  },
  "ev.hud.route.v1": {
    required: ["schema_version", "generated_at"],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      destination: { type: ["string", "null"] },
      leave_by: { type: ["string", "null"] },
      travel_time_minutes: { type: ["integer", "null"] },
      prep_checklist: { type: "array" },
      notes: { type: "array" },
    },
  },
  "ev.hud.quickcard.v1": {
    required: ["schema_version", "generated_at", "objective", "summary"],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      objective: { type: "string" },
      summary: { type: "string" },
      next_action: { type: ["string", "null"] },
      top_risk: { type: ["string", "null"] },
      people_count: { type: "integer" },
      options_count: { type: "integer" },
      decision_history_count: { type: "integer" },
      meta: { type: "object" },
    },
  },
  "ev.hud.ops.v1": {
    required: [
      "schema_version",
      "generated_at",
      "title",
      "summary",
      "focus_locked",
      "online_devices",
      "pending_alerts",
      "open_decisions",
    ],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      title: { type: "string" },
      summary: { type: "string" },
      focus_locked: { type: "boolean" },
      online_devices: { type: "integer" },
      pending_alerts: { type: "integer" },
      open_decisions: { type: "integer" },
      command_cards: { type: "array" },
      meta: { type: "object" },
    },
  },
  "ev.hud.alert.v1": {
    required: [
      "schema_version",
      "generated_at",
      "alert_id",
      "title",
      "body",
      "priority",
      "tier",
    ],
    properties: {
      schema_version: { type: "string" },
      generated_at: { type: "string" },
      alert_id: { type: "string" },
      title: { type: "string" },
      body: { type: "string" },
      priority: { type: "number" },
      tier: { type: "string" },
      kind: { type: ["string", "null"] },
      rationale: { type: ["string", "null"] },
      meta: { type: "object" },
    },
  },
};

function schemaLabel(key) {
  return String(key).replaceAll("_", " ");
}

function renderSchemaCard(data) {
  const schema = HUD_SCHEMAS[data.schema_version] || null;
  const props = schema ? schema.properties : {};
  const lines = [];
  if (!schema) {
    return Object.entries(data)
      .map(
        ([key, value]) =>
          `<div><span class="muted">${escapeHtml(schemaLabel(key))}:</span> ` +
          `${escapeHtml(Array.isArray(value) ? value.join(", ") : typeof value === "object" ? JSON.stringify(value) : String(value))}</div>`
      )
      .join("");
  }
  for (const key of Object.keys(props)) {
    const value = data[key];
    if (value === undefined || value === null) {
      continue;
    }
    if (key === "schema_version") {
      lines.push(`<div class="schema-badge">${escapeHtml(value)}</div>`);
      continue;
    }
    if (key === "generated_at") {
      lines.push(`<div class="muted">${escapeHtml(value)}</div>`);
      continue;
    }
    if (Array.isArray(value)) {
      const items = value
        .map((item) => {
          if (typeof item === "string") {
            return escapeHtml(item);
          }
          if (typeof item === "object" && item !== null) {
            const entries = Object.entries(item)
              .map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v))}`)
              .join(" · ");
            return escapeHtml(entries);
          }
          return escapeHtml(String(item));
        })
        .join("</li><li>");
      lines.push(`<div class="schema-field"><span class="muted">${escapeHtml(schemaLabel(key))}:</span></div><ul class="schema-list"><li>${items}</li></ul>`);
      continue;
    }
    if (typeof value === "object") {
      lines.push(
        `<div class="schema-field"><span class="muted">${escapeHtml(schemaLabel(key))}:</span> ` +
          `<code>${escapeHtml(JSON.stringify(value))}</code></div>`
      );
      continue;
    }
    lines.push(
      `<div class="schema-field"><span class="muted">${escapeHtml(schemaLabel(key))}:</span> ` +
        `${escapeHtml(String(value))}</div>`
    );
  }
  return lines.join("");
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

async function onboardingReadiness() {
  const body = $("onboarding-readiness");
  const steps = [];
  try {
    const health = await api("/v1/health");
    steps.push(`master key: ${health.status === "ok" ? "ok" : health.status}`);
  } catch {
    steps.push("master key: unreachable — set API key in Connection");
  }
  try {
    const consents = await api("/v1/training/consent");
    const active = consents
      .filter((row) => !row.revoked_at)
      .map((row) => row.track)
      .join(", ");
    steps.push(`consent: ${active || "none granted"}`);
  } catch {
    steps.push("consent: unavailable");
  }
  try {
    const identity = await api("/v1/identity/status");
    steps.push(
      `owner: ${identity.owner_established ? identity.display_name || "established" : "not established"}`
    );
  } catch {
    steps.push("owner: unavailable");
  }
  try {
    const enrollments = await api("/v1/voice/enrollments");
    const current = enrollments.find((row) => row.is_current);
    steps.push(
      current
        ? `voice: ${current.status} (v${current.version}, ${current.sample_count} samples)`
        : "voice: not enrolled"
    );
  } catch {
    steps.push("voice: unavailable");
  }
  body.innerHTML = steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("");
}

let wizardStep = 0;

const WIZARD_STEPS = [
  {
    title: "Master key check",
    run: async () => {
      const health = await api("/v1/health");
      const identity = await api("/v1/identity/status");
      return (
        `Master key ok (${health.app} ${health.version}). Owner: ` +
        (identity.owner_established ? identity.display_name || "established" : "not established")
      );
    },
  },
  {
    title: "Training consent",
    run: async () => {
      for (const track of ["voice_enrollment", "training_corpus", "life_data_personalization"]) {
        await api("/v1/training/consent", {
          method: "POST",
          body: JSON.stringify({ track }),
        });
      }
      return "Consent granted for voice_enrollment, training_corpus, life_data_personalization";
    },
  },
  {
    title: "Recovery codes",
    run: async () => {
      const data = await api("/v1/identity/recovery/codes", { method: "POST" });
      return (
        "One-time recovery codes (store offline, never in EV): " +
        (data.recovery_codes || []).map((code) => code.code).join(", ")
      );
    },
  },
  {
    title: "First memories",
    run: async () => {
      const texts = readOnboardingTexts();
      return texts.length
        ? `${texts.length} memories ready — use Finish & show audit above.`
        : "No memories yet — add them in the Getting Started panel.";
    },
  },
];

function renderWizard() {
  document.querySelectorAll("#wizard-steps li").forEach((item, index) => {
    item.className = index === wizardStep ? "active" : index < wizardStep ? "done" : "";
  });
  $("wizard-body").innerHTML =
    `<li><strong>${escapeHtml(WIZARD_STEPS[wizardStep].title)}</strong></li>`;
}

async function wizardNext() {
  const result = $("wizard-result");
  try {
    const output = await WIZARD_STEPS[wizardStep].run();
    $("wizard-body").innerHTML =
      `<li><strong>${escapeHtml(WIZARD_STEPS[wizardStep].title)}</strong></li>` +
      `<li>${escapeHtml(output)}</li>`;
    result.textContent = `step ${wizardStep + 1} done`;
    if (wizardStep < WIZARD_STEPS.length - 1) {
      wizardStep += 1;
      renderWizard();
    } else {
      result.textContent = "setup wizard complete";
    }
  } catch (error) {
    showError(result, error);
  }
}

function wizardBack() {
  if (wizardStep > 0) {
    wizardStep -= 1;
    renderWizard();
  }
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
    body.innerHTML = renderSchemaCard(hud);
    const cardBody = $("hud-card-body");
    if (cardBody) {
      cardBody.innerHTML = renderSchemaCard(hud);
    }
  } catch (error) {
    showError($("hud-body"), error);
  }
}

function renderHud(data) {
  return (
    `<div class="hud-header">` +
    `${escapeHtml(data.schema_version || "")}` +
    `</div>` +
    renderSchemaCard(data)
  );
}

async function loadHud(kind) {
  const topic = $("hud-topic").value.trim() || "EV status";
  const body = $("hud-more-body");
  try {
    let data;
    if (kind === "quick") {
      data = await api(
        "/v1/tactical/quick?topic=" + encodeURIComponent(topic) + "&ttl_seconds=3600"
      );
    } else if (kind === "briefing") {
      data = await api("/v1/tactical/brief", {
        method: "POST",
        body: JSON.stringify({ topic }),
      });
    } else if (kind === "focus") {
      data = await api("/v1/hud/focus");
    } else {
      data = await api("/v1/hud/route");
    }
    body.innerHTML = renderHud(data);
  } catch (error) {
    showError(body, error);
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

let askController = null;
let lastAskQuestion = null;

async function ask(event) {
  event.preventDefault();
  const question = $("ask-question").value.trim();
  if (!question) {
    $("ask-reply").textContent = "ask something";
    return;
  }
  runAsk(question);
}

function runAsk(question) {
  lastAskQuestion = question;
  const reply = $("ask-reply");
  const provenance = $("ask-provenance");
  const errorTarget = $("ask-error");
  reply.textContent = "";
  provenance.innerHTML = "";
  errorTarget.textContent = "";
  $("ask-cancel").hidden = false;
  $("ask-retry").hidden = true;
  const tokens = [];
  let answer = "";
  let provenanceItems = [];
  askController = postSse(
    "/v1/chat",
    { message: question, stream: true },
    {
      onEvent(name, data) {
        if (name === "delta") {
          tokens.push(data.text || "");
          renderStreamingText(reply, tokens.join(""));
        } else if (name === "refined") {
          answer = data.text || "";
          renderStreamingText(reply, answer);
        } else if (name === "provenance") {
          provenanceItems.push(data);
          provenanceChips(provenanceItems, provenance);
        }
      },
      onDone() {
        $("ask-cancel").hidden = true;
        if (!answer) {
          answer = tokens.join("");
          renderStreamingText(reply, answer);
        }
        lastAskQuestion = null;
      },
      onCancel() {
        $("ask-cancel").hidden = true;
        reply.textContent += "\n[stopped]";
      },
      onError(error) {
        $("ask-cancel").hidden = true;
        $("ask-retry").hidden = false;
        showError(errorTarget, error);
        if (!answer && tokens.length) {
          renderStreamingText(reply, tokens.join("") + "\n[incomplete]");
        }
      },
    }
  );
}

function askRetry() {
  if (lastAskQuestion) {
    runAsk(lastAskQuestion);
  }
}

let conversationId = null;

async function loadConversation() {
  const messages = $("conversation-messages");
  try {
    const data = await api("/v1/conversation?limit=50");
    conversationId = data.conversation ? data.conversation.id : null;
    $("conversation-id").textContent = conversationId ? `#${conversationId}` : "";
    messages.innerHTML = (data.messages || [])
      .map(
        (message) =>
          `<div class="message ${message.role === "assistant" ? "assistant" : "user"}">` +
          `<span class="muted">${escapeHtml(message.role)}</span> ` +
          `${escapeHtml(message.text)}</div>`
      )
      .join("");
  } catch (error) {
    showError(messages, error);
  }
}

let conversationController = null;
let lastConversationText = null;

async function sendConversation(event) {
  event.preventDefault();
  const input = $("conversation-text");
  const text = input.value.trim();
  if (!text) {
    return;
  }
  runSendConversation(text, true);
  input.value = "";
}

function runSendConversation(text, appendUserBubble) {
  lastConversationText = text;
  const result = $("conversation-result");
  const messages = $("conversation-messages");
  if (appendUserBubble) {
    messages.insertAdjacentHTML(
      "beforeend",
      `<div class="message user"><span class="muted">user</span> ${escapeHtml(text)}</div>`
    );
  }
  const assistant = document.createElement("div");
  assistant.className = "message assistant";
  assistant.innerHTML =
    `<span class="muted">assistant</span> <span class="stream-body"></span>` +
    `<div class="chips"></div>`;
  messages.appendChild(assistant);
  messages.scrollTop = messages.scrollHeight;
  const bodyEl = assistant.querySelector(".stream-body");
  const chipsEl = assistant.querySelector(".chips");
  $("conversation-cancel").hidden = false;
  $("conversation-retry").hidden = true;
  result.textContent = "";
  const tokens = [];
  let answer = "";
  let provenanceItems = [];
  const body = { message: text, stream: true };
  if (conversationId) {
    body.conversation_id = conversationId;
  }
  conversationController = postSse("/v1/chat", body, {
    onEvent(name, data) {
      if (name === "delta") {
        tokens.push(data.text || "");
        renderStreamingText(bodyEl, tokens.join(""));
      } else if (name === "refined") {
        answer = data.text || "";
        renderStreamingText(bodyEl, answer);
      } else if (name === "provenance") {
        provenanceItems.push(data);
        provenanceChips(provenanceItems, chipsEl);
      }
    },
    onDone(data) {
      conversationId = data.conversation_id || conversationId;
      $("conversation-cancel").hidden = true;
      if (!answer) {
        answer = tokens.join("");
        renderStreamingText(bodyEl, answer);
      }
      if (conversationId) {
        $("conversation-id").textContent = `#${conversationId}`;
      }
      messages.scrollTop = messages.scrollHeight;
      lastConversationText = null;
    },
    onCancel() {
      $("conversation-cancel").hidden = true;
      if (bodyEl.textContent) {
        bodyEl.textContent += " [stopped]";
      }
    },
    onError(error) {
      $("conversation-cancel").hidden = true;
      $("conversation-retry").hidden = false;
      showError(result, error);
      if (tokens.length && !answer) {
        bodyEl.textContent = tokens.join("") + " [incomplete]";
      }
    },
  });
}

function conversationRetry() {
  if (lastConversationText) {
    runSendConversation(lastConversationText, false);
  }
}

async function loadSettings() {
  try {
    const personality = await api("/v1/personality");
    $("p-directness").value = personality.directness ?? 3;
    $("p-humor").value = personality.humor ?? 2;
    $("p-formality").value = personality.formality ?? 2;
    $("p-technicality").value = personality.technicality ?? 4;
    $("p-assertiveness").value = personality.assertiveness ?? 3;
    $("p-verbosity").value = personality.verbosity ?? 3;
    $("p-proactivity").value = personality.proactivity ?? 3;
    $("p-challenge").value = personality.challenge_level ?? 3;
    $("p-emotional").value = personality.emotional_style || "calm";
  } catch {
    // personality may be uninitialized; leave defaults
  }
  try {
    const runtime = await api("/v1/runtime/status");
    const attention = runtime.attention || {};
    $("quiet-hours-status").textContent =
      `active now: ${runtime.quiet_hours_active} · attention: ` +
      `${attention.intrusiveness || "balanced"} · notifications/day: ` +
      `${attention.max_notifications ?? "default"}`;
  } catch (error) {
    showError($("quiet-hours-status"), error);
  }
  renderConsents();
}

async function renderConsents() {
  const list = $("consent-list");
  try {
    const rows = await api("/v1/training/consent");
    list.innerHTML = rows
      .map(
        (row) =>
          `<li>${escapeHtml(row.track)} — ` +
          `${row.revoked_at ? "revoked" : "active"} ` +
          (row.revoked_at
            ? ""
            : `<button class="consent-revoke" data-track="${escapeHtml(row.track)}">revoke</button>`) +
          `</li>`
      )
      .join("");
    list.querySelectorAll(".consent-revoke").forEach((button) => {
      button.addEventListener("click", () => revokeConsent(button.dataset.track));
    });
  } catch (error) {
    showError(list, error);
  }
}

async function revokeConsent(track) {
  try {
    await api(`/v1/training/consent/${track}/revoke`, {
      method: "POST",
      body: JSON.stringify({ reason: "web settings revoke" }),
    });
    renderConsents();
  } catch (error) {
    showError($("consent-list"), error);
  }
}

async function savePersonality() {
  const result = $("personality-result");
  const number = (id) => Number($(id).value);
  try {
    const profile = await api("/v1/personality", {
      method: "POST",
      body: JSON.stringify({
        directness: number("p-directness"),
        humor: number("p-humor"),
        formality: number("p-formality"),
        technicality: number("p-technicality"),
        assertiveness: number("p-assertiveness"),
        verbosity: number("p-verbosity"),
        proactivity: number("p-proactivity"),
        challenge_level: number("p-challenge"),
        emotional_style: $("p-emotional").value,
        reason_for_change: "web settings",
      }),
    });
    result.textContent = `personality saved (v${profile.version})`;
  } catch (error) {
    showError(result, error);
  }
}

async function showRecoveryCodes() {
  const body = $("recovery-codes-body");
  try {
    const data = await api("/v1/identity/recovery/codes", { method: "POST" });
    body.innerHTML = (data.recovery_codes || [])
      .map(
        (code) =>
          `<li><strong>${escapeHtml(code.label)}:</strong> ${escapeHtml(code.code)} ` +
          `<span class="muted">expires ${escapeHtml(code.expires_at)}</span></li>`
      )
      .join("");
  } catch (error) {
    showError(body, error);
  }
}

async function rotateVault() {
  const status = $("vault-status");
  try {
    const data = await api("/v1/integrations/vault/rotate", { method: "POST" });
    status.textContent = `vault rotated at ${data.rotated_at || "?"}`;
  } catch (error) {
    showError(status, error);
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

let voiceSamples = [];
let voiceRecorder = null;
let voiceChunks = [];

function updateVoiceSampleUi() {
  const count = voiceSamples.length;
  $("voice-samples").textContent = `${count}/5 samples`;
  $("voice-enroll").disabled = count < 5;
}

function b64FromBuffer(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

async function refreshVoiceStatus() {
  const body = $("voice-status-body");
  const list = $("voice-enrollments");
  try {
    const [consents, enrollments] = await Promise.all([
      api("/v1/training/consent"),
      api("/v1/voice/enrollments"),
    ]);
    const active = (consents || []).find(
      (c) => c.track === "voice_enrollment" && !c.revoked_at
    );
    body.innerHTML =
      `<p><strong>consent</strong> ${active ? "active" : "not granted"}` +
      (active
        ? ` · v${active.consent_version} · ${escapeHtml(active.granted_at)}`
        : "") +
      `</p>`;
    list.innerHTML = (enrollments || [])
      .map(
        (enrollment) =>
          `<li><strong>v${enrollment.version}</strong> ${escapeHtml(enrollment.status)} · ` +
          `${enrollment.sample_count} samples · ${escapeHtml(enrollment.encoder)}` +
          ` <button class="voice-revoke" data-id="${enrollment.id}">revoke</button>` +
          ` <button class="voice-delete" data-id="${enrollment.id}">delete</button></li>`
      )
      .join("");
    list.querySelectorAll(".voice-revoke").forEach((button) => {
      button.addEventListener("click", () => revokeEnrollment(button.dataset.id));
    });
    list.querySelectorAll(".voice-delete").forEach((button) => {
      button.addEventListener("click", () => deleteEnrollment(button.dataset.id));
    });
  } catch (error) {
    showError(body, error);
  }
}

async function grantVoiceConsent() {
  const result = $("voice-result");
  try {
    await api("/v1/training/consent", {
      method: "POST",
      body: JSON.stringify({ track: "voice_enrollment" }),
    });
    result.textContent = "voice consent granted";
    refreshVoiceStatus();
  } catch (error) {
    showError(result, error);
  }
}

async function recordVoiceSample() {
  const result = $("voice-result");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    result.textContent = "microphone capture is not available in this context";
    return;
  }
  if (voiceRecorder && voiceRecorder.state === "recording") {
    result.textContent = "already recording — wait for the sample to finish";
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    voiceChunks = [];
    voiceRecorder = new MediaRecorder(stream);
    voiceRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        voiceChunks.push(event.data);
      }
    };
    voiceRecorder.onstop = async () => {
      stream.getTracks().forEach((track) => track.stop());
      const blob = new Blob(voiceChunks, {
        type: voiceRecorder.mimeType || "audio/webm",
      });
      const buffer = await blob.arrayBuffer();
      voiceSamples.push(b64FromBuffer(buffer));
      updateVoiceSampleUi();
      result.textContent = `sample ${voiceSamples.length}/5 recorded (${blob.size} bytes)`;
    };
    voiceRecorder.start();
    setTimeout(() => {
      if (voiceRecorder && voiceRecorder.state === "recording") {
        voiceRecorder.stop();
      }
    }, 2000);
    result.textContent = "recording…";
  } catch (error) {
    showError(result, error);
  }
}

async function enrollVoiceprint() {
  const result = $("voice-result");
  if (voiceSamples.length < 5) {
    result.textContent = "record 5 samples first";
    return;
  }
  try {
    const livenessProof = $("voice-liveness").value;
    const liveScore = Number($("voice-live-score").value);
    const response = await api("/v1/voice/enroll", {
      method: "POST",
      body: JSON.stringify({
        samples: voiceSamples.map((sample) => ({
          audio_b64: sample,
          liveness_proof: livenessProof,
          live_score: liveScore,
        })),
        reason: "workbench enrollment",
      }),
    });
    voiceSamples = [];
    updateVoiceSampleUi();
    result.textContent = `enrolled v${response.enrollment.version} (${response.sample_count} samples, raw audio not stored)`;
    refreshVoiceStatus();
  } catch (error) {
    showError(result, error);
  }
}

async function revokeEnrollment(enrollmentId) {
  const result = $("voice-result");
  try {
    await api(`/v1/voice/enrollments/${enrollmentId}/revoke`, {
      method: "POST",
      body: JSON.stringify({ reason: "revoked from workbench" }),
    });
    result.textContent = "enrollment revoked";
    refreshVoiceStatus();
  } catch (error) {
    showError(result, error);
  }
}

async function deleteEnrollment(enrollmentId) {
  const result = $("voice-result");
  if (!window.confirm("Permanently delete this voiceprint? This cannot be undone.")) {
    return;
  }
  try {
    await api(`/v1/voice/enrollments/${enrollmentId}/delete`, {
      method: "POST",
      body: JSON.stringify({ reason: "deleted from workbench" }),
    });
    result.textContent = "voiceprint deleted";
    refreshVoiceStatus();
  } catch (error) {
    showError(result, error);
  }
}

// --------------------------------------------------------------------------- #
// Voice session (wake -> verify -> streaming utterance -> TTS)
// --------------------------------------------------------------------------- #

let voiceSessionId = null;
let voiceSessionNonce = null;
let voiceSessionConversationId = null;
let voiceSessionController = null;
let voiceHoldStream = null;
let voiceHoldRecorder = null;
let voiceHoldChunks = [];
let lastVoiceClipB64 = null;

function captureClip(seconds = 2) {
  return new Promise((resolve, reject) => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      reject(new Error("microphone capture is not available in this context"));
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then((stream) => {
        const chunks = [];
        let recorder;
        try {
          recorder = new MediaRecorder(stream);
        } catch (error) {
          stream.getTracks().forEach((track) => track.stop());
          reject(new Error("MediaRecorder unavailable: " + error.message));
          return;
        }
        recorder.ondataavailable = (event) => {
          if (event.data && event.data.size > 0) {
            chunks.push(event.data);
          }
        };
        recorder.onstop = async () => {
          stream.getTracks().forEach((track) => track.stop());
          const blob = new Blob(chunks, {
            type: recorder.mimeType || "audio/webm",
          });
          const buffer = await blob.arrayBuffer();
          resolve({
            b64: b64FromBuffer(buffer),
            blob,
            mime: recorder.mimeType || "audio/webm",
          });
        };
        recorder.onerror = () => {
          stream.getTracks().forEach((track) => track.stop());
          reject(new Error("recording failed"));
        };
        recorder.start();
        setTimeout(() => {
          if (recorder.state === "recording") {
            recorder.stop();
          }
        }, seconds * 1000);
      })
      .catch(reject);
  });
}

function renderVoiceSessionStatus(status) {
  $("voice-session-status").innerHTML =
    `<p><strong>state</strong> ${escapeHtml(status.state || "idle")} · ` +
    `owner_enrolled ${!!status.owner_enrolled} · ` +
    `owner_verified ${!!status.owner_verified}</p>`;
  $("voice-verify").disabled = !voiceSessionId;
  $("voice-talk").disabled = !voiceSessionId || !status.owner_verified;
  $("voice-end").disabled = !voiceSessionId;
  $("voice-session-refresh").disabled = !voiceSessionId;
}

async function voiceWake() {
  const result = $("voice-session-result");
  try {
    result.textContent = "waking…";
    const wake = await api("/v1/voice/wake", {
      method: "POST",
      body: JSON.stringify({
        device_id: "web-voice",
        wake_word: "evie",
        text_hint: "evie",
        priority: 0.5,
      }),
    });
    if (!wake.session_id) {
      result.textContent = wake.message || "wake not accepted";
      return;
    }
    voiceSessionId = wake.session_id;
    voiceSessionNonce = wake.challenge_nonce || null;
    renderVoiceSessionStatus({
      state: wake.state,
      owner_enrolled: wake.owner_enrolled,
      owner_verified: false,
    });
    result.textContent = wake.message || "wake accepted — verify ownership";
  } catch (error) {
    showError(result, error);
  }
}

async function voiceVerifyOwner() {
  const result = $("voice-session-result");
  if (!voiceSessionId) {
    result.textContent = "wake a session first";
    return;
  }
  try {
    result.textContent = "recording verification sample…";
    const clip = await captureClip(2);
    const verify = await api("/v1/voice/verify", {
      method: "POST",
      body: JSON.stringify({
        session_id: voiceSessionId,
        nonce: voiceSessionNonce || "web-verify",
        samples: [clip.b64],
        liveness_proof: "live",
        live_score: 0.9,
      }),
    });
    renderVoiceSessionStatus({
      state: verify.state,
      owner_enrolled: true,
      owner_verified: verify.verified,
    });
    result.textContent = verify.verified
      ? "owner verified — hold to talk"
      : `not verified: ${verify.reason}`;
  } catch (error) {
    showError(result, error);
  }
}

function voiceTalkDown() {
  if (!voiceSessionId) {
    $("voice-session-result").textContent = "wake + verify first";
    return;
  }
  const result = $("voice-session-result");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    result.textContent = "microphone capture is not available";
    return;
  }
  navigator.mediaDevices
    .getUserMedia({ audio: true })
    .then((stream) => {
      voiceHoldStream = stream;
      voiceHoldChunks = [];
      voiceHoldRecorder = new MediaRecorder(stream);
      voiceHoldRecorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) {
          voiceHoldChunks.push(event.data);
        }
      };
      voiceHoldRecorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        sendVoiceClip();
      };
      voiceHoldRecorder.start();
      result.textContent = "listening — release to send";
    })
    .catch((error) => showError(result, error));
}

function voiceTalkUp() {
  if (voiceHoldRecorder && voiceHoldRecorder.state === "recording") {
    voiceHoldRecorder.stop();
  }
}

async function sendVoiceClip() {
  if (!voiceSessionId) {
    return;
  }
  const result = $("voice-session-result");
  if (!voiceHoldChunks.length) {
    result.textContent = "no audio captured";
    return;
  }
  const blob = new Blob(voiceHoldChunks, {
    type: voiceHoldRecorder ? voiceHoldRecorder.mimeType || "audio/webm" : "audio/webm",
  });
  const buffer = await blob.arrayBuffer();
  const b64 = b64FromBuffer(buffer);
  lastVoiceClipB64 = b64;
  sendVoiceB64(b64);
}

function sendVoiceB64(b64) {
  if (!voiceSessionId) {
    return;
  }
  lastVoiceClipB64 = b64;
  const result = $("voice-session-result");
  result.textContent = "transcribing…";
  const streamEl = $("voice-stream");
  streamEl.innerHTML = "";
  $("voice-retry").hidden = true;
  const partials = [];
  const body = { session_id: voiceSessionId, audio_b64: b64, follow_up: false };
  if (voiceSessionConversationId) {
    body.conversation_id = voiceSessionConversationId;
  }
  voiceSessionController = postSse("/v1/voice/utterance/stream", body, {
    onEvent(name, data) {
      if (name === "partial") {
        partials.push(data);
        const p = document.createElement("div");
        p.className = "voice-partial" + (data.stable ? " stable" : "");
        p.textContent =
          (data.stable ? "stable: " : "partial: ") + (data.text || "");
        streamEl.appendChild(p);
        streamEl.scrollTop = streamEl.scrollHeight;
      } else if (name === "final_transcript") {
        const p = document.createElement("div");
        p.className = "voice-final";
        p.textContent =
          `transcript: ${data.text || ""} ` +
          `(${data.provider}, conf ${data.confidence}, degraded ${!!data.degraded})`;
        streamEl.appendChild(p);
      } else if (name === "reply") {
        const reply = document.createElement("div");
        reply.className = "voice-reply";
        reply.textContent = "reply: " + (data.reply || "");
        streamEl.appendChild(reply);
        if (data.conversation_id) {
          voiceSessionConversationId = data.conversation_id;
        }
        if (data.memory_deltas && data.memory_deltas.length) {
          const chips = document.createElement("div");
          chips.className = "chips";
          chips.innerHTML = data.memory_deltas
            .map(
              (delta) =>
                `<span class="chip">${escapeHtml(delta.memory_type)} · ` +
                `${escapeHtml(String(delta.text || "").slice(0, 80))}</span>`
            )
            .join("");
          streamEl.appendChild(chips);
        }
        const tts = data.tts || {};
        if (tts.audio_ref) {
          result.textContent = "playing audio…";
          playAudioRef(tts.audio_ref)
            .then(() => {
              result.textContent = "audio played";
            })
            .catch((error) => showError(result, error));
        } else {
          result.textContent =
            `reply ready — TTS ${tts.provider || "?"}` +
            `${tts.degraded ? " degraded" : ""} has no audio_ref; ` +
            "configure a real TTS provider";
        }
      }
    },
    onDone() {
      voiceSessionController = null;
    },
    onCancel() {
      voiceSessionController = null;
      result.textContent = "voice stream cancelled";
    },
    onError(error) {
      voiceSessionController = null;
      $("voice-retry").hidden = false;
      showError(result, error);
      if (streamEl.textContent) {
        streamEl.textContent += "\n[incomplete]";
      }
    },
  });
}

function voiceRetry() {
  if (lastVoiceClipB64 && voiceSessionId) {
    sendVoiceB64(lastVoiceClipB64);
  }
}

async function voiceSessionRefresh() {
  if (!voiceSessionId) {
    return;
  }
  try {
    const status = await api(`/v1/voice/sessions/${voiceSessionId}`);
    renderVoiceSessionStatus({
      state: status.state,
      owner_enrolled: status.owner_enrolled,
      owner_verified: status.owner_verified,
    });
    $("voice-session-result").textContent =
      `state ${status.state} · follow-up ${status.follow_up_remaining_seconds}s`;
  } catch (error) {
    showError($("voice-session-result"), error);
  }
}

async function voiceEndSession() {
  if (!voiceSessionId) {
    return;
  }
  try {
    await api(`/v1/voice/sessions/${voiceSessionId}/end`, { method: "POST" });
    voiceSessionId = null;
    voiceSessionNonce = null;
    renderVoiceSessionStatus({
      state: "ended",
      owner_enrolled: false,
      owner_verified: false,
    });
    $("voice-session-result").textContent = "session ended";
  } catch (error) {
    showError($("voice-session-result"), error);
  }
}

async function playAudioRef(audioRef) {
  const response = await fetch(
    baseUrl() + "/v1/voice/audio/" + encodeURIComponent(audioRef),
    { headers: authHeaders() }
  );
  if (!response.ok) {
    throw new Error(`audio fetch failed: ${response.status}`);
  }
  const arrayBuffer = await response.arrayBuffer();
  await playAudioBuffer(arrayBuffer);
}

async function playAudioBuffer(arrayBuffer) {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    throw new Error("WebAudio is not available in this browser");
  }
  const context = new AudioContextClass();
  const decoded = await context.decodeAudioData(arrayBuffer);
  const source = context.createBufferSource();
  source.buffer = decoded;
  source.connect(context.destination);
  source.start();
}

function testToneWavBuffer(seconds = 0.5, rate = 16000) {
  const sampleCount = Math.floor(rate * seconds);
  const dataSize = sampleCount * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  const writeString = (offset, value) => {
    for (let index = 0; index < value.length; index += 1) {
      view.setUint8(offset + index, value.charCodeAt(index));
    }
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, dataSize, true);
  for (let index = 0; index < sampleCount; index += 1) {
    const sample = Math.sin((2 * Math.PI * 440 * index) / rate) * 0.2;
    view.setInt16(44 + index * 2, Math.round(sample * 32767), true);
  }
  return buffer;
}

async function voiceAudioTest() {
  const result = $("voice-session-result");
  try {
    await playAudioBuffer(testToneWavBuffer());
    result.textContent =
      "audio output OK — played a 0.5s test tone (not EVIE audio; " +
      "real TTS requires a configured provider)";
  } catch (error) {
    showError(result, error);
  }
}

// --------------------------------------------------------------------------- #
// People (Agent 7 roster)
// --------------------------------------------------------------------------- #

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function peopleRefresh() {
  const list = $("people-list");
  try {
    const rows = await api("/v1/people/enrollments");
    list.innerHTML = rows
      .map(
        (row) =>
          `<div class="person-card">` +
          `<strong>${escapeHtml(row.person_name)}</strong> v${row.version} · ` +
          `${escapeHtml(row.status)} · ${row.sample_count} photos · ` +
          `${escapeHtml(row.algorithm)}${row.degraded ? " degraded" : ""}` +
          ` <button class="person-revoke" data-id="${row.id}">revoke</button>` +
          ` <button class="person-delete-enrollment" data-id="${row.id}">delete enrollment</button>` +
          ` <button class="person-delete" data-entity="${row.entity_id}">delete person</button>` +
          ` <button class="person-audit" data-name="${escapeHtml(row.person_name)}">audit memory</button>` +
          `</div>`
      )
      .join("");
    list.querySelectorAll(".person-revoke").forEach((button) => {
      button.addEventListener("click", () => {
        api(`/v1/people/enrollments/${button.dataset.id}/revoke?reason=web+revoke`, {
          method: "POST",
        })
          .then(() => {
            $("people-result").textContent = "enrollment revoked";
            peopleRefresh();
          })
          .catch((error) => showError($("people-result"), error));
      });
    });
    list.querySelectorAll(".person-delete-enrollment").forEach((button) => {
      button.addEventListener("click", () => {
        if (!window.confirm("Delete this enrollment? Templates are erased.")) {
          return;
        }
        api(`/v1/people/enrollments/${button.dataset.id}/delete?reason=web+delete`, {
          method: "POST",
        })
          .then(() => {
            $("people-result").textContent = "enrollment deleted";
            peopleRefresh();
          })
          .catch((error) => showError($("people-result"), error));
      });
    });
    list.querySelectorAll(".person-delete").forEach((button) => {
      button.addEventListener("click", () => {
        if (
          !window.confirm(
            "Permanently delete this person? Templates, samples, sightings and cached biodata are erased."
          )
        ) {
          return;
        }
        api(
          `/v1/people/${button.dataset.entity}?reason=${encodeURIComponent("web person deletion")}`,
          { method: "DELETE" }
        )
          .then(() => {
            $("people-result").textContent = "person erased";
            peopleRefresh();
          })
          .catch((error) => showError($("people-result"), error));
      });
    });
    list.querySelectorAll(".person-audit").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          const found = await api(
            "/v1/memories?q=" + encodeURIComponent(button.dataset.name) + "&limit=1"
          );
          const memory = (found.memories || [])[0];
          if (!memory) {
            $("people-result").textContent = "no memory found for this person";
            return;
          }
          showAudit(memory.id);
          $("audit-panel").scrollIntoView({ behavior: "smooth", block: "center" });
        } catch (error) {
          showError($("people-result"), error);
        }
      });
    });
  } catch (error) {
    showError($("people-result"), error);
  }
}

async function peopleEnroll() {
  const result = $("people-result");
  const name = $("people-name").value.trim();
  const files = $("people-photos").files;
  if (!name) {
    result.textContent = "enter a person name";
    return;
  }
  if (!files || files.length < 5) {
    result.textContent = "select at least 5 photos";
    return;
  }
  try {
    const photos = [];
    for (const file of files) {
      photos.push({
        image_b64: await fileToB64(file),
        quality: 0.99,
        confidence: 0.99,
        source: "web",
      });
    }
    const data = await api("/v1/people/enrollments", {
      method: "POST",
      body: JSON.stringify({
        person_name: name,
        photos,
        reason: "web enrollment",
      }),
    });
    result.textContent =
      `enrolled ${data.enrollment.person_name} v${data.enrollment.version} ` +
      `(${data.sample_count} photos, degraded=${data.degraded})`;
    $("people-name").value = "";
    $("people-photos").value = "";
    peopleRefresh();
  } catch (error) {
    showError(result, error);
  }
}

async function peopleCorrectRecognition() {
  const result = $("people-result");
  const recognitionId = $("people-recognition-id").value.trim();
  const label = $("people-correct-label").value.trim();
  if (!recognitionId || !label) {
    result.textContent = "enter a recognition id and corrected label";
    return;
  }
  try {
    const data = await api(`/v1/people/recognitions/${recognitionId}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        correct_label: label,
        reason: "web correction",
      }),
    });
    result.textContent = `recognition corrected -> ${data.label}`;
    $("people-recognition-id").value = "";
    $("people-correct-label").value = "";
    peopleRefresh();
  } catch (error) {
    showError(result, error);
  }
}

// --------------------------------------------------------------------------- #
// Integrations (Agent 12)
// --------------------------------------------------------------------------- #

async function integrationsRefresh() {
  try {
    const [catalog, rows] = await Promise.all([
      api("/v1/integrations/catalog"),
      api("/v1/integrations"),
    ]);
    const select = $("integration-adapter");
    select.innerHTML = (catalog || [])
      .map(
        (item) =>
          `<option value="${escapeHtml(item.adapter)}" ` +
          `data-scopes="${escapeHtml(JSON.stringify(item.default_scopes || []))}">` +
          `${escapeHtml(item.name)}</option>`
      )
      .join("");
    renderIntegrationList(rows || []);
  } catch (error) {
    showError($("integration-result"), error);
  }
}

function renderIntegrationList(rows) {
  const list = $("integration-list");
  list.innerHTML = rows
    .map(
      (row) =>
        `<div class="integration-card">` +
        `<strong>${escapeHtml(row.name)}</strong> (${escapeHtml(row.adapter)}) · ` +
        `status ${escapeHtml(row.status)} · privacy ${escapeHtml(row.privacy_level)}` +
        `<br><span class="muted">scopes: ${escapeHtml((row.scopes || []).join(", ") || "none")}</span>` +
        `<br><span class="muted">credential ${row.credential_configured ? "configured" : "missing"} · ` +
        `webhook ${row.webhook_configured ? "configured" : "missing"} · ` +
        `last used ${row.last_used_at || "never"}</span>` +
        `<br><button class="integration-scopes" data-id="${row.id}" ` +
        `data-scopes="${escapeHtml(JSON.stringify(row.scopes || []))}">scopes</button> ` +
        `<button class="integration-oauth" data-id="${row.id}">oauth</button> ` +
        `<button class="integration-sync" data-id="${row.id}">sync</button> ` +
        `<button class="integration-events" data-id="${row.id}">events</button> ` +
        `<button class="integration-revoke" data-id="${row.id}">revoke</button>` +
        `<div class="integration-detail" id="integration-detail-${row.id}"></div>` +
        `</div>`
    )
    .join("");
  list.querySelectorAll(".integration-scopes").forEach((button) => {
    button.addEventListener("click", async () => {
      const current = [];
      try {
        current.push(...JSON.parse(button.dataset.scopes || "[]"));
      } catch {
        // ignore malformed stored scopes
      }
      const input = window.prompt("Scopes (comma separated)", current.join(", "));
      if (!input) {
        return;
      }
      const scopes = input
        .split(",")
        .map((scope) => scope.trim())
        .filter(Boolean);
      if (!scopes.length) {
        return;
      }
      try {
        await api(`/v1/integrations/${button.dataset.id}/scopes`, {
          method: "PATCH",
          body: JSON.stringify({ scopes }),
        });
        $("integration-result").textContent = "scopes updated";
        integrationsRefresh();
      } catch (error) {
        showError($("integration-result"), error);
      }
    });
  });
  list.querySelectorAll(".integration-oauth").forEach((button) => {
    button.addEventListener("click", async () => {
      const detail = $("integration-detail-" + button.dataset.id);
      detail.textContent = "starting OAuth…";
      try {
        const data = await api(
          `/v1/integrations/oauth/authorize?integration_id=${button.dataset.id}`
        );
        window.open(data.authorize_url, "_blank", "noopener");
        detail.textContent =
          `OAuth started — complete authorization in the new tab ` +
          `(state expires ${data.expires_at})`;
      } catch (error) {
        detail.textContent = "oauth start failed: " + error.message;
      }
    });
  });
  list.querySelectorAll(".integration-sync").forEach((button) => {
    button.addEventListener("click", async () => {
      const detail = $("integration-detail-" + button.dataset.id);
      detail.textContent = "syncing…";
      try {
        const data = await api(
          `/v1/integrations/${button.dataset.id}/sync?days=7`,
          { method: "POST" }
        );
        detail.textContent =
          `synced ${data.events_ingested || data.accepted || "?"} events`;
      } catch (error) {
        detail.textContent = "sync requires owner re-verification: " + error.message;
      }
    });
  });
  list.querySelectorAll(".integration-events").forEach((button) => {
    button.addEventListener("click", async () => {
      const detail = $("integration-detail-" + button.dataset.id);
      try {
        const events = await api(
          `/v1/integrations/${button.dataset.id}/events?limit=10`
        );
        detail.innerHTML = (events || [])
          .map(
            (event) =>
              `<div class="muted">${escapeHtml(event.occurred_at)} ` +
              `${escapeHtml(event.event_type)}: ${escapeHtml(JSON.stringify(event.payload || {}))}</div>`
          )
          .join("");
      } catch (error) {
        detail.textContent = error.message;
      }
    });
  });
  list.querySelectorAll(".integration-revoke").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Revoke this integration? Credentials are erased.")) {
        return;
      }
      try {
        await api(`/v1/integrations/${button.dataset.id}?reason=${encodeURIComponent("web revoke")}`, {
          method: "DELETE",
        });
        $("integration-result").textContent = "integration revoked";
        integrationsRefresh();
      } catch (error) {
        showError($("integration-result"), error);
      }
    });
  });
}

async function integrationConnect() {
  const result = $("integration-result");
  const select = $("integration-adapter");
  const option = select.selectedOptions[0];
  if (!option) {
    result.textContent = "no adapters in catalog";
    return;
  }
  let scopes = [];
  try {
    scopes = JSON.parse(option.dataset.scopes || "[]");
  } catch {
    scopes = [];
  }
  try {
    await api("/v1/integrations", {
      method: "POST",
      body: JSON.stringify({
        adapter: option.value,
        name: option.textContent.trim(),
        scopes: scopes.length ? scopes : ["read"],
        privacy_level: "normal",
      }),
    });
    result.textContent = "integration installed — configure credentials/oauth";
    integrationsRefresh();
  } catch (error) {
    showError(result, error);
  }
}

// --------------------------------------------------------------------------- #
// Routines (Agent 14)
// --------------------------------------------------------------------------- #

async function routinesRefresh() {
  try {
    const [overview, rows] = await Promise.all([
      api("/v1/routines/overview"),
      api("/v1/routines"),
    ]);
    $("routine-result").textContent =
      `routines ${overview.routines_total} · enabled ${overview.routines_enabled} · ` +
      `runs/24h ${overview.runs_last_24h} · awaiting approval ${overview.awaiting_approval}`;
    renderRoutineList(rows || []);
  } catch (error) {
    showError($("routine-result"), error);
  }
}

function renderRoutineList(rows) {
  const list = $("routine-list");
  list.innerHTML = rows
    .map(
      (row) =>
        `<li><strong>${escapeHtml(row.name)}</strong> ` +
        `${row.enabled ? "enabled" : "disabled"} · ${escapeHtml(row.kind)} · ` +
        `${escapeHtml(row.schedule || "")} · ${escapeHtml(row.action_type)}` +
        ` <button class="routine-run" data-id="${row.id}">run</button>` +
        ` <button class="routine-toggle" data-id="${row.id}" data-enabled="${row.enabled}">` +
        `${row.enabled ? "disable" : "enable"}</button></li>`
    )
    .join("");
  list.querySelectorAll(".routine-run").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const run = await api(`/v1/routines/${button.dataset.id}/run`, {
          method: "POST",
        });
        $("routine-result").textContent =
          `routine run ${run.id} status ${run.status}`;
        routinesRefresh();
      } catch (error) {
        showError($("routine-result"), error);
      }
    });
  });
  list.querySelectorAll(".routine-toggle").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        const enabled = button.dataset.enabled === "true";
        await api(`/v1/routines/${button.dataset.id}/${enabled ? "disable" : "enable"}`, {
          method: "POST",
        });
        routinesRefresh();
      } catch (error) {
        showError($("routine-result"), error);
      }
    });
  });
}

async function routineCreate() {
  const result = $("routine-result");
  const name = $("routine-name").value.trim();
  const actionType = $("routine-action").value.trim();
  const schedule = $("routine-schedule").value.trim();
  if (!name || !actionType) {
    result.textContent = "routine name and action_type are required";
    return;
  }
  try {
    const body = { name, kind: "scheduled", action_type: actionType };
    if (schedule) {
      body.schedule = schedule;
    }
    const row = await api("/v1/routines", {
      method: "POST",
      body: JSON.stringify(body),
    });
    result.textContent = `created routine ${row.id} (${row.name})`;
    $("routine-name").value = "";
    $("routine-action").value = "";
    $("routine-schedule").value = "";
    routinesRefresh();
  } catch (error) {
    showError(result, error);
  }
}

async function routineTemplates() {
  const body = $("routine-template-list");
  try {
    const rows = await api("/v1/routines/templates");
    body.innerHTML = (rows || [])
      .map(
        (row) =>
          `<li><strong>${escapeHtml(row.key || row.id || "?")}</strong> ` +
          `${escapeHtml(row.name || "")} — ${escapeHtml(row.description || "")}</li>`
      )
      .join("");
  } catch (error) {
    showError(body, error);
  }
}

// --------------------------------------------------------------------------- #
// HUD console: ticker, health, focus, alerts, gear, models, notifications
// --------------------------------------------------------------------------- #

let consoleTimer = null;

function fulfill(parts, index) {
  return parts[index] && parts[index].status === "fulfilled"
    ? parts[index].value
    : null;
}

function failReason(parts, index) {
  return parts[index] && parts[index].status === "rejected"
    ? parts[index].reason.message
    : "";
}

function tileHtml(title, value) {
  if (value === null || value === undefined || value === "") {
    return `<div class="muted">${escapeHtml(title)}: —</div>`;
  }
  return `<div>${escapeHtml(title)}: ${escapeHtml(String(value))}</div>`;
}

async function refreshConsole() {
  const parts = await Promise.allSettled([
    api("/v1/hud/card"),
    api("/v1/live/status"),
    api("/v1/alerts"),
    api("/v1/hud/alerts"),
    api("/v1/focus"),
    api("/v1/hud/focus"),
    api("/v1/health/summary"),
    api("/v1/gear"),
    api("/v1/gateway/models"),
    api("/v1/gateway/stats"),
    api("/v1/ops/metrics"),
    api("/v1/runtime/notify/status"),
    api("/v1/runtime/notifications"),
    api("/v1/runtime/health"),
  ]);
  const [
    hud,
    live,
    alerts,
    hudAlerts,
    focus,
    hudFocus,
    health,
    gear,
    models,
    gatewayStats,
    ops,
    notifyStatus,
    notifications,
    runtimeHealth,
  ] = parts;

  const liveValue = fulfill(parts, 1);
  const alertsValue = fulfill(parts, 2);
  const hudAlertsValue = fulfill(parts, 3);
  const focusValue = fulfill(parts, 4) || fulfill(parts, 5);
  const healthValue = fulfill(parts, 6);
  const gearValue = fulfill(parts, 7);
  const modelsValue = fulfill(parts, 8);
  const gatewayStatsValue = fulfill(parts, 9);
  const opsValue = fulfill(parts, 10);
  const notifyStatusValue = fulfill(parts, 11);
  const notificationsValue = fulfill(parts, 12);
  const runtimeHealthValue = fulfill(parts, 13);

  const liveText = liveValue
    ? `${liveValue.total_events_24h || 0} events/24h · ` +
      (liveValue.channels || []).length + " channels"
    : failReason(parts, 1) || "unavailable";
  $("ticker-live").textContent = "live: " + liveText;
  $("ticker-alerts").textContent =
    "alerts: " + (alertsValue ? alertsValue.length : failReason(parts, 2) || "—");
  $("ticker-focus").textContent =
    "focus: " +
    (focusValue && focusValue.focus
      ? focusValue.focus.label || focusValue.focus.text || JSON.stringify(focusValue.focus)
      : focusValue && focusValue.label
        ? focusValue.label
        : "none");

  const healthEl = $("health-tiles");
  healthEl.innerHTML =
    tileHtml("readiness", healthValue && healthValue.readiness != null ? healthValue.readiness : "") +
    tileHtml("band", healthValue && healthValue.band) +
    tileHtml("sleep h", healthValue && healthValue.sleep_hours) +
    tileHtml("hrv ms", healthValue && healthValue.hrv_ms) +
    tileHtml("resting hr", healthValue && healthValue.resting_hr) +
    (healthValue && healthValue.recommendation
      ? `<div class="muted">${escapeHtml(healthValue.recommendation)}</div>`
      : "") +
    (runtimeHealthValue
      ? `<div class="muted">runtime ${escapeHtml(runtimeHealthValue.status || runtimeHealthValue.state || "?")}</div>`
      : "");

  const focusEl = $("focus-tile");
  if (focusValue) {
    focusEl.innerHTML =
      tileHtml("label", focusValue.focus ? focusValue.focus.label : focusValue.label) +
      tileHtml("locked", focusValue.locked != null ? focusValue.locked : "") +
      tileHtml("next action", focusValue.next_action) +
      (focusValue.context ? `<div class="muted">${escapeHtml(focusValue.context)}</div>` : "");
  } else {
    focusEl.innerHTML = `<div class="muted">focus: ${failReason(parts, 4) || "—"}</div>`;
  }

  const alertsEl = $("alert-tiles");
  const alertRows = hudAlertsValue || alertsValue || [];
  alertsEl.innerHTML = (alertRows.slice ? alertRows.slice(0, 8) : [])
    .map(
      (alert) =>
        `<div class="alert-row"><span class="tier ${escapeHtml(alert.tier || "background")}">` +
        `${escapeHtml(alert.tier || "alert")}</span> ${escapeHtml(alert.title || alert.body || "")}</div>`
    )
    .join("") || `<div class="muted">alerts: ${failReason(parts, 3) || "none"}</div>`;

  const gearEl = $("gear-tiles");
  gearEl.innerHTML = (gearValue || [])
    .slice(0, 6)
    .map(
      (item) =>
        `<div>${escapeHtml(item.device_id)}: bat ${item.battery_percent ?? "?"}% · ` +
        `cpu ${item.cpu_percent ?? "?"}% · mem ${item.memory_used_percent ?? "?"}% · ` +
        `up ${item.uptime_seconds ?? "?"}s</div>`
    )
    .join("") || `<div class="muted">gear: ${failReason(parts, 7) || "no snapshots"}</div>`;

  const modelsEl = $("model-tiles");
  const arbiter =
    (opsValue && (opsValue.arbiter || opsValue.models_residency)) || {};
  modelsEl.innerHTML =
    tileHtml("provider", modelsValue && modelsValue.provider) +
    `<div class="muted">models: ${escapeHtml(((modelsValue && modelsValue.models) || []).join(", ") || "—")}</div>` +
    (gatewayStatsValue
      ? tileHtml("p50 ms", gatewayStatsValue.latency && gatewayStatsValue.latency.p50_ms) +
        tileHtml("p95 ms", gatewayStatsValue.latency && gatewayStatsValue.latency.p95_ms) +
        tileHtml("calls/24h", gatewayStatsValue.calls && gatewayStatsValue.calls.total)
      : "") +
    (opsValue
      ? tileHtml("cost/30d $", opsValue.cost && opsValue.cost.last_30d_usd)
      : "") +
    (arbiter.resident_total_mb != null
      ? tileHtml("resident MB", arbiter.resident_total_mb)
      : "") +
    (arbiter.ceiling_mb != null ? tileHtml("ceiling MB", arbiter.ceiling_mb) : "") +
    (arbiter.backend ? tileHtml("backend", arbiter.backend) : "") +
    (arbiter.resident_by_tier_mb
      ? `<div class="muted">tiers: ${escapeHtml(JSON.stringify(arbiter.resident_by_tier_mb))}</div>`
      : "") +
    `<div class="muted">residency: ${
      arbiter.resident_total_mb != null
        ? "live arbiter stats"
        : "arbiter stats not exposed by API yet (Agent 2)"
    }</div>`;

  const notifyEl = $("notification-tiles");
  notifyEl.innerHTML =
    tileHtml("backend", notifyStatusValue && notifyStatusValue.backend) +
    tileHtml("available", notifyStatusValue && notifyStatusValue.available) +
    tileHtml("delivered today", notifyStatusValue && notifyStatusValue.delivered_today) +
    tileHtml("suppressed today", notifyStatusValue && notifyStatusValue.suppressed_today) +
    tileHtml("failed today", notifyStatusValue && notifyStatusValue.failed_today);
  const notifyList = $("notification-list");
  notifyList.innerHTML = (notificationsValue || [])
    .slice(0, 10)
    .map(
      (row) =>
        `<li><span class="receipt ${escapeHtml(row.status)}">${escapeHtml(row.status)}</span> ` +
        `${escapeHtml(row.title)} · ${escapeHtml(row.tier)} · ` +
        `<span class="muted">${escapeHtml(row.queued_at)}${row.reason ? " · " + escapeHtml(row.reason) : ""}</span></li>`
    )
    .join("");

  if (hud && hud.status === "fulfilled") {
    $("hud-schema").textContent = hud.value.schema_version;
    $("hud-body").innerHTML = renderSchemaCard(hud.value);
  }
}

function startConsoleLoop() {
  if (consoleTimer) {
    clearInterval(consoleTimer);
  }
  refreshConsole();
  consoleTimer = setInterval(refreshConsole, 15000);
}

function updateClock() {
  const clock = $("ev-clock");
  if (clock) {
    clock.textContent = new Date().toLocaleTimeString();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("api-url").value = store.url;
  $("api-key").value = store.key;
  $("connection-form").addEventListener("submit", connect);
  $("onboarding-add").addEventListener("click", addOnboardingText);
  $("onboarding-finish").addEventListener("click", finishOnboarding);
  $("onboarding-check").addEventListener("click", onboardingReadiness);
  $("wizard-next").addEventListener("click", wizardNext);
  $("wizard-back").addEventListener("click", wizardBack);
  $("capture-form").addEventListener("submit", capture);
  $("sync-queue").addEventListener("click", syncQueue);
  $("hud-quick").addEventListener("click", () => loadHud("quick"));
  $("hud-briefing").addEventListener("click", () => loadHud("briefing"));
  $("hud-focus").addEventListener("click", () => loadHud("focus"));
  $("hud-route").addEventListener("click", () => loadHud("route"));
  $("ask-form").addEventListener("submit", ask);
  $("ask-cancel").addEventListener("click", () => {
    if (askController) {
      askController.abort();
    }
  });
  $("ask-retry").addEventListener("click", askRetry);
  $("conversation-form").addEventListener("submit", sendConversation);
  $("conversation-cancel").addEventListener("click", () => {
    if (conversationController) {
      conversationController.abort();
    }
  });
  $("conversation-retry").addEventListener("click", conversationRetry);
  $("personality-save").addEventListener("click", savePersonality);
  $("recovery-codes").addEventListener("click", showRecoveryCodes);
  $("vault-rotate").addEventListener("click", rotateVault);
  $("memory-load").addEventListener("click", browseMemories);
  $("timeline-load").addEventListener("click", refreshTimeline);
  $("transparency-load").addEventListener("click", loadTransparency);
  $("voice-status").addEventListener("click", refreshVoiceStatus);
  $("voice-consent").addEventListener("click", grantVoiceConsent);
  $("voice-record").addEventListener("click", recordVoiceSample);
  $("voice-reset").addEventListener("click", () => {
    voiceSamples = [];
    updateVoiceSampleUi();
    $("voice-result").textContent = "samples cleared";
  });
  $("voice-enroll").addEventListener("click", enrollVoiceprint);
  $("voice-wake").addEventListener("click", voiceWake);
  $("voice-verify").addEventListener("click", voiceVerifyOwner);
  $("voice-talk").addEventListener("pointerdown", voiceTalkDown);
  $("voice-talk").addEventListener("pointerup", voiceTalkUp);
  $("voice-talk").addEventListener("pointerleave", voiceTalkUp);
  $("voice-end").addEventListener("click", voiceEndSession);
  $("voice-session-refresh").addEventListener("click", voiceSessionRefresh);
  $("voice-retry").addEventListener("click", voiceRetry);
  $("voice-audio-test").addEventListener("click", voiceAudioTest);
  $("people-refresh").addEventListener("click", peopleRefresh);
  $("people-enroll").addEventListener("click", peopleEnroll);
  $("people-correct").addEventListener("click", peopleCorrectRecognition);
  $("integration-refresh").addEventListener("click", integrationsRefresh);
  $("integration-connect").addEventListener("click", integrationConnect);
  $("routine-refresh").addEventListener("click", routinesRefresh);
  $("routine-create").addEventListener("click", routineCreate);
  $("routine-templates").addEventListener("click", routineTemplates);
  $("notifications-refresh").addEventListener("click", refreshConsole);
  refreshHud();
  refreshTimeline();
  updateVoiceSampleUi();
  updateQueueStatus();
  renderOnboardingList();
  renderWizard();
  loadConversation();
  loadSettings();
  peopleRefresh();
  integrationsRefresh();
  routinesRefresh();
  renderVoiceSessionStatus({});
  startConsoleLoop();
  updateClock();
  setInterval(updateClock, 1000);
});

window.EV = {
  queueCapture,
  syncQueue,
  readQueue,
  addOnboardingText,
  finishOnboarding,
  readOnboardingTexts,
  onboardingReadiness,
  wizardNext,
  wizardBack,
  loadHud,
  loadConversation,
  sendConversation,
  loadSettings,
  savePersonality,
  showRecoveryCodes,
  rotateVault,
  postSse,
  renderSchemaCard,
  refreshConsole,
  voiceWake,
  voiceVerifyOwner,
  voiceTalkDown,
  voiceTalkUp,
  voiceEndSession,
  voiceSessionRefresh,
  voiceRetry,
  voiceAudioTest,
  peopleRefresh,
  peopleEnroll,
  peopleCorrectRecognition,
  integrationsRefresh,
  integrationConnect,
  routinesRefresh,
  routineCreate,
  routineTemplates,
  askRetry,
  conversationRetry,
};
