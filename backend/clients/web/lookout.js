"use strict";

const ASK_POOL = [
  "What did I decide about SQLite?",
  "Should I leave by 10:25?",
  "Is sleep still short tonight?",
  "What's the live risk on this call?",
  "Do I have anything on the watchlist?",
  "Where did we land on the enclosure?",
  "Can this wait until morning?",
  "Who am I seeing at 11?",
];

const REPLY_POOL = [
  "You chose SQLite on August 9 for a zero-config loop.",
  "Yes — leave by 10:25 if you want a quiet buffer.",
  "Sleep was 5h40. Protect an early night.",
  "Scope creep. Cap it in writing before rate talk.",
  "Two signals. Contract Friday, PETG below reorder.",
  "Keep the current PETG spec. Reorder is the action.",
  "It can wait. Nothing here is urgent.",
  "Renegotiation with X. Talking points are ready.",
];

const DEMO_WINDOWS = [
  {
    id: "lookout-vitals",
    kind: "vitals",
    size: "lookout",
    time_type: "lookout",
    placement: "upper_left",
    title: "Vitals",
    body: "Readiness 68 · short sleep, HRV up. Protect an early night.",
    items: ["Sleep 5h40", "HRV 48ms", "Resting HR 61"],
    lookout: true,
    source: "health_radar",
    layout: "field",
  },
  {
    id: "lookout-radar",
    kind: "radar",
    size: "lookout",
    time_type: "lookout",
    placement: "upper_right",
    title: "Radar",
    body: "Two signals on the watchlist.",
    items: ["Contract renewal Friday", "Enclosure PETG below reorder"],
    questions: ["Do I have anything on the watchlist?"],
    response: "Two signals. Contract Friday, PETG below reorder.",
    lookout: true,
    source: "alert_radar",
    layout: "ask",
  },
  {
    id: "lookout-horizon",
    kind: "horizon",
    size: "lookout",
    time_type: "lookout",
    placement: "lower_right",
    title: "Horizon",
    body: "Next: renegotiation with X at 11:00.",
    items: ["Leave by 10:25", "Talking points ready"],
    questions: ["Who am I seeing at 11?"],
    response: "Renegotiation with X. Talking points are ready.",
    lookout: true,
    source: "calendar",
    layout: "reply",
  },
  {
    id: "hud-brief",
    kind: "briefing",
    size: "brief",
    time_type: "linger",
    placement: "center",
    title: "Renegotiation with X",
    body: "Two prior fixed-term wins. Scope creep is the live risk.",
    recommendation: "Cap scope in writing. Option A: fixed + milestones.",
    items: ["History matches Option A", "Do not reopen the rate"],
    questions: ["What's the live risk on this call?"],
    response: "Scope creep. Cap it in writing before rate talk.",
    lookout: false,
    source: "tactical",
    layout: "split",
  },
];

