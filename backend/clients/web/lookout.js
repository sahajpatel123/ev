"use strict";

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
    lookout: true,
    source: "alert_radar",
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
    lookout: true,
    source: "calendar",
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
    lookout: false,
    source: "tactical",
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

function windowFromQuery(search) {
  const get = (name, fallback = "") => search.get(name) || fallback;
  const items = get("items")
    .split(/[|\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    id: get("id", "lookout-card"),
    kind: get("kind", "card"),
    size: get("size", "card"),
    time_type: get("time", "linger"),
    placement: get("place", "center"),
    title: get("title", "EVIE"),
    body: get("body", ""),
    items,
    recommendation: get("recommendation"),
    source: get("source"),
    lookout: get("lookout") === "1",
    ttl_ms: get("ttl") ? Number(get("ttl")) : null,
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderSingle(win) {
  document.title = `${win.title} — EVIE`;
  const kind = document.getElementById("kind");
  const time = document.getElementById("time");
  const source = document.getElementById("source");
  const title = document.getElementById("title");
  const body = document.getElementById("body");
  const rec = document.getElementById("rec");
  const items = document.getElementById("items");
  const live = document.getElementById("live");
  const ttl = document.getElementById("ttl");
  if (!kind || !title) return;
  kind.textContent = String(win.kind || "card").toUpperCase();
  time.textContent = String(win.time_type || "hold").toUpperCase();
  source.textContent = win.source || "";
  title.textContent = win.title || "EVIE";
  body.textContent = win.body || "";
  if (win.recommendation) {
    rec.hidden = false;
    rec.textContent = win.recommendation;
  }
  items.innerHTML = (win.items || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
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
    recommendation: win.recommendation || "",
    source: win.source || "",
  });
  if (win.ttl_ms) query.set("ttl", String(win.ttl_ms));
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
      const items = (win.items || [])
        .map((item) => `<li>${escapeHtml(item)}</li>`)
        .join("");
      const rec = win.recommendation
        ? `<p class="rec">${escapeHtml(win.recommendation)}</p>`
        : "";
      return `
        <article class="panel place-${escapeHtml(win.placement || "stack")} size-${escapeHtml(win.size || "card")}" data-id="${escapeHtml(win.id || "")}">
          <header>
            <span>EVIE</span>
            <span class="kind">${escapeHtml(String(win.kind || "card").toUpperCase())}</span>
            ${win.lookout ? '<span class="live"></span>' : ""}
            <button class="pop" type="button" data-pop="${escapeHtml(win.id || "")}">POP OUT</button>
          </header>
          <h2>${escapeHtml(win.title || "EVIE")}</h2>
          <p class="body">${escapeHtml(win.body || "")}</p>
          ${rec}
          <ul class="items">${items}</ul>
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

const GALLERY = [
  {
    id: "gal-chip",
    kind: "chip",
    size: "chip",
    time_type: "flash",
    placement: "upper_right",
    title: "Got it",
    body: "Saved to memory.",
    why: "JARVIS peripheral chip — 1.6s, then gone.",
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
    why: "Needed pulse — emergency, even if you did not ask.",
  },
  {
    id: "gal-card",
    kind: "card",
    size: "card",
    time_type: "linger",
    placement: "center",
    title: "Status",
    body: "One answer. Lingers about 30 seconds.",
    why: "Default slate when you say show me.",
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
    why: "Karen tactical brief.",
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
    lookout: true,
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
    lookout: true,
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
    why: "JARVIS plot — large, until dismissed.",
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
    why: "Five-second ticker.",
  },
];

function renderGallery(windows) {
  const root = document.getElementById("gallery");
  if (!root) return;
  root.innerHTML = windows
    .map((win) => {
      const items = (win.items || [])
        .map((item) => `<li>${escapeHtml(item)}</li>`)
        .join("");
      const rec = win.recommendation
        ? `<p class="rec">${escapeHtml(win.recommendation)}</p>`
        : "";
      return `
        <article class="gallery-card">
          <div class="meta">
            <span>EVIE</span>
            <span>${escapeHtml(String(win.kind).toUpperCase())}</span>
            <span>${escapeHtml(String(win.size).toUpperCase())}</span>
            <span>${escapeHtml(String(win.time_type).toUpperCase())}</span>
            ${win.lookout ? '<span class="live"></span>' : ""}
          </div>
          <h2>${escapeHtml(win.title)}</h2>
          <p class="body">${escapeHtml(win.body || "")}</p>
          ${rec}
          <ul class="items">${items}</ul>
          <p class="why">${escapeHtml(win.why || "")}</p>
          <button class="pop" type="button" data-pop="${escapeHtml(win.id)}">POP OUT</button>
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
