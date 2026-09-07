/* EvieOrb alias — Presence Entity lives in presence.js. */
/* Cycle 3 (iPhone-only): connection-quality dot model for 16 Pro / SE Safari PWA. */
(function (root) {
  if (root.EviePresence && !root.EvieOrb) root.EvieOrb = root.EviePresence;

  // Pure mapper: conn string -> {tone, label}. No DOM, no network, so the Mac
  // web workbench (if ever loaded) sees identical text for identical input.
  const CONN_DOT = {
    READY: { tone: "ok", label: "Live" },
    ACTIVE: { tone: "ok", label: "Live" },
    LISTENING: { tone: "ok", label: "Listening" },
    CONNECTING: { tone: "busy", label: "Connecting" },
    AUTHENTICATING: { tone: "busy", label: "Signing in" },
    RECONNECTING: { tone: "warn", label: "Reconnecting" },
    OFFLINE: { tone: "bad", label: "Offline — queued" },
    DISCONNECTED: { tone: "idle", label: "Idle" },
    UNPAIRED: { tone: "idle", label: "Pair this iPhone" },
  };

  function connDot(conn, talking) {
    const base = CONN_DOT[conn] || { tone: "idle", label: String(conn || "Idle") };
    if ((conn === "ACTIVE" || conn === "READY") && talking) {
      return { tone: "ok", label: "Listening" };
    }
    return { tone: base.tone, label: base.label };
  }

  root.EvieConnDot = { map: CONN_DOT, connDot: connDot };
})(typeof window !== "undefined" ? window : globalThis);