const GALLERY = [
  {
    id: "gal-chip",
    kind: "chip",
    size: "chip",
    time_type: "flash",
    placement: "upper_right",
    title: "Got it",
    body: "Saved to memory.",
    layout: "pulse",
    why: "A flash confirmation. Gone in 1.6s.",
  },
  {
    id: "gal-pulse",
    kind: "pulse",
    size: "chip",
    time_type: "pulse",
    placement: "top",
    title: "Now",
    body: "Heart rate is outside your safe band.",
    lookout: true,
    layout: "pulse",
    why: "Urgent pulse — even if you did not ask.",
  },
  {
    id: "gal-card",
    kind: "card",
    size: "card",
    time_type: "linger",
    placement: "center",
    title: "Status",
    questions: ["Can this wait until morning?"],
    response: "It can wait. Nothing here is urgent.",
    body: "One answer. Lingers about 30 seconds.",
    layout: "ask",
    why: "Question-led folio when you say show me.",
  },
  {
    id: "gal-brief",
    kind: "briefing",
    size: "brief",
    time_type: "linger",
    placement: "center",
    title: "Renegotiation with X",
    body: "Two prior fixed-term wins. Scope creep is the live risk.",
    recommendation: "Cap scope in writing.",
    items: ["Option A matches history", "Do not reopen the rate"],
    questions: ["What's the live risk on this call?"],
    response: "Scope creep. Cap it in writing before rate talk.",
    layout: "split",
    why: "Split: the ask on the left, the reply on the right.",
  },
  {
    id: "gal-vitals",
    kind: "vitals",
    size: "lookout",
    time_type: "lookout",
    placement: "upper_left",
    title: "Vitals",
    body: "Helio → Apple Health. Readiness 68.",
    items: ["Sleep 5h40", "HRV 48ms", "Resting HR 61", "SpO2 97%"],
    lookout: true,
    source: "amazfit_helio",
    layout: "field",
    why: "Body-scan lookout. Stays until you dismiss it.",
  },
  {
    id: "gal-radar",
    kind: "radar",
    size: "lookout",
    time_type: "lookout",
    placement: "upper_right",
    title: "Radar",
    body: "Baby Monitor — watching deadlines.",
    items: ["Contract renewal Friday"],
    questions: ["Do I have anything on the watchlist?"],
    response: "One signal. Contract Friday.",
    lookout: true,
    layout: "ledger",
    why: "Persistent watch without a dashboard.",
  },
  {
    id: "gal-horizon",
    kind: "horizon",
    size: "lookout",
    time_type: "lookout",
    placement: "lower_right",
    title: "Horizon",
    body: "Next: renegotiation at 11:00.",
    items: ["Leave by 10:25"],
    questions: ["Should I leave by 10:25?"],
    response: "Yes — leave by 10:25 if you want a quiet buffer.",
    lookout: true,
    layout: "reply",
    why: "What's next. Corner lookout.",
  },
  {
    id: "gal-map",
    kind: "map",
    size: "canvas",
    time_type: "hold",
    placement: "center",
    title: "Route",
    body: "Canvas map when coordinates exist.",
    layout: "field",
    why: "Large plot, until dismissed.",
  },
  {
    id: "gal-trace",
    kind: "trace",
    size: "slate",
    time_type: "hold",
    placement: "center",
    title: "Trace",
    body: "Why EVIE knows this, with sources.",
    items: ["voice note 2026-08-09", "supersedes July 3"],
    questions: ["What did I decide about SQLite?"],
    response: "You chose SQLite on August 9 for a zero-config loop.",
    layout: "stack",
    why: "Audit slate.",
  },
  {
    id: "gal-ticker",
    kind: "ticker",
    size: "ticker",
    time_type: "glance",
    placement: "top",
    title: "Leave in 12 minutes",
    body: "Glance bar across the top.",
    layout: "ribbon",
    why: "Five-second ticker.",
  },
];

function params() {
  return new URLSearchParams(window.location.search);
}

