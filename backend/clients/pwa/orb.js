/* EvieOrb alias — Presence Entity lives in presence.js. */
(function (root) {
  if (root.EviePresence && !root.EvieOrb) root.EvieOrb = root.EviePresence;
})(typeof window !== "undefined" ? window : globalThis);
