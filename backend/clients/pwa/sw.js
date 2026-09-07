const BUILD = "2026.09.08.24";
const CACHE = "evie-static-" + BUILD;
const STATIC = [
  "/evie/",
  "/evie/style.css",
  "/evie/manifest.webmanifest",
  "/evie/icon.svg",
  "/evie/apple-touch-icon.png",
];
const NETWORK_ONLY = [
  "/evie/app.js",
  "/evie/audio.js",
  "/evie/orb.js",
  "/evie/presence.js",
  "/evie/webrtc.js",
  "/evie/mobile-actions.js",
  "/evie/capabilities.js",
  "/evie/feedback.js",
  "/evie/pcm-worklet.js",
  "/evie/playback-worklet.js",
  "/evie/sw.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/v1/") || event.request.method !== "GET") {
    return;
  }
  if (!url.pathname.startsWith("/evie/")) {
    return;
  }
  const network = NETWORK_ONLY.some((path) => url.pathname === path);
  if (network) {
    event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        if (resp.ok && event.request.method === "GET") {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
        }
        return resp;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/evie/")))
  );
});

/* Cycle 49 — Web Push (VAPID): show inbox nudges as notifications when the
   PWA is backgrounded. Click focuses or opens /evie/. Handler only —
   subscription lives in app.js. */
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_err) { data = {}; }
  const title = String(data.title || "Evie");
  const body = String(data.body || "").slice(0, 300);
  const url = String(data.url || "/evie/");
  event.waitUntil(
    self.registration.showNotification(title, { body: body, data: { url: url }, tag: "evie-inbox" })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/evie/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url.includes("/evie")) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
/* Cycle 24 — iPhone-only service-worker version display helper. Backward
   compatible: additive self.EvieSwVersion (mirrored to window when present);
   existing install/activate/fetch listeners untouched. */
self.EvieSwVersion = {
  build: BUILD,
  label: function () {
    return "Evie " + BUILD;
  },
  matches: function (clientBuild) {
    return clientBuild === BUILD;
  }
};
if (typeof window !== "undefined") window.EvieSwVersion = self.EvieSwVersion;
/* Cycle 25 — iPhone-only device-role label. Backward compatible: additive
   self.EvieDeviceRole pure helper; owner-declared input only (never probed
   from hardware); existing listeners untouched. */
self.EvieDeviceRole = {
  label: function (declared) {
    var d = String(declared == null ? "" : declared).trim().toLowerCase();
    if (d === "se" || d.indexOf("se ") === 0 || d.indexOf(" se") !== -1) {
      return "iPhone SE (fallback)";
    }
    return "iPhone 16 Pro (preferred)";
  }
};
if (typeof window !== "undefined") window.EvieDeviceRole = self.EvieDeviceRole;
/* Cycle 41 — iPhone-only presence-heartbeat display model. Backward
   compatible: additive self.EviePresenceHeartbeat pure model mapping
   heartbeat age to tone+label; existing listeners untouched. */
self.EviePresenceHeartbeat = {
  model: function (lastSeenMs, nowMs) {
    var now = Number(nowMs);
    var seen = Number(lastSeenMs);
    if (!isFinite(now) || !isFinite(seen)) return { tone: "neutral", label: "Presence unknown", ageS: -1 };
    var ageS = Math.max(0, Math.floor((now - seen) / 1000));
    if (ageS < 30) return { tone: "live", label: "Here now", ageS: ageS };
    if (ageS < 120) return { tone: "stale", label: "Away " + ageS + "s", ageS: ageS };
    return { tone: "gone", label: "Away " + Math.floor(ageS / 60) + "m", ageS: ageS };
  }
};
if (typeof window !== "undefined") window.EviePresenceHeartbeat = self.EviePresenceHeartbeat;