function parsePlan() {
  const hash = window.location.hash.replace(/^#/, "");
  if (hash) {
    try {
      return JSON.parse(decodeURIComponent(hash));
    } catch (_err) {
      return null;
    }
  }
  return null;
}

function splitPipe(value) {
  return String(value || "")
    .split(/[|\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function stableInt(key) {
  let value = 5381;
  const text = String(key || "");
  for (let index = 0; index < text.length; index += 1) {
    value = (((value << 5) + value) + text.charCodeAt(index)) >>> 0;
  }
  return value;
}

function pickDrift(win) {
  if (win.drift_x != null || win.dx != null) {
    return {
      x: Number(win.drift_x != null ? win.drift_x : win.dx) || 0,
      y: Number(win.drift_y != null ? win.drift_y : win.dy) || 0,
      tilt: Number(win.tilt) || 0,
    };
  }
  const seed = stableInt(win.id || win.title || "folio");
  const placement = win.placement || "center";
  if (placement === "top") {
    return { x: (seed % 41) - 20, y: 0, tilt: 0 };
  }
  if (placement === "center") {
    return { x: (seed % 49) - 24, y: ((seed >>> 8) % 37) - 18, tilt: (((seed >>> 14) % 29) - 14) / 16 };
  }
  return {
    x: (seed % 73) - 36,
    y: ((seed >>> 7) % 61) - 30,
    tilt: (((seed >>> 14) % 29) - 14) / 10,
  };
}

function pickLayout(win) {
  if (win.layout) return win.layout;
  const kind = win.kind || "card";
  if (kind === "ticker" || kind === "conversation") return "ribbon";
  if (kind === "chip" || kind === "pulse") return "pulse";
  if (kind === "map") return "field";
  const asks = win.questions || [];
  const reply = (win.response || win.body || "").trim();
  const seed = stableInt(win.id || win.title || "folio");
  let pool = ["reply", "stack"];
  if (asks.length && reply) pool = ["ask", "reply", "split", "ledger", "stack"];
  else if (asks.length) pool = ["ask", "stack", "ledger"];
  else if ((win.items || []).length) pool = ["stack", "field", "ledger"];
  return pool[seed % pool.length];
}

function pickRandom(list, salt) {
  const index = Math.abs(stableInt(String(Date.now()) + salt) + Math.floor(Math.random() * 97)) % list.length;
  return list[index];
}

function windowFromQuery(search) {
  const get = (name, fallback = "") => search.get(name) || fallback;
  const items = splitPipe(get("items"));
  const questions = splitPipe(get("questions"));
  return {
    id: get("id", "lookout-card"),
    kind: get("kind", "card"),
    size: get("size", "card"),
    time_type: get("time", "linger"),
    placement: get("place", "center"),
    title: get("title", "EVIE"),
    body: get("body", ""),
    items,
    questions,
    response: get("response") || "",
    recommendation: get("recommendation"),
    source: get("source"),
    lookout: get("lookout") === "1",
    ttl_ms: get("ttl") ? Number(get("ttl")) : null,
    layout: get("layout") || "",
    drift_x: get("dx") ? Number(get("dx")) : null,
    drift_y: get("dy") ? Number(get("dy")) : null,
    tilt: get("tilt") ? Number(get("tilt")) : null,
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function asksOf(win) {
  const listed = (win.questions || []).filter(Boolean);
  if (listed.length) return listed;
  const blob = String(win.body || "");
  if (blob.includes("?")) {
    return blob.split(/(?<=\?)/).map((part) => part.trim()).filter((part) => part.endsWith("?"));
  }
  return [];
}

function replyOf(win) {
  return String(win.response || "").trim() || String(win.body || "").trim();
}

function notesMarkup(items) {
  return (items || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
}

function askBlock(questions) {
  if (!questions.length) return "";
  return questions
    .map((question) => `<p class="ask"><span class="ask-label">ask</span>${escapeHtml(question)}</p>`)
    .join("");
}

function replyBlock(text) {
  if (!text) return "";
  return `<p class="reply"><span class="reply-label">evie</span>${escapeHtml(text)}</p>`;
}

function composeInner(win) {
  const layout = pickLayout(win);
  const questions = asksOf(win);
  const reply = replyOf(win);
  const rec = win.recommendation
    ? `<p class="steer">${escapeHtml(win.recommendation)}</p>`
    : "";
  const items = win.items || [];
  if (layout === "split" && questions.length && reply) {
    return `
      <div class="split">
        <div>${askBlock(questions)}</div>
        <div>${replyBlock(reply)}${rec}<ul class="notes">${notesMarkup(items)}</ul></div>
      </div>`;
  }
  if (layout === "ask") {
    const shown = questions.length ? questions : [win.title];
    return `${askBlock(shown)}${replyBlock(reply)}${rec}<ul class="notes">${notesMarkup(items)}</ul>`;
  }
  if (layout === "reply") {
    return `<h2 class="folio-title">${escapeHtml(win.title || "EVIE")}</h2>${replyBlock(reply)}${askBlock(questions)}${rec}<ul class="notes">${notesMarkup(items)}</ul>`;
  }
  if (layout === "ledger") {
    const rows = (questions.length ? questions : [win.title]).map((question, index) => {
      const answer = index === 0 ? reply : (items[index - 1] || "");
      return `<div class="ledger-row">${askBlock([question])}${answer ? replyBlock(answer) : ""}</div>`;
    }).join("");
    return `<div class="ledger">${rows}${rec}</div>`;
  }
  if (layout === "field") {
    return `<h2 class="folio-title">${escapeHtml(win.title || "EVIE")}</h2><p class="body">${escapeHtml(win.body || reply)}</p><ul class="notes field-notes">${notesMarkup(items)}</ul>${rec}`;
  }
  if (layout === "ribbon" || layout === "pulse") {
    const line = questions[0] || win.title;
    const rest = reply && reply !== line ? replyBlock(reply) : `<p class="body">${escapeHtml(win.body || "")}</p>`;
    return `<p class="ask">${escapeHtml(line)}</p>${rest}`;
  }
  return `<h2 class="folio-title">${escapeHtml(win.title || "EVIE")}</h2>${askBlock(questions)}${replyBlock(reply || win.body)}${rec}<ul class="notes">${notesMarkup(items)}</ul>`;
}

function driftStyle(win) {
  const drift = pickDrift(win);
  return `--dx:${drift.x}px;--dy:${drift.y}px;--tilt:${drift.tilt}deg`;
}

function renderSingle(win) {
  document.title = `${win.title} — EVIE`;
  const kind = document.getElementById("kind");
  const time = document.getElementById("time");
  const source = document.getElementById("source");
  const compose = document.getElementById("compose");
  const live = document.getElementById("live");
  const ttl = document.getElementById("ttl");
  const root = document.getElementById("lookout");
  if (!kind || !compose) return;
  kind.textContent = String(win.kind || "card");
  time.textContent = String(win.time_type || "hold");
  source.textContent = win.source || "";
  compose.innerHTML = composeInner(win);
  compose.className = `compose layout-${escapeHtml(pickLayout(win))}`;
  if (root) root.setAttribute("style", driftStyle(win));
  if (win.lookout || win.time_type === "pulse" || win.time_type === "lookout") {
    live.hidden = false;
  }
  if (win.ttl_ms && ttl) {
    ttl.hidden = false;
    const bar = ttl.querySelector("span");
    bar.style.transition = `transform ${win.ttl_ms}ms linear`;
    requestAnimationFrame(() => {
      bar.style.transform = "scaleX(0)";
    });
    window.setTimeout(() => window.close(), win.ttl_ms);
  }
}

function lookoutHref(win) {
  const query = new URLSearchParams({
    id: win.id || "",
    title: win.title || "EVIE",
    body: win.body || "",
    kind: win.kind || "card",
    size: win.size || "card",
    time: win.time_type || "hold",
    place: win.placement || "center",
    lookout: win.lookout ? "1" : "0",
    items: (win.items || []).join("|"),
    questions: (win.questions || []).join("|"),
    recommendation: win.recommendation || "",
    source: win.source || "",
    response: win.response || "",
    layout: pickLayout(win),
  });
  if (win.ttl_ms) query.set("ttl", String(win.ttl_ms));
  const drift = pickDrift(win);
  query.set("dx", String(drift.x));
  query.set("dy", String(drift.y));
  query.set("tilt", String(drift.tilt));
  return `/app/lookout?${query.toString()}`;
}

function popFeatures(win) {
  const sizes = {
    pip: [180, 72],
    chip: [280, 148],
    card: [500, 320],
    brief: [580, 440],
    slate: [740, 540],
    canvas: [980, 700],
    lookout: [360, 480],
    ticker: [940, 80],
  };
  const [width, height] = sizes[win.size] || sizes.card;
  return `popup=yes,width=${width},height=${height}`;
}

function renderStage(windows) {
  const root = document.getElementById("stage");
  if (!root) return;
  root.innerHTML = windows
    .map((win) => {
      const layout = pickLayout(win);
      return `
        <article class="panel layout-${escapeHtml(layout)} place-${escapeHtml(win.placement || "stack")} size-${escapeHtml(win.size || "card")}" data-id="${escapeHtml(win.id || "")}" style="${driftStyle(win)}">
          <header>
            <span>EVIE</span>
            <span class="kind">${escapeHtml(String(win.kind || "card"))}</span>
            ${win.lookout ? '<span class="live"></span>' : ""}
            <button class="pop" type="button" data-pop="${escapeHtml(win.id || "")}">open alone</button>
          </header>
          ${composeInner(win)}
        </article>`;
    })
    .join("");
  root.querySelectorAll("[data-pop]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.getAttribute("data-pop");
      const win = windows.find((item) => item.id === id);
      if (!win) return;
      window.open(lookoutHref(win), `evie-${id}`, popFeatures(win));
    });
  });
}

function withRandomPair(win, index) {
  if ((win.questions && win.questions.length) || win.response) return win;
  if (Math.random() < 0.45) {
    return {
      ...win,
      questions: [pickRandom(ASK_POOL, win.id + index)],
      response: pickRandom(REPLY_POOL, win.id + "r" + index),
    };
  }
  return win;
}

function randomGalleryExtras() {
  const layouts = ["ask", "reply", "split", "ledger", "stack"];
  return [0, 1, 2].map((index) => {
    const question = pickRandom(ASK_POOL, "x" + index + Math.random());
    const response = pickRandom(REPLY_POOL, "y" + index + Math.random());
    return {
      id: "gal-random-" + index,
      kind: "card",
      size: index === 2 ? "brief" : "card",
      time_type: "linger",
      placement: "center",
      title: "Open thread",
      body: response,
      questions: [question],
      response,
      layout: layouts[index % layouts.length],
      why: "A random pairing — same folio language, different composition.",
    };
  });
}

function renderGallery(windows) {
  const root = document.getElementById("gallery");
  if (!root) return;
  const mixed = windows.map(withRandomPair).concat(randomGalleryExtras());
  root.innerHTML = mixed
    .map((win, index) => {
      const layout = pickLayout(win);
      const tilt = (((stableInt(win.id) >> 3) % 21) - 10) / 10;
      const wide = layout === "split" || layout === "ribbon" || win.size === "ticker" || win.size === "canvas";
      return `
        <article class="gallery-card ${wide ? "wide" : ""} layout-${escapeHtml(layout)}" style="--tilt:${tilt}deg">
          <div class="meta">
            <span>EVIE</span>
            <span>${escapeHtml(layout)}</span>
            <span>${escapeHtml(String(win.kind))}</span>
            ${win.lookout ? '<span class="live"></span>' : ""}
          </div>
          ${composeInner(win)}
          <p class="why">${escapeHtml(win.why || "")}</p>
          <button class="pop" type="button" data-pop="${escapeHtml(win.id)}" data-index="${index}">open alone</button>
        </article>`;
    })
    .join("");
  root.querySelectorAll("[data-pop]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.getAttribute("data-pop");
      const win = mixed.find((item) => item.id === id);
      if (!win) return;
      window.open(lookoutHref(win), `evie-${id}`, popFeatures(win));
    });
  });
}

function attachTranscript() {
  const list = document.getElementById("transcript");
  const offline = document.getElementById("offline");
  if (!list) return;
  if (window.location.protocol === "file:") {
    list.hidden = false;
    list.innerHTML = "<li>Serve /app/lookout over HTTP to stream the live transcript.</li>";
    return;
  }
  if (typeof window.EventSource !== "function") return;
  list.hidden = false;
  const source = new EventSource("/v1/runtime/transcript/stream");
  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data || "{}");
      const items = payload.events || [];
      items.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = (item.text || "").slice(0, 240);
        list.appendChild(li);
      });
      if (offline) offline.hidden = true;
    } catch (_err) {
      return;
    }
  };
  source.onerror = () => {
    if (offline) {
      offline.hidden = false;
      offline.textContent = "offline";
    }
  };
}

