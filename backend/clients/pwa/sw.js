const BUILD = "2026.08.21.22";
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