function boot() {
  const search = params();
  const plan = parsePlan();
  const gallery = document.getElementById("gallery");
  if (gallery) {
    renderGallery(GALLERY);
    return;
  }
  const stage = document.getElementById("stage");
  if (stage) {
    const windows = search.get("demo") === "1"
      ? DEMO_WINDOWS
      : (plan && Array.isArray(plan.windows) && plan.windows.length ? plan.windows : DEMO_WINDOWS);
    renderStage(windows);
    return;
  }
  if (search.get("demo") === "1") {
    renderSingle(DEMO_WINDOWS[3]);
    return;
  }
  renderSingle(windowFromQuery(search));
  attachTranscript();
  fetchDiagnosticsStrip();
}

async function fetchDiagnosticsStrip() {
  const el = document.getElementById("diagnostics-strip");
  if (!el) return;
  const base = (window.EV_API_URL || "").replace(/\/+$/, "");
  try {
    const headers = {};
    const key = window.EV_API_KEY || localStorage.getItem("ev.apiKey");
    if (key) headers.Authorization = "Bearer " + key;
    const resp = await fetch((base || "") + "/v1/diagnostics/last", { headers });
    if (!resp.ok) {
      el.textContent = "diagnostics unavailable";
      return;
    }
    const data = await resp.json();
    const checks = (data.report && data.report.checks) || [];
    el.innerHTML = checks
      .map((check) => `<li>${escapeHtml(check.name)}: ${escapeHtml(check.status)}</li>`)
      .join("") || `<li>stale${data.generated_at ? " · " + escapeHtml(String(data.generated_at)) : ""}</li>`;
  } catch (_err) {
    el.textContent = "diagnostics stale";
  }
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    window.close();
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
