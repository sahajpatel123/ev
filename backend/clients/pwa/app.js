const CLIENT_BUILD = "2026.09.09.04";
const DESIGN_VERSION = "atelier-1";
const PROTOCOL_VERSION = "1";
const TARGET_RATE = 16000;
const ASSET_V = "?v=" + CLIENT_BUILD;
const halfDuplex =
  /audio_debug=half_duplex/.test(location.search) ||
  localStorage.getItem("PWA_AUDIO_DEBUG_MODE") === "half_duplex";

const $ = (id) => document.getElementById(id);
let engine = null;
let AUDIO_ENGINE_VERSION = "3";
let transport = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("failed " + src));
    document.head.appendChild(s);
  });
}

async function ensureAudioModules() {
  if (!window.EvieAudio) await loadScript("/evie/audio.js" + ASSET_V);
  if (!window.EviePresence && !window.EvieOrb) await loadScript("/evie/presence.js" + ASSET_V);
  if (!window.EvieFeedback) await loadScript("/evie/feedback.js" + ASSET_V);
  if (!window.EvieWebRTC) await loadScript("/evie/webrtc.js" + ASSET_V);
}

function pcmEngine() {
  if (!engine) {
    engine = new window.EvieAudio.EvieAudioPlaybackEngine();
    engine.halfDuplex = halfDuplex;
    AUDIO_ENGINE_VERSION = window.EvieAudio.AUDIO_ENGINE_VERSION || "3";
  }
  if (state._ttfaStart) {
    engine._ttfaStart = state._ttfaStart;
  }
  return engine;
}

/* Cycle 83 — TTFA/latency: the dev overlay (triple-tap the mood line)
   shows time-to-first-audio, underruns, jitter cushion, backend. Dev-only
   surface; production users never see it. */
function markTtfaStart() {
  state._ttfaStart = performance.now();
}

function toggleLatencyOverlay() {
  let el = $("latency-overlay");
  if (el) {
    el.remove();
    return;
  }
  el = document.createElement("div");
  el.id = "latency-overlay";
  el.style.cssText = "position:fixed;left:8px;bottom:8px;z-index:9999;background:rgba(0,0,0,.75);color:#9fe870;padding:8px 10px;border-radius:8px;font:11px ui-monospace,monospace;max-width:280px;white-space:pre-wrap";
  document.body.appendChild(el);
  const render_ = () => {
    if (!document.getElementById("latency-overlay")) return;
    const m = (engine && engine.metrics) || {};
    const web = (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot && state.webrtc.diag.snapshot()) || {};
    el.textContent = [
      "TTFA last: " + (m.lastTtfaMs || "—") + " ms · best: " + (m.ttfaMs || "—") + " ms",
      "underruns: " + (m.underruns || 0) + " · jitter: " + (m.jitterTargetMs || "—") + " ms",
      "backend: " + (m.playbackBackend || state.activeBackend || "—"),
      "ctx: " + (m.contextState || "—") + " · out " + (m.outputLatency || "—") + "s",
      "SE profile: " + String(!!(window.EvieAudioProfile && window.EvieAudioProfile.se)),
    ].join("\n");
    setTimeout(render_, 1000);
  };
  render_();
}

const state = {
  ui: "BOOTING",
  conn: "DISCONNECTED",
  deviceToken: null,
  accessToken: null,
  device: null,
  hello: null,
  instanceId: sessionStorage.getItem("evie_instance") || crypto.randomUUID(),
  talking: false,
  _talkInflight: false,
  _recoverInflight: false,
  audioLeader: false,
  ws: null,
  webrtc: null,
  mediaBackend: "webrtc_strict",
  activeBackend: "none",
  encodedPlaying: false,
  encodedUrl: null,
  voiceHealth: null,
  talkPhase: "IDLE",
  connectionDiag: null,
  lastAsr: "",
  lastAsrConfidence: null,
  lastIndependentAsr: "",
  forensic: {},
  reconnectTimer: null,
  reconnects: 0,
  sessionGen: 0,
  sessionId: null,
  capture: "none",
  preflight: {},
  history: [],
  activity: [],
  inbox: [],
  status: null,
  queue: [],
  syncCursor: null,
  drainedCaptures: {},
  assetManifestHash: null,
  helloRecheckTimer: 0,
  userLine: "",
  caption: "",
  mood: "Connecting to Evie…",
  captureSettings: {},
  incidents: [],
  surface: "presence",
};

sessionStorage.setItem("evie_instance", state.instanceId);

const bus = "BroadcastChannel" in window ? new BroadcastChannel("evie-audio-leader") : null;
if (bus) {
  bus.onmessage = (ev) => {
    const data = ev.data || {};
    if (data.type === "claim" && data.instanceId && data.instanceId !== state.instanceId) {
      loseAudio("audio_owner_lost");
    }
  };
}

function claimAudioLeader() {
  state.audioLeader = true;
  if (bus) bus.postMessage({ type: "claim", instanceId: state.instanceId, at: Date.now() });
}

function loseAudio(reason) {
  state.audioLeader = false;
  if (engine) engine.stop();
  if (state.talking) stopTalk();
  if (reason === "audio_owner_lost" || reason === "conversation_moved") {
    textOf($("reply"), "Conversation moved to another device.");
    setMood("Ready");
  }
}

function setConn(next) {
  state.conn = next;
  if (next === "OFFLINE") state.ui = "OFFLINE";
  else if (next === "RECONNECTING") state.ui = "RECONNECTING";
  else if (next === "CONNECTING" || next === "AUTHENTICATING") state.ui = "CONNECTING";
  else if (next === "ACTIVE" && state.talking) state.ui = "LISTENING";
  else if (next === "ACTIVE") state.ui = "CONNECTING";
  else if (next === "READY") state.ui = "READY";
  else if (next === "DISCONNECTED" && !state.deviceToken) state.ui = "UNPAIRED";
  render();
}

function textOf(el, value) {
  if (el) el.textContent = value == null ? "" : String(value);
}

function showHomeStationResult(payload) {
  const body = payload && payload.phone_action ? payload.phone_action : (payload || {});
  const isHomeStation =
    body.route === "HOME_STATION" ||
    body.provenance === "home_station.dispatch" ||
    (payload && payload.home_station_result);
  if (!isHomeStation) return false;
  const tool = String(body.tool || body.operation || payload.name || "action").trim();
  let status = "accepted";
  if (body.confirmation_required) {
    status = "confirmation required";
  } else if (body.ok === false || body.tool_ok === false || body.error_code) {
    status = "failed";
  } else if (body.queued) {
    status = "queued";
  } else if (body.executed === true) {
    status = body.verified === true ? "completed · verified" : "completed · verification pending";
  }
  const line = "Home Station · " + status + " · " + tool;
  const card = $("action-card");
  if (card) {
    card.hidden = false;
    card.setAttribute("role", "status");
    card.setAttribute("aria-live", "polite");
    textOf(card, line);
  }
  if (state._lastHomeStationResult !== line) {
    state._lastHomeStationResult = line;
    pushActivity(line);
    if (window.EvieFeedback) {
      if (status.indexOf("confirmation") === 0) {
        window.EvieFeedback.haptic(10);
      } else if (status.indexOf("failed") === 0) {
        window.EvieFeedback.hapticEvent("bargeIn");
      } else if (status.indexOf("completed") === 0) {
        window.EvieFeedback.hapticEvent("turnDone");
      } else {
        window.EvieFeedback.haptic(10);
      }
    }
  }
  return true;
}

function digitalFollowPrompts(digital) {
  const payload = digital || {};
  const chips = [];
  const manner = String(payload.manner || "");
  const status = String(payload.status || "").toUpperCase();
  const focus = payload.focus || {};
  const person = String(focus.person || "").trim();
  const dest = String(payload.destination || "").trim();
  if (manner === "digest" || manner === "particular") {
    chips.push({ id: "more", label: "More about that", prompt: "more about that particular chat" });
    if (person) chips.push({ id: "who", label: "More about " + person, prompt: "more about " + person });
  }
  if (manner === "details") {
    chips.push({ id: "more", label: "A bit more", prompt: "tell me more" });
  }
  if (status === "PREPARED" || status === "WAITING_FOR_APPROVAL") {
    chips.push({ id: "approve", label: "Approve send", prompt: "approve send" });
    chips.push({ id: "hold", label: "Don't send", prompt: "do not send" });
  }
  if (status === "CLARIFY" && dest) {
    chips.push({ id: "to", label: "To " + dest, prompt: "reroute those chats to " + dest });
  }
  const seen = new Set();
  return chips.filter((chip) => {
    if (!chip.prompt || seen.has(chip.id)) return false;
    seen.add(chip.id);
    return true;
  });
}

function turnOutcomeLine(body) {
  const payload = body || {};
  const digital = payload.digital || {};
  if (payload.route === "DIGITAL_OPS" || digital.kind === "phone_brief") {
    const bits = ["This iPhone"];
    if (digital.manner === "digest") bits.push("summary");
    else if (digital.manner === "details") bits.push("more on that chat");
    else if (digital.manner === "particular") bits.push("that chat");
    else if (digital.manner === "reroute") bits.push("reroute");
    else if (digital.manner) bits.push(String(digital.manner));
    if (digital.sent === false) bits.push("not sent");
    if (digital.focus && digital.focus.person) bits.push(String(digital.focus.person));
    return bits.join(" · ");
  }
  const target = String(payload.route_target || "");
  const result = String(payload.action_result || "").toUpperCase();
  if (target === "HOME_STATION" || payload.queued || result === "QUEUED") {
    let status = "accepted";
    if (payload.queued || result === "QUEUED") status = "queued";
    if (result === "NEEDS_CONFIRMATION" || payload.confirmation_required) status = "needs confirmation";
    if (result === "DEVICE_OFFLINE") status = "Mac offline";
    if (result === "COMPLETED") status = "completed";
    if (result === "FAILED" || result === "BLOCKED") status = result.toLowerCase();
    return "Home Station · " + status;
  }
  return "";
}

function mergePeopleRows(body) {
  const rows = [];
  const seen = new Set();
  const add = (name, source, extra) => {
    const label = String(name || "").trim();
    if (!label) return;
    const key = label.toLowerCase();
    if (seen.has(key)) return;
    const extraRow = extra || {};
    rows.push({
      name: label,
      source: source || "home",
      callable: !!extraRow.callable,
      channels: extraRow.channels || [],
    });
    seen.add(key);
  };
  ((body && body.contacts) || []).forEach((row) => add(typeof row === "string" ? row : row && row.name, "phone", row));
  ((body && body.home) || []).forEach((row) => add(typeof row === "string" ? row : row && row.name, "home", row));
  return rows;
}

function trustBannerCopy(hello) {
  const status = (hello && hello.status) || hello || {};
  const raw = String(status.trust_state || (hello && hello.environment) || "");
  const trust = raw === "OWNER" ? "TRUSTED_OWNER_DEVICE" : raw === "SANDBOX" ? "PAIRED_SANDBOX" : raw;
  if (window.EvieTrust && typeof window.EvieTrust.banner === "function") {
    return window.EvieTrust.banner(trust);
  }
  if (trust === "TRUSTED_OWNER_DEVICE") return { tone: "good", label: "Trusted owner device", cta: "Manage devices" };
  if (trust === "REVOKED") return { tone: "bad", label: "Trust revoked", cta: "Re-pair this iPhone" };
  return { tone: "neutral", label: trust || "Paired · Sandbox", cta: "Review pairing" };
}

function missionLines(payload) {
  const mission = (payload && payload.mission) || payload || {};
  const lines = [];
  if (mission.now && mission.now.objective) lines.push("Now · " + mission.now.objective);
  (mission.working || []).forEach((row) => {
    if (row && row.objective) lines.push("Working · " + row.objective);
  });
  (mission.waiting || []).forEach((row) => {
    if (row && row.objective) lines.push("Waiting · " + row.objective);
  });
  (mission.needs_you || []).forEach((row) => {
    const title = row && (row.title || row.objective || row.what);
    if (title) lines.push("Needs you · " + title);
  });
  return lines;
}

function changedLines(payload) {
  const rows = (payload && payload.changes) || [];
  return rows.map((row) => {
    if (typeof row === "string") return row;
    return String((row && (row.summary || row.title || row.text || row.kind)) || "").trim();
  }).filter(Boolean);
}

function fillFollowChips(digital, extra) {
  const host = $("follow-chips");
  const prompts = digitalFollowPrompts(digital).concat(Array.isArray(extra) ? extra : []);
  if (!host) return prompts;
  while (host.firstChild) host.removeChild(host.firstChild);
  host.hidden = prompts.length === 0;
  prompts.forEach((chip) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = chip.label;
    btn.setAttribute("data-follow", chip.id);
    btn.addEventListener("click", () => {
      if (typeof sendText !== "function") return;
      sendText(chip.prompt).catch((err) => {
        state.caption = String(err.message || err);
        paintLive();
      });
    });
    host.appendChild(btn);
  });
  if (prompts.length) {
    const exchange = $("room-exchange");
    if (exchange) exchange.open = true;
  }
  return prompts;
}

function applyTurnOutcome(body) {
  const payload = body || {};
  fillFollowChips(payload.digital, payload.follow_prompts);
  if (showHomeStationResult(payload)) return payload;
  const line = turnOutcomeLine(payload);
  const card = $("action-card");
  if (line && card) {
    card.hidden = false;
    card.setAttribute("role", "status");
    card.setAttribute("aria-live", "polite");
    textOf(card, line);
    if (state._lastTurnOutcome !== line) {
      state._lastTurnOutcome = line;
      pushActivity(line);
    }
  }
  return payload;
}

function showCameraStatus(status) {
  const line = "Camera · " + String(status || "working").trim();
  const card = $("action-card");
  if (card) {
    card.hidden = false;
    card.setAttribute("role", "status");
    card.setAttribute("aria-live", "polite");
    textOf(card, line);
  }
  if (state._lastCameraStatus !== line) {
    state._lastCameraStatus = line;
    pushActivity(line);
  }
}

function setMood(label) {
  state.mood = label;
  textOf($("mood"), label);
  const presence = state.orb;
  if (presence) {
    const map = {
      Ready: "idle",
      Listening: "listening",
      Thinking: "thinking",
      Speaking: "speaking",
      "Working on MacBook": "tool",
      "Working on Home Station": "tool",
      Camera: "vision",
      "Connecting to Evie…": "connecting",
      Reconnecting: "connecting",
      "Home Station is offline.": "offline",
      "Tap to enable voice": "error",
      "Voice unavailable": "error",
      "Connecting voice…": "connecting",
      "Connecting microphone…": "connecting",
      "Voice connected — tap to enable audio": "error",
    };
    presence.setState(map[label] || (state.ui === "ERROR" ? "error" : "idle"));
  }
  if ($("room-exchange")) syncQuietRoom();
}

function applyAppearance(mode) {
  const next = mode === "light" || mode === "dark" ? mode : "system";
  if (next === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("evie-appearance", next);
  const row = $("appearance");
  if (row) {
    const buttons = row.querySelectorAll("button");
    for (let i = 0; i < buttons.length; i += 1) {
      const on = buttons[i].getAttribute("data-appearance") === next;
      buttons[i].className = on ? "on" : "";
    }
  }
  if (state.orb && state.orb.refreshTheme) state.orb.refreshTheme();
  syncRoomTheme();
}

function syncRoomTheme() {
  const color = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim();
  document.querySelectorAll('meta[name="theme-color"]').forEach(meta => { meta.content = color; });
}

function arrangeRoomTools() {
  const list = $("room-tool-list");
  if (!list || list.dataset.arranged) return;
  const groups = [
    ["Quick actions", ["look", "capture", "conversation"]],
    ["Your day", ["today", "weather", "health", "routines"]],
    ["Your memory", ["memory", "search", "looks"]],
    ["Your space", ["inbox", "people", "devices", "queue", "activity", "privacy"]],
  ];
  groups.forEach(([title, surfaces], index) => {
    const section = document.createElement("section");
    section.className = "room-tool-group";
    const heading = document.createElement("h3");
    heading.id = "room-tools-group-" + index;
    heading.textContent = title;
    section.setAttribute("aria-labelledby", heading.id);
    section.appendChild(heading);
    surfaces.forEach(surface => {
      const button = surface === "look" ? $("look-btn") : list.querySelector('[data-surface="' + surface + '"]');
      if (button) section.appendChild(button);
    });
    list.appendChild(section);
  });
  list.dataset.arranged = "true";
}

function prettyRole(role) {
  if (role === "primary_companion") return "Primary iPhone";
  if (role === "secondary_companion") return "Secondary iPhone";
  if (role === "home_station") return "Home Station";
  return role || "";
}

function abbrev(value) {
  const text = String(value || "");
  if (text.length <= 12) return text || "—";
  return text.slice(0, 8) + "…";
}

function homeLine(hello) {
  const station = hello.home_station || (hello.states || {}).home_station || "—";
  if (station === "ONLINE") return "MacBook · Online";
  if (station === "BACKEND_DOWN") return "Home Station is offline.";
  if (station === "TAILSCALE_DOWN") return "Home Station unreachable";
  return "MacBook · " + station;
}

function backendLabel() {
  if (state.activeBackend === "webrtc" || state.activeBackend === "webrtc_strict") return "WebRTC strict";
  if (state.activeBackend === "encoded") return "Encoded";
  if (state.activeBackend === "pcm_ws") return "PCM Stream";
  return String(state.hello && state.hello.recommended_backend || "webrtc_strict");
}

function paintLive() {
  const unpaired = !state.deviceToken;
  const welcome = $("welcome");
  const ready = $("ready-ui");
  if (welcome) welcome.hidden = !unpaired;
  if (ready) ready.hidden = unpaired;
  const online = state.conn === "READY" || state.conn === "ACTIVE";
  const offline = state.conn === "DISCONNECTED" || state.conn === "OFFLINE";
  textOf($("status"), offline && unpaired ? "Pair this device" : (online ? "Private" : state.conn));
  const talk = $("talk");
  if (talk) {
    const canStop = state.talking || state._talkInflight;
    talk.disabled = !canStop && (!online || state.ui === "OFFLINE" || state.ui === "CONNECTING");
    talk.textContent = "";
    talk.setAttribute("aria-label", canStop ? "Stop talking" : "Talk to Evie");
    talk.setAttribute("title", canStop ? "Stop talking" : "Talk to Evie");
    talk.dataset.connecting = String(!!state._talkInflight);
  }
  textOf($("user-line"), state.userLine);
  textOf($("reply"), state.caption);
  const quick = $("quick-row");
  if (quick) quick.hidden = true;
  syncQuietRoom();
  if (state.ui === "OFFLINE") setMood("Home Station is offline.");
}

// Visual disclosure only. No permission requests or model calls live here.
function syncQuietRoom() {
  const ready = $("ready-ui");
  if (!ready) return;
  ready.dataset.session = state.talking || state._talkInflight ? "active" : "idle";
  $("room-session").hidden = !(state.talking || state._talkInflight);
  $("room-enable-audio").hidden = !(state.talking && state.webrtc && state.webrtc.playBlocked);
  const exchange = $("room-exchange");
  const hasContent = !!(state.userLine || state.caption);
  const newTurn = state._roomSeenUserLine !== state.userLine;
  const newCaption = state._roomSeenCaption !== state.caption;
  const recovery = state.ui === "ERROR" || state.ui === "OFFLINE" || /unavailable|tap to enable|denied/i.test(state.mood || "");
  if (newTurn) state._roomSetAside = false;
  if (hasContent && (exchange.hidden || (!state._roomSetAside && (newTurn || newCaption)) || recovery)) exchange.open = true;
  exchange.hidden = !hasContent;
  state._roomSeenUserLine = state.userLine;
  state._roomSeenCaption = state.caption;
  ready.dataset.exchange = hasContent && exchange.open ? "open" : "closed";
  textOf(exchange.querySelector(".room-fold-hint"), exchange.open ? "Set aside" : "Read");
  const dialog = document.querySelector('.sheet[role="dialog"]:not([hidden])');
  if (state.orb && state.orb.setPaused) state.orb.setPaused(!!dialog);
  if (dialog) {
    let feedback = dialog.querySelector(".room-panel-feedback");
    if (!feedback) {
      feedback = document.createElement("p");
      feedback.className = "room-panel-feedback quiet";
      feedback.setAttribute("role", "status");
      dialog.querySelector("h2, h1")?.insertAdjacentElement("afterend", feedback);
    }
    feedback.textContent = state.caption || "";
    feedback.hidden = !state.caption;
  }
  const actionCard = $("mobile-action-card");
  if (actionCard) {
    const actionParent = dialog || ready;
    if (actionCard.parentElement !== actionParent) actionParent.appendChild(actionCard);
  }
  const bar = $("room-call-bar");
  const parent = dialog || document.querySelector(".stage");
  if (bar.parentElement !== parent) parent.appendChild(bar);
  bar.hidden = !((state.talking || state._talkInflight) && dialog);
  const enable = $("room-enable-audio");
  const enableParent = dialog ? bar : $("room-session").parentElement;
  if (enable.parentElement !== enableParent) enableParent.appendChild(enable);
  document.body.dataset.callOverlay = String(!bar.hidden);
  if (!bar.hidden) document.body.style.setProperty("--room-call-height", bar.offsetHeight + "px");
}

function render() {
  paintLive();
  const hello = state.hello || {};
  const device = hello.device || state.device || {};
  textOf($("device-role"), prettyRole(device.role) || "—");
  textOf($("home-station"), homeLine(hello));
  textOf($("environment"), hello.environment || "SANDBOX");
  const status = hello.status || hello.session_context || state.status || {};
  state.status = status;
  const trust = status.trust_state || (hello.session_context && hello.session_context.trust_state) || hello.environment || "";
  const next = status.next_action || (hello.session_context && hello.session_context.next_action) || "";
  textOf($("trust-line"), [trust, prettyRole(device.role), next && next !== "ready" ? ("next: " + next) : ""].filter(Boolean).join(" · "));
  textOf($("sandbox-banner"), "Personal memory off");
  const st = hello.states || {};
  textOf($("states"), [
    st.tailnet ? "TAILNET " + String(st.tailnet).toUpperCase() : "",
    st.evie_core ? "EVIE CORE " + String(st.evie_core).toUpperCase() : "",
    st.realtime ? "REALTIME " + String(st.realtime).toUpperCase() : "",
  ].filter(Boolean).join(" · "));
  if (!state.talking) {
    fillSettings(hello, device);
    fillPrivacy(hello, device);
    fillMobileActions(hello);
    fillDevices(hello, device);
    fillInbox();
    fillWelcomeStatus();
  }
  refreshInstallHint();
  showUpdateLine();
  paintCameraRole();
  textOf($("audio-badge"), "Audio · " + backendLabel());
  const health = state.voiceHealth && state.voiceHealth.health;
  textOf($("voice-health"), health
    ? ("VOICE HEALTH: " + (health.ready ? "READY" : "NOT READY")
      + " · MIC " + health.mic
      + " · UPLINK " + health.uplink
      + " · ASR " + health.asr
      + " · REALTIME " + health.realtime
      + " · DOWNLINK " + health.downlink
      + " · FALLBACK " + health.fallback)
    : "VOICE HEALTH: idle");
  textOf($("voice-status-line"), "MOBILE VOICE: CONNECTION CONVERGENCE");
  renderConnectionStages();
  const diagPanel = $("diag-panel");
  if (diagPanel && diagPanel.open) {
    textOf($("diag"), JSON.stringify({
    conn: state.conn,
    ui: state.ui,
    client_build: CLIENT_BUILD,
    mobile_runtime_version: (window.EvieMobileVoice && window.EvieMobileVoice.RUNTIME_VERSION) || "",
    signaling: (state.connectionDiag && state.connectionDiag.signaling) || hello.signaling || "unified_calls",
    signaling_version: hello.signaling_version,
    attempt_id: state.connectionDiag && state.connectionDiag.attempt_id,
    failed_stage: state.connectionDiag && state.connectionDiag.failed_stage,
    connection: state.connectionDiag,
    design_version: DESIGN_VERSION,
    audio_engine_version: AUDIO_ENGINE_VERSION,
    media_backend: state.activeBackend,
    recommended_backend: hello.recommended_backend,
    playback_backend: state.activeBackend === "webrtc" || state.activeBackend === "webrtc_strict" ? "webrtc" : (engine ? engine.backend : "uninitialized"),
    pcm_fallback: "off",
    voice_health: state.voiceHealth,
    talk_phase: state.talkPhase,
    last_asr_label: "TRANSCRIPT",
    last_asr: state.lastAsr,
    last_asr_confidence: state.lastAsrConfidence,
    last_independent_asr: state.lastIndependentAsr,
    forensic: state.forensic,
    protocol_version: PROTOCOL_VERSION,
    server_build: hello.pwa_build || hello.server_build,
    server_release: hello.server_release || hello.pwa_build || "",
    backend_sha_fingerprint: abbrev(hello.backend_sha),
    asset_manifest_hash: hello.asset_manifest_hash || "",
    device_id: abbrev(device.device_id),
    role: device.role,
    memory_scope: hello.memory_scope || "sandbox",
    production_memory_enabled: false,
    https_secure_context: window.isSecureContext,
    service_worker: navigator.serviceWorker && navigator.serviceWorker.controller ? "active" : "none",
    gateway: state.conn,
    voice: state.talking ? "active" : "idle",
    camera: $("camera-sheet").hidden ? "idle" : "active",
    home_station: hello.home_station,
    realtime: (hello.states || {}).realtime,
    tool_schema_generation: hello.tool_schema_generation,
    capture: state.capture,
    capture_settings: state.captureSettings,
    preflight: state.preflight,
    instance_id: abbrev(state.instanceId),
    socket_generation: state.sessionGen,
    audio_generation: engine ? engine.generation : 0,
    audio_context_id: engine ? engine.ctxId : 0,
    audio_leader: state.audioLeader,
    half_duplex: halfDuplex,
    rtc: state.webrtc ? state.webrtc.metrics : {},
    playback: engine ? engine.metrics : {},
    incidents: state.incidents.slice(-8),
  }, null, 2));
  }
}

let healthRenderTimer = 0;
function scheduleHealthRender() {
  if (healthRenderTimer) return;
  healthRenderTimer = window.setTimeout(() => {
    healthRenderTimer = 0;
    render();
  }, 250);
}

function renderConnectionStages() {
  const ol = $("voice-stages");
  const failEl = $("voice-fail-stage");
  const mv = window.EvieMobileVoice;
  const diag = state.connectionDiag || (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot());
  if (failEl) {
    if (diag && diag.failed_stage) {
      failEl.textContent = "FAILED AT: " + diag.failed_stage + " " + (diag.failed_name || "");
    } else if (state.talking && state.webrtc && state.webrtc.runtime === "VOICE_READY") {
      failEl.textContent = "VOICE READY";
    } else {
      failEl.textContent = "FAILED AT: —";
    }
  }
  if (!ol || !mv) return;
  while (ol.firstChild) ol.removeChild(ol.firstChild);
  const stages = mv.STAGES || [];
  for (let i = 0; i < stages.length; i += 1) {
    const id = stages[i][0];
    const name = stages[i][1];
    const row = (diag && diag.stages && diag.stages[id]) || { status: "pending" };
    const li = document.createElement("li");
    const mark = row.status === "pass" ? "✓" : (row.status === "fail" ? "✗" : "·");
    li.textContent = name.replace(/_/g, " ") + "  " + mark;
    ol.appendChild(li);
  }
}

async function copyVoiceDiagnostic() {
  const mv = window.EvieMobileVoice;
  const diag = state.connectionDiag || (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot()) || {};
  const hello = state.hello || {};
  const text = mv && mv.formatConnectionDiag
    ? mv.formatConnectionDiag(diag, {
      build: CLIENT_BUILD,
      sw_build: hello.pwa_build || "",
      audio_mode: state.activeBackend || "webrtc_strict",
      signaling: diag.signaling || hello.signaling || "unified_calls",
    })
    : JSON.stringify(diag, null, 2);
  if (!navigator.clipboard?.writeText) throw new Error("Clipboard is unavailable in this browser.");
  await navigator.clipboard.writeText(text);
  state.caption = "Voice diagnostic copied.";
  render();
}

async function copyPhoneDiagnostic() {
  const hello = state.hello || {};
  const status = state.status || hello.status || {};
  const diag = state.connectionDiag || (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot()) || {};
  const payload = {
    schema: "evie.phone-diagnostic.v1",
    captured_at: new Date().toISOString(),
    client_build: CLIENT_BUILD,
    server_release: hello.server_release || hello.pwa_build || "",
    backend_sha_fingerprint: abbrev(hello.backend_sha),
    asset_manifest_hash: hello.asset_manifest_hash || "",
    protocol: PROTOCOL_VERSION,
    device_id: abbrev((state.device || {}).device_id),
    role: (state.device || {}).role || "",
    trust_state: status.trust_state || "",
    auth_revision: status.auth_revision || (state.device || {}).auth_revision || null,
    connection: {
      state: state.conn,
      ui: state.ui,
      backend: state.activeBackend,
      talk_phase: state.talkPhase,
      session_active: !!state.sessionId,
      instance_id: abbrev(state.instanceId),
      lease_id: abbrev(state.leaseId),
      reconnects: state.reconnects,
    },
    voice: {
      health: state.voiceHealth && state.voiceHealth.health,
      signaling: diag.signaling || hello.signaling || "",
      failed_stage: diag.failed_stage || "",
    },
    camera_role: state.cameraRole,
    queue: {
      local_items: Array.isArray(state.queue) ? state.queue.length : 0,
      pending_items: Array.isArray(state.queue)
        ? state.queue.filter((item) => item && item.state === "pending").length
        : 0,
    },
    action: {
      home_station_status: state._lastHomeStationResult || "",
      camera_status: state._lastCameraStatus || "",
    },
  };
  const text = JSON.stringify(payload, null, 2);
  if (!navigator.clipboard?.writeText) throw new Error("Clipboard is unavailable in this browser.");
  await navigator.clipboard.writeText(text);
  state.caption = "Phone diagnostic copied.";
  render();
}

function detailOf(body, fallback) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (d && typeof d.message === "string") return d.message;
  return fallback;
}

function fillDl(id, rows) {
  const dl = $(id);
  if (!dl) return;
  while (dl.firstChild) dl.removeChild(dl.firstChild);
  rows.forEach((row) => {
    const dt = document.createElement("dt");
    dt.textContent = row[0];
    const dd = document.createElement("dd");
    dd.textContent = row[1];
    dl.appendChild(dt);
    dl.appendChild(dd);
  });
}

function fillMobileActions(hello) {
  const ma = (hello && hello.mobile_actions) || {};
  const caps = ma.capabilities || [];
  const native = !!(window.EvieNativeShell && window.EvieNativeShell.post) || !!ma.native_shell_connected;
  const rows = [
    ["Native shell", native ? "Connected" : "Not this page"],
    ["This device", ma.this_device || "This iPhone"],
    ["Broker", ma.broker_version || "—"],
    ["Actions", ma.native_actions_enabled === true ? "Enabled" : ma.native_actions_enabled === false ? "Disabled" : "Not verified"],
  ];
  const legacy = document.getElementById("legacy-bridge-panel");
  if (legacy) legacy.hidden = !/legacy_bridge=1/.test(location.search);
  caps.forEach((cap) => {
    rows.push([cap.title || cap.operation, cap.available ? "Ready" : (cap.reason || "Unavailable")]);
  });
  if (ma.last_action && ma.last_action.action_id) {
    rows.push(["Last action", (ma.last_action.operation || "") + " · " + (ma.last_action.state || "")]);
  }
  fillDl("mobile-actions-meta", rows);
}

async function refreshStatus() {
  if (!state.deviceToken) return;
  try {
    const [status, caps, phoneCaps] = await Promise.all([
      api("/v1/device-gateway/status"),
      api("/v1/device-gateway/capabilities").catch(() => null),
      api("/v1/device-gateway/phone-capabilities").catch(() => null),
    ]);
    if (status && status.ok === false) return;
    state.status = status || {};
    state.phoneCapabilities = phoneCaps || null;
    fillSettings(Object.assign({}, state.hello || {}, { status: status || {} }), state.device || {});
    if (caps && caps.capabilities) {
      const rows = Object.keys(caps.capabilities).map((name) => {
        const c = caps.capabilities[name];
        const label = name.replace(/_/g, " ");
        return [label, c.available ? "available" : (c.reason ? c.reason.replace(/_/g, " ") : "unavailable")];
      });
      const home = (state.hello && state.hello.home_station_capabilities) || {};
      if (home.executor) {
        rows.push(["Home Station executor", home.executor + " · " + (home.availability || "server validated")]);
        (home.safe_actions || []).forEach((action) => {
          rows.push(["Home Station · " + action, "server validated"]);
        });
        if (Array.isArray(home.blocked) && home.blocked.length) {
          rows.push(["Home Station blocked", home.blocked.join(", ")]);
        }
      }
      rows.push(["HealthKit", "never sent to a model"]);
      if (phoneCaps && Array.isArray(phoneCaps.safe_actions)) {
        rows.push(["Phone catalog", phoneCaps.trust_state || "server"]);
      }
      fillDl("capability-meta", rows);
    } else {
      fillDl("capability-meta", [
        ["Capabilities", phoneCaps ? "Home Station catalog available" : "capability snapshot unavailable"],
        ["HealthKit", "never sent to a model"],
      ]);
    }
    paintLive();
  } catch (_err) {}
}

function fillSettings(hello, device) {
  const status = hello.status || hello.session_context || state.status || {};
  const rows = [
    ["Device", device.display_name || prettyRole(device.role) || "—"],
    ["Role", prettyRole(device.role) || "—"],
    ["Trust", status.trust_state || hello.environment || "—"],
    ["Owner scope", status.owner_scope || status.scope || "—"],
    ["Auth revision", String(status.auth_revision || device.auth_revision || "—")],
    ["Next action", status.next_action || "—"],
    ["Backend", status.backend_build || hello.backend_sha || "—"],
    ["Backend fingerprint", abbrev(hello.backend_sha)],
    ["Server release", hello.server_release || hello.pwa_build || "—"],
    ["Asset manifest", abbrev(hello.asset_manifest_hash)],
    ["Trust", status.trust_state || hello.environment || "—"],
    ["Battery", typeof status.battery_percent === "number" ? Math.round(status.battery_percent) + "%" + (status.battery_percent <= 15 ? " · low — alarms only" : "") : "not reported"],
    ["Product", status.product || "Tailscale PWA"],
    ["Connection", state.conn],
    ["Home Station", homeLine(hello)],
    ["PWA build", CLIENT_BUILD + (state.updateAvailable ? " · update available" : "")],
    ["Runtime", (window.EvieMobileVoice && window.EvieMobileVoice.RUNTIME_VERSION) || "—"],
    ["Mind", cognitiveLine(hello)],
    ["Signaling", hello.signaling_version || "unified-calls-v1"],
    ["Design", DESIGN_VERSION],
    ["Protocol", PROTOCOL_VERSION],
    ["HealthKit", healthkitLine(status)],
    ["Notifications", notificationLine(status)],
    ["Sync cursor", state.syncCursor ? "yes" : "none"],
    ["Install", isStandalonePwa() ? "Home Screen" : "Safari tab"],
  ];
  const battery = Number(status.battery_percent);
  if (Number.isFinite(battery)) {
    rows.splice(rows.findIndex((r) => r[0] === "Home Station"), 0, ["Battery", battery.toFixed(0) + "%"]);
  }
  fillDl("settings-meta", rows);
  const banner = $("trust-banner");
  if (banner) {
    const copy = trustBannerCopy(hello);
    banner.textContent = copy.label + (copy.cta ? " · " + copy.cta : "");
    banner.className = "quiet evie-trust-" + (copy.tone || "neutral");
  }
}

function cognitiveLine(hello) {
  const cog = (hello && hello.cognitive) || {};
  if (cog.muse_kernel) {
    return (cog.brain || "muse-spark-1.3-contributor") + " · speech " + (cog.speech || "gpt-realtime-2.1-mini");
  }
  return "Realtime Mini (legacy mind)";
}

function healthkitLine(status) {
  const hk = (status && status.healthkit) || {};
  const freshness = hk.freshness || "unavailable";
  return freshness + " · never sent to a model";
}

function notificationLine(status) {
  const note = (status && status.notifications) || {};
  const delivery = note.push_delivery || "poll";
  const inbox = note.inbox_channel || "in_app_poll";
  if (delivery === "web_notification") return "local alerts on this iPhone · inbox " + inbox;
  if (delivery === "apns") return "apns · inbox " + inbox;
  return "poll · inbox " + inbox + " · APNs not registered";
}

const EviePhoneAlerts = {
  key: "evie_local_alerts",
  seenKey: "evie_inbox_seen",
  timers: {},
  load: function () {
    try {
      return JSON.parse(localStorage.getItem(this.key) || "[]");
    } catch (_err) {
      return [];
    }
  },
  save: function (rows) {
    try { localStorage.setItem(this.key, JSON.stringify(rows.slice(-12))); } catch (_err) {}
  },
  canNotify: function () {
    return typeof Notification !== "undefined" && Notification.permission === "granted";
  },
  show: function (title, body) {
    if (!this.canNotify()) return false;
    try {
      new Notification(title || "Evie", { body: body || "", tag: "evie-local" });
      return true;
    } catch (_err) {
      return false;
    }
  },
  arm: function (row) {
    const item = row || {};
    const fireAt = Number(item.fireAt || 0);
    const id = String(item.id || ("t" + fireAt));
    if (!fireAt || fireAt <= Date.now()) {
      this.show(item.title || "Evie", item.body || "Time's up.");
      return;
    }
    const pending = this.load().filter((row) => row && row.id !== id && Number(row.fireAt) > Date.now());
    pending.push({ id: id, fireAt: fireAt, title: item.title || "Evie", body: item.body || "Time's up." });
    this.save(pending);
    if (this.timers[id]) clearTimeout(this.timers[id]);
    const delay = Math.min(Math.max(0, fireAt - Date.now()), 2147483647);
    this.timers[id] = setTimeout(() => {
      this.show(item.title || "Evie", item.body || "Time's up.");
      this.save(this.load().filter((row) => row && row.id !== id));
      delete this.timers[id];
    }, delay);
  },
  restore: function () {
    this.load().forEach((row) => {
      if (row && Number(row.fireAt) > Date.now()) this.arm(row);
    });
  },
  noticeInbox: function (items) {
    const primedKey = this.seenKey + "_ok";
    let seen = [];
    try { seen = JSON.parse(sessionStorage.getItem(this.seenKey) || "[]"); } catch (_err) {}
    const known = new Set(seen);
    const primed = sessionStorage.getItem(primedKey) === "1";
    (items || []).forEach((item) => {
      if (!item || !item.id) return;
      if (!primed) {
        known.add(item.id);
        return;
      }
      if (item.unread && !known.has(item.id)) {
        this.show(item.title || "Evie", item.body || "");
        known.add(item.id);
      }
    });
    try {
      sessionStorage.setItem(this.seenKey, JSON.stringify(Array.from(known).slice(-80)));
      sessionStorage.setItem(primedKey, "1");
    } catch (_err) {}
  },
};
window.EviePhoneAlerts = EviePhoneAlerts;

async function registerPhoneAlerts() {
  let delivery = "poll";
  let authorization = "undetermined";
  if (typeof Notification !== "undefined") {
    authorization = Notification.permission || "undetermined";
    if (Notification.permission === "granted") delivery = "web_notification";
  }
  try {
    await api("/v1/device-gateway/push/register", {
      method: "POST",
      body: JSON.stringify({
        token: "",
        delivery: delivery,
        bundle_id: "com.ev.evie.shell",
        authorization: authorization,
      }),
    });
  } catch (_err) {}
  EviePhoneAlerts.restore();
}

async function enableLocalAlerts() {
  const meta = $("alerts-meta");
  if (typeof Notification === "undefined") {
    textOf(meta, "This Safari cannot show notifications. Inbox still polls.");
    return;
  }
  try {
    const permission = await Notification.requestPermission();
    textOf(
      meta,
      permission === "granted"
        ? "Local alerts on. Inbox still polls Home Station. Not APNs."
        : "Alerts stay off until Safari allows notifications. Inbox still polls."
    );
  } catch (_err) {
    textOf(meta, "Could not ask for notifications. Inbox still polls.");
  }
  await registerPhoneAlerts();
}

function fillPrivacy(hello, device) {
  fillDl("privacy-meta", [
    ["Private connection", "On"],
    ["Public Funnel", "Off"],
    ["Personal memory", "Off"],
    ["Microphone", state.capture === "none" ? "Ask on Talk" : "Allowed"],
    ["Camera", $("camera-sheet").hidden ? "Ask on Look" : "Allowed"],
    ["This phone", prettyRole(device.role) || "Companion"],
    ["HealthKit", "Unavailable in this build · never sent to a model"],
    ["Notifications", "Local Evie alerts when allowed · otherwise in-app poll. Not APNs."],
  ]);
}

function fillDevices(hello, device) {
  const root = $("constellation");
  if (!root) return;
  while (root.firstChild) root.removeChild(root.firstChild);
  const nodes = [
    ["Evie", "Home Station core", homeLine(hello)],
    ["This phone", prettyRole(device.role) || "Companion", state.talking ? "Active" : "Ready"],
    ["MacBook", "Control + camera", hello.home_station === "ONLINE" ? "Online" : "Waiting"],
  ];
  ((hello && hello.companions) || []).forEach((row) => {
    if (!row) return;
    const seen = row.last_seen_at ? "last seen " + String(row.last_seen_at).replace("T", " ").slice(0, 16) : "not seen yet";
    nodes.push([
      row.display_name || "Other iPhone",
      prettyRole(row.role) || "Companion",
      (row.presence_state || "OFFLINE") + " · " + seen,
    ]);
  });
  nodes.forEach((row) => {
    const el = document.createElement("div");
    el.className = "node";
    const title = document.createElement("strong");
    title.textContent = row[0];
    const sub = document.createElement("span");
    sub.textContent = row[1] + " · " + row[2];
    el.appendChild(title);
    el.appendChild(sub);
    root.appendChild(el);
  });
}

function fillWelcomeStatus() {
  const status = state.status || (state.hello && state.hello.status) || {};
  fillDl("welcome-status", status.trust_state ? [
    ["Trust", status.trust_state],
    ["Next", status.next_action || "—"],
    ["Product", "Tailscale PWA"],
  ] : []);
}

function fillInbox() {
  const list = $("inbox-list");
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  const items = state.inbox || [];
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "evie-today-empty";
    li.textContent = "Inbox is clear.";
    list.appendChild(li);
  }
  items.forEach((item) => {
    const li = document.createElement("li");
    if (item.unread) li.className = "evie-inbox-unread";
    const via = item.delivery || item.push_delivery || "in_app_poll";
    const label = document.createElement("span");
    label.textContent = (item.title || item.kind || "notice") + " — " + (item.body || "") + " · " + via;
    li.appendChild(label);
    if (item.unread) {
      const ack = document.createElement("button");
      ack.type = "button";
      ack.className = "evie-queue-drop";
      ack.textContent = "✕";
      ack.setAttribute("aria-label", "Dismiss");
      ack.addEventListener("click", async () => {
        if (ack.disabled) return;
        ack.disabled = true;
        try {
          const result = await api("/v1/device-gateway/inbox/ack", {
            method: "POST",
            body: JSON.stringify({ item_id: item.id }),
          });
          if (!result || result.ok !== true) throw new Error("Dismiss was not confirmed.");
          await refreshInbox();
        } catch (err) {
          let message = li.querySelector('[role="status"]');
          if (!message) {
            message = document.createElement("span");
            message.setAttribute("role", "status");
            li.appendChild(message);
          }
          textOf(message, "Could not dismiss. Try again. " + String(err.message || err));
        } finally { ack.disabled = false; }
      });
      li.appendChild(ack);
    }
    list.appendChild(li);
  });
}

async function markAllInboxRead() {
  const btn = $("inbox-ack-all-btn");
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  try {
    const result = await api("/v1/device-gateway/inbox/ack-all", { method: "POST", body: "{}" });
    if (!result || result.ok !== true) throw new Error("Update was not confirmed.");
    await refreshInbox();
  } catch (err) {
    let meta = $("inbox-secondary-status");
    if (!meta) {
      meta = document.createElement("p");
      meta.id = "inbox-secondary-status";
      meta.setAttribute("role", "status");
      $("inbox-list")?.parentNode.appendChild(meta);
    }
    textOf(meta, "Could not mark all read. Try again. " + String(err.message || err));
  } finally { if (btn) btn.disabled = false; }
}

async function refreshInbox() {
  if (!state.deviceToken) return;
  const token = state.deviceToken;
  try {
    const body = await api("/v1/device-gateway/inbox");
    if (token !== state.deviceToken) return;
    if (!body || body.ok === false || !Array.isArray(body.items)) throw new Error("Invalid inbox response.");
    state.inbox = body.items || [];
    fillInbox();
    EviePhoneAlerts.noticeInbox(state.inbox);
    const unread = (state.inbox || []).filter((item) => item.unread).length;
    const btn = $("inbox-ack-all-btn");
    if (btn) btn.hidden = unread === 0;
    textOf($("inbox-secondary-status"), "");
  } catch (err) {
    if (token !== state.deviceToken) return;
    let meta = $("inbox-secondary-status");
    if (!meta) {
      meta = document.createElement("p");
      meta.id = "inbox-secondary-status";
      meta.setAttribute("role", "status");
      $("inbox-list")?.parentNode.appendChild(meta);
    }
    textOf(meta, "Inbox could not refresh; showing the previous list. " + String(err.message || err));
  }
}

function fillOl(id, items, limit, emptyLabel) {
  const list = $(id);
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  const rows = (items || []).slice(0, limit || 12);
  if (!rows.length) {
    const li = document.createElement("li");
    li.className = "evie-today-empty";
    li.textContent = emptyLabel || "Nothing here yet.";
    list.appendChild(li);
    return;
  }
  rows.forEach((entry) => {
    const li = document.createElement("li");
    li.textContent = String(entry);
    list.appendChild(li);
  });
}

function todayTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function metricLabel(key) {
  const names = { steps: "Steps", sleep_hours: "Sleep", heart_rate: "Heart rate", active_energy: "Active energy", resting_hr: "Resting heart rate", vo2: "VO₂", weight: "Weight" };
  return names[key] || key.replace(/_/g, " ");
}

async function refreshToday() {
  if (!state.deviceToken) return;
  const hud = $("today-hud");
  const meta = $("today-meta");
  try {
    const body = await api("/v1/device-gateway/today");
    const card = body.hud || null;
    if (card && hud) {
      hud.hidden = false;
      textOf($("today-hud-title"), card.title || "Today");
      textOf($("today-hud-body"), card.body || "");
    } else if (hud) {
      hud.hidden = true;
    }
    const health = body.health || {};
    const rows = [["Status", health.freshness || "unavailable"]];
    const metrics = health.metrics || {};
    Object.keys(metrics).slice(0, 5).forEach((key) => {
      const value = metrics[key];
      rows.push([metricLabel(key), typeof value === "number" ? String(value) : String(value || "—")]);
    });
    fillDl("today-health", rows);
    const calendar = (body.calendar || {}).events || [];
    fillOl("today-calendar", calendar.map((ev) => (todayTime(ev.start) ? todayTime(ev.start) + " · " : "") + (ev.title || "Event")), 6, "Nothing scheduled.");
    const reminders = (body.reminders || []).map((row) => row.text || "Reminder");
    fillOl("today-reminders", reminders, 8, "No pending reminders.");
    const memories = (body.memories || []).map((row) => (row.memory_type ? row.memory_type + " — " : "") + row.text);
    fillOl("today-memories", memories, 6, "No memories yet.");
    const scope = body.memory_enabled ? body.memory_scope || "owner" : "sandbox";
    const unread = body.inbox_pending || 0;
    let metaText = "Memory: " + scope + (unread ? " · " + unread + " unread in Inbox" : "");
    const qh = body.quiet_hours || {};
    if (qh.window && qh.window.start) {
      const end = qh.window.end || "—";
      metaText += qh.active ? " · Quiet hours until " + end : " · Digest quiet " + qh.window.start + "–" + end;
    }
    textOf(meta, metaText);
  } catch (err) {
    textOf(meta, "Today is unavailable: " + String(err.message || err));
  }
}

async function refreshMemories(query) {
  if (!state.deviceToken) return;
  const list = $("memory-list");
  const detail = $("memory-detail");
  const meta = $("memory-meta");
  const request = {};
  const token = state.deviceToken;
  if (detail) {
    detail.hidden = true;
    detail._evieMemoryRequest = request;
  }
  textOf($("memory-detail-text"), "");
  textOf($("memory-detail-meta"), "");
  textOf($("memory-versions"), "");
  textOf($("memory-sources"), "");
  $("memory-back-btn")?.remove();
  if (list) {
    list.hidden = false;
    while (list.firstChild) list.removeChild(list.firstChild);
  }
  try {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    const body = await api("/v1/device-gateway/memories" + (params.toString() ? "?" + params.toString() : ""));
    if (token !== state.deviceToken || (detail && detail._evieMemoryRequest !== request)) return;
    if (body.memory_enabled === false) {
      textOf(meta, "Personal memory is off — pair and promote this phone from the Mac.");
      const form = $("memory-search-form");
      if (form) form.hidden = true;
      return;
    }
    const form = $("memory-search-form");
    if (form) form.hidden = false;
    const rows = body.memories || [];
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "evie-today-empty";
      li.textContent = query ? "No memories match that search." : "No memories yet.";
      if (list) list.appendChild(li);
    }
    rows.forEach((row) => {
      const li = document.createElement("li");
      li.className = "evie-memory-row";
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      const strong = document.createElement("strong");
      strong.textContent = row.memory_type || "memory";
      const span = document.createElement("span");
      span.textContent = String(row.text || "").slice(0, 160);
      li.appendChild(strong);
      li.appendChild(span);
      li.addEventListener("click", () => openMemoryDetail(row.id));
      li.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          if (!event.repeat) openMemoryDetail(row.id);
        }
      });
      if (list) list.appendChild(li);
    });
    textOf(meta, body.total + " memories" + (query ? " · “" + query + "”" : ""));
  } catch (err) {
    if (token !== state.deviceToken || (detail && detail._evieMemoryRequest !== request)) return;
    textOf(meta, "Memory unavailable: " + String(err.message || err));
  }
}

async function openMemoryDetail(memoryId) {
  const detail = $("memory-detail");
  const list = $("memory-list");
  if (!detail || !memoryId) return;
  const request = {};
  const token = state.deviceToken;
  const opener = document.activeElement;
  detail._evieMemoryRequest = request;
  detail.hidden = true;
  textOf($("memory-detail-text"), "");
  textOf($("memory-detail-meta"), "");
  textOf($("memory-sources"), "");
  textOf($("memory-versions"), "");
  $("memory-back-btn")?.remove();
  if (list) list.hidden = false;
  textOf($("memory-meta"), "Loading memory…");
  try {
    const body = await api("/v1/device-gateway/memories/" + encodeURIComponent(memoryId));
    if (token !== state.deviceToken || detail._evieMemoryRequest !== request) return;
    const mem = body.memory;
    if (!mem) throw new Error("This memory is unavailable.");
    textOf($("memory-meta"), "");
    detail.hidden = false;
    if (list) list.hidden = true;
    textOf($("memory-detail-text"), mem.text || "");
    const confidence = typeof mem.confidence === "number" ? Math.round(mem.confidence * 100) + "%" : "—";
    textOf($("memory-detail-meta"), (mem.memory_type || "memory") + " · confidence " + confidence + " · " + (mem.source_type || "inferred"));
    const sources = $("memory-sources");
    if (sources) {
      while (sources.firstChild) sources.removeChild(sources.firstChild);
      const items = body.sources || [];
      if (!items.length) {
        const li = document.createElement("li");
        li.className = "evie-today-empty";
        li.textContent = "No source events recorded.";
        sources.appendChild(li);
      }
      items.forEach((src) => {
        const li = document.createElement("li");
        li.textContent = (src.kind || "event") + " — " + (src.text || "");
        sources.appendChild(li);
      });
    }
    const back = document.createElement("button");
    back.type = "button";
    back.className = "secondary";
    back.textContent = "Back to list";
    back.addEventListener("click", () => {
      detail._evieMemoryRequest = null;
      detail.hidden = true;
      if (list) list.hidden = false;
      if (opener && opener.isConnected) opener.focus();
    });
    const existing = $("memory-back-btn");
    if (existing) existing.remove();
    back.id = "memory-back-btn";
    detail.appendChild(back);
    back.focus();
    try {
      const prov = await api("/v1/device-gateway/memories/" + encodeURIComponent(memoryId) + "/provenance");
      if (token !== state.deviceToken || detail._evieMemoryRequest !== request) return;
      const versions = $("memory-versions");
      if (versions) {
        while (versions.firstChild) versions.removeChild(versions.firstChild);
        const rows = prov.versions || [];
        if (!rows.length) {
          const li = document.createElement("li");
          li.className = "evie-today-empty";
          li.textContent = "No version history.";
          versions.appendChild(li);
        }
        rows.forEach((v) => {
          const li = document.createElement("li");
          const suffix = v.is_current ? " · current" : "";
          const reason = v.reason_for_change ? " — " + v.reason_for_change : "";
          li.textContent = "v" + v.version + suffix + ": " + (v.text || "").slice(0, 140) + reason;
          versions.appendChild(li);
        });
      }
    } catch (_err) {
      if (token !== state.deviceToken || detail._evieMemoryRequest !== request) return;
      fillOl("memory-versions", [], 1, "Version history unavailable. Reopen to retry.");
    }
  } catch (err) {
    if (token !== state.deviceToken || detail._evieMemoryRequest !== request) return;
    textOf($("memory-meta"), "Memory unavailable: " + String(err.message || err));
  }
}

async function runSearch(query) {
  const meta = $("search-meta");
  const q = (query || "").trim();
  if (!q) {
    textOf(meta, "Type a query to search everything.");
    return;
  }
  try {
    const body = await api("/v1/device-gateway/search?q=" + encodeURIComponent(q));
    fillOl("search-memories", (body.memories || []).map((m) => (m.memory_type ? m.memory_type + " — " : "") + m.text), 8, body.memory_enabled === false ? "Memory is off (pair + promote on the Mac)." : "No memory matches.");
    fillOl("search-events", (body.events || []).map((e) => (e.kind || "event") + " — " + e.text), 8, "No events match.");
    fillOl("search-reminders", (body.reminders || []).map((r) => r.text || "Reminder"), 6, "No reminders match.");
    fillOl("search-contacts", (body.contacts || []).map((c) => c.name || ""), 6, "No contacts match.");
    const total = (body.memories || []).length + (body.events || []).length + (body.reminders || []).length + (body.contacts || []).length;
    textOf(meta, total + " result" + (total === 1 ? "" : "s") + " for “" + q + "”");
  } catch (err) {
    textOf(meta, "Search unavailable: " + String(err.message || err));
  }
}

function openSearch() {
  openSurface("search");
  const input = $("search-q");
  if (input) input.focus();
}

async function submitCapture() {
  cleanupEvieVoiceNote();
  if (submitCapture._pending) return;
  const input = $("capture-text");
  const meta = $("capture-meta");
  const text = input ? input.value.trim() : "";
  if (!text) {
    textOf(meta, "Write something first.");
    return;
  }
  const privacy = $("capture-privacy");
  const chosen = privacy && privacy.querySelector("button.on") ? privacy.querySelector("button.on").getAttribute("data-privacy") : "normal";
  const fingerprint = JSON.stringify([state.deviceToken, text, chosen]);
  if (!submitCapture._draft || submitCapture._draft.fingerprint !== fingerprint) {
    submitCapture._draft = { fingerprint, key: "note-" + crypto.randomUUID() };
  }
  const draft = submitCapture._draft;
  const key = draft.key;
  submitCapture._pending = draft;
  try {
    const body = await api("/v1/device-gateway/capture", {
      method: "POST",
      body: JSON.stringify({ text: text, privacy_level: chosen, idempotency_key: key }),
    });
    if (submitCapture._pending !== draft) return;
    if (body && body.ok) {
      if (input && input.value.trim() === text) input.value = "";
      submitCapture._draft = null;
      textOf(meta, body.duplicate ? "Already saved (duplicate)." : "Saved to memory.");
    } else {
      textOf(meta, "Could not save right now.");
    }
  } catch (err) {
    if (submitCapture._pending !== draft) return;
    const code = err && (err.error_code || (err.body && err.body.error_code));
    if (code === "capture_requires_owner") {
      textOf(meta, "This phone is still sandboxed — approve it from the Mac first.");
    } else if (err && err.status === 422) {
      textOf(meta, "Note is empty.");
    } else {
      textOf(meta, "Offline or unreachable — try again when Home Station is back.");
    }
  } finally {
    if (submitCapture._pending === draft) submitCapture._pending = null;
  }
}

// Capture-only lifecycle; MAIN may call cleanupEvieVoiceNote() when closing Capture.
const evieVoiceNoteLifecycle = { active: null, draft: null, saving: false, epoch: 0 };

function evieVoiceNoteIsNormal() {
  const selected = $("capture-privacy")?.querySelector("button.on");
  return !selected || selected.getAttribute("data-privacy") === "normal";
}

function releaseEvieVoiceNoteResources(session) {
  if (!session) return;
  window.clearTimeout(session.timer);
  session.timer = null;
  if (session.stream) session.stream.getTracks().forEach(track => {
    try { track.stop(); } catch (_err) {}
  });
}

function finishEvieVoiceNote(session) {
  releaseEvieVoiceNoteResources(session);
  if (session.finished) return;
  session.finished = true;
  if (evieVoiceNoteLifecycle.active !== session) return;
  evieVoiceNoteLifecycle.active = null;
  const mime = session.recorder?.mimeType || "audio/mp4";
  const blob = new Blob(session.chunks, { type: mime });
  if (blob.size) {
    evieVoiceNoteLifecycle.draft = { blob, mime, capturedAt: session.capturedAt, key: session.key };
  }
  const btn = $("voice-note-btn");
  if (btn) {
    btn.disabled = false;
    btn.textContent = blob.size ? "Retry save voice note" : "Voice note";
  }
  textOf($("voice-note-state"), blob.size ? "Recording kept on this page. Tap to save; closing the page loses it." : "No audio recorded. Try again.");
  if (!evieVoiceNoteIsNormal()) textOf($("voice-note-state"), "Private and sensitive voice notes are unavailable. Use a text note. Any recorded audio remains on this page, unsent.");
  if (session.save && blob.size) saveEvieVoiceNoteDraft();
}

function stopEvieVoiceNoteSession(session, save) {
  session.save = save;
  session.phase = "stopping";
  window.clearTimeout(session.timer);
  session.timer = null;
  try {
    if (session.recorder && session.recorder.state !== "inactive") {
      session.stopRequested = true;
      session.recorder.stop();
    } else if (!session.stopRequested) finishEvieVoiceNote(session);
  } catch (_err) {
    session.save = false;
    finishEvieVoiceNote(session);
  } finally { releaseEvieVoiceNoteResources(session); }
}

function cleanupEvieVoiceNote(options = {}) {
  const session = evieVoiceNoteLifecycle.active;
  if (options.discard) {
    evieVoiceNoteLifecycle.epoch += 1;
    evieVoiceNoteLifecycle.draft = null;
    evieVoiceNoteLifecycle.saving = false;
    evieVoiceNoteLifecycle.active = null;
  }
  if (session) {
    if (!session.recorder) evieVoiceNoteLifecycle.active = null; // Invalidates late microphone permission.
    stopEvieVoiceNoteSession(session, false);
  }
  const btn = $("voice-note-btn");
  if (btn && !evieVoiceNoteLifecycle.saving) {
    btn.disabled = false;
    btn.textContent = evieVoiceNoteLifecycle.draft ? "Retry save voice note" : "Voice note";
  }
  if (options.discard) textOf($("voice-note-state"), "Voice note cleared from this page.");
}

async function saveEvieVoiceNoteDraft() {
  const lifecycle = evieVoiceNoteLifecycle;
  const draft = lifecycle.draft;
  if (!draft || lifecycle.saving) return;
  const btn = $("voice-note-btn");
  const meta = $("voice-note-state");
  if (!evieVoiceNoteIsNormal()) {
    textOf(meta, "Private and sensitive voice notes are unavailable. Use a text note; audio can only be saved as Normal.");
    return;
  }
  const epoch = lifecycle.epoch;
  lifecycle.saving = true;
  if (btn) btn.disabled = true;
  textOf(meta, "Saving voice note as Normal…");
  try {
    const audioB64 = await blobToBase64(draft.blob);
    if (epoch !== lifecycle.epoch) return;
    if (!evieVoiceNoteIsNormal()) throw new Error("Private and sensitive audio cannot be saved. Use a text note.");
    const body = await api("/v1/device-gateway/capture/audio", {
      method: "POST",
      body: JSON.stringify({ audio_b64: audioB64, content_type: draft.mime, captured_at: draft.capturedAt, idempotency_key: draft.key }),
    });
    if (epoch !== lifecycle.epoch) return;
    if (!body || body.ok !== true) throw new Error("Home Station did not confirm the save.");
    lifecycle.draft = null;
    textOf(meta, "Voice note saved as Normal.");
  } catch (err) {
    if (epoch !== lifecycle.epoch) return;
    const code = err && (err.error_code || (err.body && err.body.error_code));
    textOf(meta, "Save failed. Recording kept on this page for retry. " + (code === "capture_requires_owner" ? "Approve this phone from the Mac first." : String(err.message || err)));
  } finally {
    if (epoch === lifecycle.epoch) {
      lifecycle.saving = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = lifecycle.draft ? "Retry save voice note" : "Voice note";
      }
    }
  }
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || "").split(",")[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function toggleVoiceNote() {
  const btn = $("voice-note-btn");
  const stateEl = $("voice-note-state");
  const lifecycle = evieVoiceNoteLifecycle;
  if (lifecycle.saving) return;
  if (!evieVoiceNoteIsNormal()) {
    cleanupEvieVoiceNote();
    textOf(stateEl, "Private and sensitive voice recording is unavailable. Use a text note; audio can only be saved as Normal.");
    return;
  }
  if (lifecycle.active) {
    if (lifecycle.active.phase !== "recording") return;
    if (btn) btn.disabled = true;
    stopEvieVoiceNoteSession(lifecycle.active, true);
    return;
  }
  if (lifecycle.draft) return saveEvieVoiceNoteDraft();
  if (state.talking || state._talkInflight) {
    textOf(stateEl, "Stop Talk before recording a voice note.");
    return;
  }
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) || typeof MediaRecorder === "undefined") {
    textOf(stateEl, "This browser cannot record audio.");
    return;
  }
  const session = { phase: "acquiring", chunks: [], stream: null, recorder: null, timer: null, save: false, capturedAt: new Date().toISOString(), key: "voicenote-" + crypto.randomUUID() };
  lifecycle.active = session;
  if (btn) btn.disabled = true;
  textOf(stateEl, "Waiting for microphone permission…");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    session.stream = stream;
    if (lifecycle.active !== session) { releaseEvieVoiceNoteResources(session); return; }
    if (!evieVoiceNoteIsNormal() || state.talking || state._talkInflight) {
      throw new Error("Voice recording needs Normal privacy and Talk stopped. Text notes remain available.");
    }
    const mime = MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
    const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    session.recorder = recorder;
    recorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size) session.chunks.push(ev.data);
    };
    recorder.onstop = () => finishEvieVoiceNote(session);
    recorder.onerror = () => {
      if (lifecycle.active !== session) { releaseEvieVoiceNoteResources(session); return; }
      stopEvieVoiceNoteSession(session, false);
      textOf(stateEl, "Recording failed — microphone error.");
    };
    recorder.start(250);
    session.phase = "recording";
    if (btn) { btn.disabled = false; btn.textContent = "Stop and save"; }
    textOf(stateEl, "Recording as Normal… tap again to stop and save.");
    session.timer = window.setTimeout(() => {
      if (lifecycle.active === session && session.phase === "recording") toggleVoiceNote();
    }, 90000);
  } catch (err) {
    releaseEvieVoiceNoteResources(session);
    if (lifecycle.active !== session) return;
    cleanupEvieVoiceNote();
    textOf(stateEl, "Could not record. " + String(err.message || err));
  }
}

async function refreshQueue() {
  if (!state.deviceToken) return;
  const token = state.deviceToken;
  const list = $("queue-list");
  const meta = $("queue-meta");
  try {
    const body = await api("/v1/device-gateway/queue");
    if (token !== state.deviceToken) return;
    if (!body || body.ok === false || !Array.isArray(body.items)) throw new Error("Invalid queue response.");
    const items = body.items || [];
    if (list) {
      while (list.firstChild) list.removeChild(list.firstChild);
    }
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "evie-today-empty";
      li.textContent = "Nothing queued.";
      if (list) list.appendChild(li);
    }
    items.forEach((item) => {
      const li = document.createElement("li");
      const text = item.kind === "siri_capture" && item.payload && item.payload.text ? String(item.payload.text) : item.kind || "request";
      li.textContent = text.slice(0, 120) + " · " + (item.state || "pending") + (item.error_code ? " · " + item.error_code : "");
      if (item.state === "pending") {
        const drop = document.createElement("button");
        drop.type = "button";
        drop.className = "evie-queue-drop";
        drop.textContent = "Drop";
        drop.addEventListener("click", async () => {
          if (drop.disabled) return;
          drop.disabled = true;
          try {
            const result = await api("/v1/device-gateway/queue/" + encodeURIComponent(item.id), { method: "DELETE" });
            if (!result || result.ok !== true) throw new Error("Drop was not confirmed.");
            await refreshQueue();
          } catch (err) {
            if (token === state.deviceToken) textOf(meta, "Could not drop this item. Try again. " + String(err.message || err));
          } finally { drop.disabled = false; }
        });
        li.appendChild(drop);
      }
      if (list) list.appendChild(li);
    });
    textOf(meta, items.length + " item" + (items.length === 1 ? "" : "s") + " on this phone");
  } catch (err) {
    if (token !== state.deviceToken) return;
    textOf(meta, "Queue unavailable: " + String(err.message || err));
  }
}

let routineTimes = [];

function renderRoutineTimes() {
  const box = $("routines-times");
  if (!box) return;
  while (box.firstChild) box.removeChild(box.firstChild);
  routineTimes.forEach((t) => {
    const chip = document.createElement("span");
    chip.className = "evie-routine-chip";
    chip.textContent = t;
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "✕";
    x.setAttribute("aria-label", "Remove " + t);
    x.addEventListener("click", () => {
      routineTimes = routineTimes.filter((v) => v !== t);
      renderRoutineTimes();
    });
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

async function loadRoutines() {
  if (!state.deviceToken) return;
  const meta = $("routines-meta");
  try {
    const body = await api("/v1/device-gateway/routines");
    const cfg = body.routines || {};
    routineTimes = (cfg.digest_times || []).slice(0, 4);
    renderRoutineTimes();
    const seg = $("routines-enabled");
    if (seg) {
      seg.querySelectorAll("button").forEach((btn) => {
        const on = btn.getAttribute("data-rv") === "on";
        btn.classList.toggle("on", on === !!cfg.enabled);
      });
    }
    const qs = $("routines-q-start");
    const qe = $("routines-q-end");
    if (qs) qs.value = cfg.quiet_hours_start || "";
    if (qe) qe.value = cfg.quiet_hours_end || "";
    textOf(meta, cfg.enabled ? "Digest is on — " + (cfg.digest_times || []).join(", ") : "Digest is off.");
  } catch (err) {
    textOf(meta, "Routines unavailable: " + String(err.message || err));
  }
}

async function saveRoutines() {
  const meta = $("routines-meta");
  const seg = $("routines-enabled");
  const enabled = !!(seg && seg.querySelector("button.on") && seg.querySelector("button.on").getAttribute("data-rv") === "on");
  const timezone = (Intl.DateTimeFormat && Intl.DateTimeFormat().resolvedOptions && Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";
  const qs = $("routines-q-start");
  const qe = $("routines-q-end");
  try {
    const body = await api("/v1/device-gateway/routines", {
      method: "PUT",
      body: JSON.stringify({
        enabled: enabled,
        digest_times: routineTimes,
        quiet_hours_start: qs && qs.value ? qs.value : null,
        quiet_hours_end: qe && qe.value ? qe.value : null,
        timezone: timezone,
      }),
    });
    if (body && body.ok) {
      textOf(meta, "Saved. Timezone: " + timezone + ".");
    } else {
      textOf(meta, "Could not save routines.");
    }
  } catch (err) {
    if (err && err.status === 422) textOf(meta, "Enable requires at least one digest time.");
    else textOf(meta, "Save failed: " + String(err.message || err));
  }
}

let peopleCache = [];

async function refreshPeople(filterText) {
  const list = $("people-list");
  const meta = $("people-meta");
  const needle = (filterText || "").trim().toLowerCase();
  try {
    const body = await api("/v1/device-gateway/contacts");
    peopleCache = mergePeopleRows(body);
    if (list) {
      while (list.firstChild) list.removeChild(list.firstChild);
    }
    const shown = needle
      ? peopleCache.filter((row) => row.name.toLowerCase().indexOf(needle) !== -1)
      : peopleCache;
    if (!shown.length) {
      const li = document.createElement("li");
      li.className = "evie-today-empty";
      li.textContent = needle
        ? "No matching contact."
        : "No people yet — Evie uses WhatsApp, iMessage, mail, and Home Station names. Safari cannot read the iPhone address book.";
      if (list) list.appendChild(li);
      textOf(meta, "");
      return;
    }
    shown.forEach((row) => {
      const name = row.name;
      const li = document.createElement("li");
      li.className = "evie-people-row";
      const span = document.createElement("span");
      span.textContent = name;
      li.appendChild(span);
      const actions = document.createElement("span");
      actions.className = "evie-people-actions";
      const actionsFor = [
        { verb: "Last", prompt: "What did I last have with " + name },
        { verb: "Message", prompt: "latest messages with " + name },
      ];
      if (row.callable) actionsFor.push({ verb: "Call", prompt: "Call " + name });
      actionsFor.forEach((action) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "evie-queue-drop";
        btn.textContent = action.verb;
        btn.addEventListener("click", () => {
          openSurface("home");
          sendText(action.prompt).catch(() => {});
        });
        actions.appendChild(btn);
      });
      li.appendChild(actions);
      if (list) list.appendChild(li);
    });
    const phoneN = peopleCache.filter((row) => row.source === "phone").length;
    const homeN = peopleCache.filter((row) => row.source === "home").length;
    const source = phoneN && homeN
      ? "this phone and Home Station"
      : phoneN
        ? "this phone"
        : "Home Station";
    textOf(meta, shown.length + " of " + peopleCache.length + " people · " + source);
  } catch (err) {
    textOf(meta, "People unavailable: " + String(err.message || err));
  }
}

async function refreshMission() {
  const meta = $("mission-meta");
  try {
    const body = await api("/v1/device-gateway/presence/mission");
    const changed = await api("/v1/device-gateway/presence/what-changed");
    const lines = missionLines(body);
    fillOl("mission-list", lines, 12, "Nothing in flight right now.");
    fillOl("mission-changed", changedLines(changed), 8, "No recent changes.");
    const pocket = (body.mission && body.mission.pocket) || {};
    textOf(meta, (pocket.working || 0) + " working · " + (pocket.needs_you || 0) + " need you");
  } catch (err) {
    fillOl("mission-list", [], 12, "In flight is unavailable.");
    textOf(meta, "In flight unavailable: " + String(err.message || err));
  }
}

async function refreshLooks() {
  const list = $("looks-list");
  const meta = $("looks-meta");
  if (list) {
    while (list.firstChild) list.removeChild(list.firstChild);
  }
  try {
    const body = await api("/v1/device-gateway/looks");
    const looks = body.looks || [];
    if (body.memory_enabled === false) {
      textOf(meta, "Look history needs Mac approval first.");
      return;
    }
    if (!looks.length) {
      const li = document.createElement("li");
      li.className = "evie-today-empty";
      li.textContent = "No looks yet — tap Look and share what the camera sees.";
      if (list) list.appendChild(li);
    }
    looks.forEach((look) => {
      const li = document.createElement("li");
      const when = todayTime(look.occurred_at);
      const text = look.summary || (look.scene ? "Saw " + look.scene : "A look");
      li.textContent = (when ? when + " · " : "") + text;
      if (look.has_media && look.media_url) {
        const row = document.createElement("div");
        row.className = "look-media";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        const isClip = String(look.media_kind || "") === "clip" || String(look.media_kind || "") === "video";
        button.textContent = isClip
          ? "Play clip" + (look.duration_s ? " · " + Math.round(look.duration_s) + "s" : "")
          : "View image";
        button.addEventListener("click", () => toggleLookMedia(li, button, look));
        row.appendChild(button);
        if (look.moment_count) {
          const note = document.createElement("span");
          note.className = "quiet";
          note.textContent = " · " + look.moment_count + " moments";
          row.appendChild(note);
        }
        li.appendChild(row);
      }
      if (look.transcript) {
        const transcript = document.createElement("p");
        transcript.className = "quiet";
        transcript.textContent = "Speech: " + look.transcript;
        li.appendChild(transcript);
      }
      if (list) list.appendChild(li);
    });
    textOf(meta, looks.length + " look" + (looks.length === 1 ? "" : "s") + " recorded");
  } catch (err) {
    textOf(meta, "Look history unavailable: " + String(err.message || err));
  }
}

async function toggleLookMedia(li, button, look) {
  const existing = li.querySelector("video, img");
  if (existing) {
    if (existing.src && existing.src.indexOf("blob:") === 0) URL.revokeObjectURL(existing.src);
    existing.remove();
    button.textContent = String(look.media_kind || "") === "clip" ? "Play clip" : "View image";
    return;
  }
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "Loading…";
  try {
    const response = await fetch(look.media_url, {
      headers: state.deviceToken ? { Authorization: "Bearer " + state.deviceToken } : {},
    });
    if (!response.ok) {
      throw new Error(response.status === 410 ? "The stored media was deleted by retention." : "Media unavailable.");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const isClip = String(look.media_kind || "") === "clip" || String(look.media_kind || "") === "video";
    const node = document.createElement(isClip ? "video" : "img");
    node.src = url;
    node.controls = isClip;
    node.playsInline = true;
    node.className = "look-media-view";
    li.appendChild(node);
    button.textContent = "Close";
  } catch (err) {
    button.textContent = label;
    textOf($("looks-meta"), String(err.message || err));
  } finally {
    button.disabled = false;
  }
}

function conversationExportText() {
  const speaker = (state.device && state.device.display_name) || "Me";
  const lines = (state.history || []).slice(-24).map((entry) => {
    const who = entry.role === "assistant" || entry.role === "evie" ? "Evie" : speaker;
    return who + ": " + String(entry.text || "");
  });
  return lines.join("\n");
}

async function copyConversation() {
  const meta = $("conv-export-meta");
  const text = conversationExportText();
  if (!text) {
    textOf(meta, "Nothing to copy yet.");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    textOf(meta, "Copied the last " + Math.min(24, state.history.length) + " messages available on this phone.");
  } catch (_err) {
    textOf(meta, "Copy is blocked in this browser.");
  }
}

async function shareConversation() {
  const meta = $("conv-export-meta");
  const text = conversationExportText();
  if (!text) {
    textOf(meta, "Nothing to share yet.");
    return;
  }
  try {
    if (!navigator.share || (navigator.canShare && !navigator.canShare({ text: text }))) {
      textOf(meta, "Sharing is unavailable here. Use Copy instead.");
      return;
    }
    await navigator.share({ title: "Conversation with Evie · last 24 messages on this phone", text: text });
    textOf(meta, "Shared the messages available on this phone (up to 24).");
  } catch (err) {
    textOf(meta, err && err.name === "AbortError" ? "Sharing cancelled." : "Sharing failed. Try again or use Copy. " + String(err.message || err));
  }
}

function healthChip(label, value) {
  const chip = document.createElement("div");
  chip.className = "evie-health-chip";
  const strong = document.createElement("strong");
  strong.textContent = String(value == null ? "—" : value);
  const span = document.createElement("span");
  span.textContent = label;
  chip.appendChild(strong);
  chip.appendChild(span);
  return chip;
}

async function refreshHealth() {
  const chips = $("health-chips");
  const series = $("health-series");
  const meta = $("health-meta");
  if (chips) {
    while (chips.firstChild) chips.removeChild(chips.firstChild);
  }
  if (series) {
    while (series.firstChild) series.removeChild(series.firstChild);
  }
  try {
    const body = await api("/v1/device-gateway/vitals");
    const snap = body.phone_snapshot || {};
    const rows = body.series || [];
    if (!chips) return;
    if (snap.available && snap.metrics && Object.keys(snap.metrics).length) {
      const m = snap.metrics || {};
      chips.appendChild(healthChip("Steps", m.steps != null ? String(m.steps) : "—"));
      chips.appendChild(healthChip("Sleep (h)", m.sleep_hours != null ? String(m.sleep_hours) : "—"));
      chips.appendChild(healthChip("Freshness", snap.freshness || "—"));
    } else if (rows.length && rows[0].metrics && Object.keys(rows[0].metrics).length) {
      const m = rows[0].metrics || {};
      chips.appendChild(healthChip("Steps", m.steps != null ? String(m.steps) : "—"));
      chips.appendChild(healthChip("Sleep (h)", m.sleep_hours != null ? String(m.sleep_hours) : "—"));
      const note = document.createElement("p");
      note.className = "quiet";
      note.textContent = "Home Station vitals — not from this iPhone's HealthKit. Never sent to a model.";
      chips.appendChild(note);
    } else {
      const chip = healthChip("Health", "off");
      chips.appendChild(chip);
      const note = document.createElement("p");
      note.className = "quiet";
      note.textContent = "No Health numbers on Home Station yet. Safari cannot read HealthKit. Nothing from Health is sent to a model.";
      chips.appendChild(note);
    }
    if (!rows.length && series) {
      const li = document.createElement("li");
      li.className = "evie-today-empty";
      li.textContent = "No vitals series yet.";
      series.appendChild(li);
    }
    rows.slice(0, 14).forEach((row) => {
      const li = document.createElement("li");
      const when = row.occurred_at ? new Date(row.occurred_at).toLocaleDateString([], { month: "short", day: "numeric" }) : "";
      const readiness = typeof row.readiness === "number" ? " · readiness " + Math.round(row.readiness) : "";
      const band = row.band ? " · " + row.band : "";
      li.textContent = (when ? when + " · " : "") + (row.source || "vitals") + readiness + band;
      if (series) series.appendChild(li);
    });
    textOf(meta, "Series entries: " + rows.length + " · phone snapshot " + (snap.freshness || "unavailable"));
  } catch (err) {
    textOf(meta, "Health unavailable: " + String(err.message || err));
  }
}

async function refreshWeather(placeText) {
  const card = $("weather-card");
  const meta = $("weather-meta");
  const place = (placeText || "").trim();
  try {
    const params = place ? "?place=" + encodeURIComponent(place) : "";
    const body = await api("/v1/device-gateway/weather" + params);
    if (body.status === "ok" && body.forecast) {
      if (card) card.hidden = false;
      textOf($("weather-title"), body.forecast.title || "Weather");
      textOf($("weather-body"), body.forecast.snippet || "");
      textOf(meta, "Live from Home Station.");
    } else if (body.error_code === "NO_PLACE") {
      if (card) card.hidden = true;
      textOf(meta, "Tell Evie a place — no location is guessed.");
    } else {
      if (card) card.hidden = true;
      textOf(meta, body.error_code === "WEATHER_TIMEOUT" ? "Weather lookup timed out." : "Weather is unavailable right now.");
    }
  } catch (err) {
    if (card) card.hidden = true;
    textOf(meta, "Weather unavailable: " + String(err.message || err));
  }
}

async function enqueueOffline(kind, payload, key) {
  const idem = (key && String(key).length >= 8) ? String(key) : crypto.randomUUID();
  const item = { idempotency_key: idem, kind: kind, payload: payload, state: "pending", executed: false };
  try {
    const body = await api("/v1/device-gateway/queue", {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idem, kind: kind, payload: payload }),
    });
    item.state = (body.item && body.item.state) || (body.status === 201 ? "pending" : "accepted");
    item.executed = !!body.executed;
  } catch (err) {
    if (err && err.status === 409) {
      item.state = "duplicate";
      item.executed = !!(err.body && err.body.executed);
    } else {
      item.state = err && err.status === 422 ? "rejected" : "queued_local";
    }
  }
  state.queue.push(item);
  saveOfflineQueueKeys();
  return item;
}

// Offline queue keys survive reload via localStorage (keys only, never payloads).
const OFFLINE_QUEUE_KEYS = "ev.offlineQueueKeys";
function loadOfflineQueueKeys() {
  try {
    const raw = localStorage.getItem(OFFLINE_QUEUE_KEYS);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((k) => typeof k === "string" && k.length >= 8);
  } catch (_err) {
    return [];
  }
}
function saveOfflineQueueKeys() {
  try {
    const keys = state.queue.map((item) => item && item.idempotency_key).filter((k) => typeof k === "string" && k.length >= 8);
    localStorage.setItem(OFFLINE_QUEUE_KEYS, JSON.stringify(keys));
  } catch (_err) {}
}
function restoreOfflineQueue() {
  const keys = loadOfflineQueueKeys();
  const seen = new Set(state.queue.map((item) => item && item.idempotency_key));
  keys.forEach((key) => {
    if (seen.has(key)) return;
    seen.add(key);
    state.queue.push({ idempotency_key: key, kind: "siri_capture", payload: {}, state: "queued_local", executed: false });
  });
}
function nativePost(payload) {
  if (!(window.EvieNativeShell && window.EvieNativeShell.post)) return Promise.resolve(null);
  return Promise.race([
    window.EvieNativeShell.post(payload).catch(() => null),
    new Promise((resolve) => setTimeout(() => resolve(null), 2500)),
  ]);
}

async function drainPendingCapture() {
  const pending = await nativePost({ type: "pending_capture" });
  const note = pending && String(pending.note || "").trim();
  if (!note) return;
  const key = pending.idempotency_key || "";
  await enqueueOffline("siri_capture", { text: note, executed: false }, key);
}

async function postNativeSnapshots() {
  const hk = await nativePost({ type: "healthkit_snapshot" });
  if (hk) {
    try {
      await api("/v1/device-gateway/healthkit/snapshot", {
        method: "POST",
        body: JSON.stringify({
          snapshot: hk.snapshot || {},
          captured_at: new Date().toISOString(),
          available: !!hk.available,
          reason: hk.reason || "no_entitlement",
        }),
      });
    } catch (_err) {}
  } else {
    try {
      await api("/v1/device-gateway/healthkit/snapshot", {
        method: "POST",
        body: JSON.stringify({
          snapshot: {},
          captured_at: new Date().toISOString(),
          available: false,
          reason: "no_entitlement",
        }),
      });
    } catch (_err) {}
  }
  const cal = await nativePost({ type: "calendar_snapshot" });
  if (cal && Array.isArray(cal.events) && cal.events.length) {
    try {
      await api("/v1/device-gateway/calendar/snapshot", {
        method: "POST",
        body: JSON.stringify({ events: cal.events, captured_at: new Date().toISOString() }),
      });
    } catch (_err) {}
  }
  const book = await nativePost({ type: "contacts_snapshot" });
  if (book && Array.isArray(book.contacts) && book.contacts.length) {
    try {
      await api("/v1/device-gateway/contacts/snapshot", {
        method: "POST",
        body: JSON.stringify({ contacts: book.contacts, captured_at: new Date().toISOString() }),
      });
    } catch (_err) {}
  }
  const note = await nativePost({ type: "notification_status" });
  try {
    await api("/v1/device-gateway/push/register", {
      method: "POST",
      body: JSON.stringify({
        token: "",
        delivery: "poll",
        bundle_id: "com.ev.evie.shell",
        authorization: (note && note.authorization) || "undetermined",
      }),
    });
  } catch (_err) {}
  await registerPhoneAlerts().catch(() => {});
}

async function pullEverywhere() {
  try {
    const boot = await api("/v1/device-gateway/sync/bootstrap");
    if (boot && boot.sync_cursor_str) state.syncCursor = boot.sync_cursor_str;
    else if (boot && typeof boot.sync_cursor === "string") state.syncCursor = boot.sync_cursor;
    if (state.syncCursor) {
      await api("/v1/device-gateway/sync/changes?cursor=" + encodeURIComponent(state.syncCursor)).catch(() => {});
    }
  } catch (_err) {}
}

async function replayOfflineQueue() {
  if (!state.deviceToken) return;
  try {
    const listed = await api("/v1/device-gateway/queue");
    const items = listed.items || [];
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (!item || item.state !== "pending") continue;
      const text = item.kind === "siri_capture" && item.payload && item.payload.text;
      const mark = item.idempotency_key || text || "";
      if (text && trust === "TRUSTED_OWNER_DEVICE" && mark && !state.drainedCaptures[mark]) {
        try {
          await sendText(text, mark);
          state.drainedCaptures[mark] = true;
        } catch (_err) {
          continue;
        }
      }
      if (!item.idempotency_key) continue;
      // Cycle 53 — exactly-once: the SERVER executes queued voice intents
      // under the queue's idempotency key (the turn gate dedupes on it).
      try {
        const out = await api("/v1/device-gateway/queue/replay", {
          method: "POST",
          body: JSON.stringify({ idempotency_key: item.idempotency_key }),
        });
        if (out && out.reply) {
          pushHistory("evie", out.reply);
          state.caption = out.reply;
          render();
        }
        state.queue = state.queue.filter((local) => !local || local.idempotency_key !== item.idempotency_key);
        saveOfflineQueueKeys();
      } catch (_err) {}
    }
  } catch (_err) {}
}

async function syncOnboarding() {
  if (!state.deviceToken) return;
  const status = state.status || {};
  const trusted = status.trust_state === "TRUSTED_OWNER_DEVICE";
  const steps = [];
  if (state.cameraRole && state.cameraRole !== "unknown") steps.push("camera_role");
  if (trusted) {
    steps.push("promoted");
    const prev = localStorage.getItem("evie_trust_seen");
    if (prev && prev !== "trusted") {
      state.caption = "Memory is now on for this phone.";
      render();
    }
    localStorage.setItem("evie_trust_seen", "trusted");
  }
  try {
    await api("/v1/device-gateway/onboarding", {
      method: "PUT",
      body: JSON.stringify({
        steps_completed: steps,
        camera_role_set: state.cameraRole === "pro" || state.cameraRole === "standard",
      }),
    });
  } catch (_err) {}
}

async function syncPhoneLife() {
  await drainPendingCapture().catch(() => {});
  await postNativeSnapshots().catch(() => {});
  await pullEverywhere().catch(() => {});
  await replayOfflineQueue().catch(() => {});
  await refreshInbox().catch(() => {});
  await reportBattery().catch(() => {});
  try {
    const snap = await api("/v1/device-gateway/status");
    if (snap) {
      state.status = snap;
      if (state.hello) state.hello.status = snap;
      await syncOnboarding().catch(() => {});
    }
  } catch (_err) {}
}

/* Cycle 52 — battery awareness: report once per sync when the platform
   exposes the Battery Status API (Android/desktop Chrome). iOS Safari
   does not expose it; the row then reads "not reported". */
async function reportBattery() {
  if (!state.deviceToken) return;
  if (!navigator.getBattery) return;
  const b = await navigator.getBattery();
  if (!b || typeof b.level !== "number") return;
  await api("/v1/device-gateway/heartbeat", {
    method: "POST",
    body: JSON.stringify({
      instance_id: state.instanceId,
      method: "battery",
      battery_percent: Math.round(b.level * 100),
    }),
  });
}

function pushHistory(role, text) {
  if (!text) return;
  const previous = state.history[state.history.length - 1];
  if (previous && previous.role === role && previous.text === text) return;
  state.history.push({ role: role, text: text });
  if (state.history.length > 24) state.history.shift();
  paintConversation();
}

function paintConversation(query) {
  const list = $("history");
  if (!list) return;
  while (list.firstChild) list.removeChild(list.firstChild);
  const needle = query != null ? query : (($("conv-q") && $("conv-q").value) || "");
  let turns = state.history || [];
  if (window.EvieSearch && typeof window.EvieSearch.filterTurns === "function") {
    turns = window.EvieSearch.filterTurns(turns, needle);
  } else if (String(needle).trim()) {
    const q = String(needle).trim().toLowerCase();
    turns = turns.filter((item) => String(item.text || "").toLowerCase().indexOf(q) !== -1);
  }
  turns.forEach((item) => {
    const li = document.createElement("li");
    li.className = item.role === "user" ? "as-user" : "as-evie";
    li.textContent = item.text;
    list.appendChild(li);
  });
}

function pushActivity(text) {
  state.activity.unshift({ text: text, at: Date.now() });
  if (state.activity.length > 12) state.activity.pop();
  const list = $("activity");
  while (list.firstChild) list.removeChild(list.firstChild);
  state.activity.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item.text;
    list.appendChild(li);
  });
}

function openSurface(surface, origin, opener) {
  const previous = document.querySelector('.sheet[role="dialog"]:not([hidden])');
  if (!previous) state._roomReturnFocus = opener || (document.activeElement?.matches("button, input, textarea, summary, a[href]") ? document.activeElement : $("more-btn"));
  $("room-tool-search").value = "";
  $("room-tool-list").querySelectorAll("button").forEach(button => { button.hidden = false; });
  $("room-tool-list").querySelectorAll(".room-tool-group").forEach(group => { group.hidden = false; });
  $("room-tool-empty").hidden = true;
  const map = {
    more: "more-sheet",
    today: "today-sheet",
    weather: "weather-sheet",
    health: "health-sheet",
    looks: "looks-sheet",
    people: "people-sheet",
    routines: "routines-sheet",
    queue: "queue-sheet",
    capture: "capture-sheet",
    search: "search-sheet",
    memory: "memory-sheet",
    conversation: "conversation-sheet",
    devices: "devices-sheet",
    activity: "activity-sheet",
    inbox: "inbox-sheet",
    mission: "mission-sheet",
    privacy: "settings-sheet",
  };
  ["more-sheet", "today-sheet", "weather-sheet", "health-sheet", "looks-sheet", "people-sheet", "routines-sheet", "queue-sheet", "capture-sheet", "search-sheet", "memory-sheet", "conversation-sheet", "devices-sheet", "activity-sheet", "inbox-sheet", "mission-sheet", "settings-sheet"].forEach((id) => {
    const on = map[surface] === id;
    const el = $(id);
    if (!el) return;
    el.classList.remove("from-left", "from-right");
    if (on && origin) el.classList.add(origin);
    showSheet(id, on);
  });
  if (surface === "inbox") refreshInbox();
  if (surface === "conversation") paintConversation();
  if (surface === "today") refreshToday();
  if (surface === "memory") refreshMemories();
  if (surface === "privacy") refreshStatus();
  if (surface === "queue") refreshQueue();
  if (surface === "routines") loadRoutines();
  if (surface === "people") refreshPeople();
  if (surface === "mission") refreshMission();
  if (surface === "looks") refreshLooks();
  if (surface === "health") refreshHealth();
  if (surface === "weather") refreshWeather();
  state.surface = surface;
  syncQuietRoom();
}

function showSheet(id, on) {
  const el = $(id);
  if (!el) return;
  const wasHidden = el.hidden;
  if (!on && !wasHidden && id === "capture-sheet" && typeof cleanupEvieVoiceNote === "function") cleanupEvieVoiceNote();
  el.hidden = !on;
  if (id === "welcome") return;
  if (on && wasHidden) {
    el._returnFocus = document.activeElement;
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    const heading = el.querySelector("h2, h1");
    if (heading) {
      if (!heading.id) heading.id = id + "-title";
      el.setAttribute("aria-labelledby", heading.id);
      heading.tabIndex = -1;
    }
    requestAnimationFrame(() => {
      if (!el.hidden) (heading || el.querySelector("button, input"))?.focus({ preventScroll: true });
    });
  }
  const ready = $("ready-ui");
  const hasDialog = !!document.querySelector('.sheet[role="dialog"]:not([hidden])');
  if (ready) ready.inert = hasDialog;
  if (!on && !wasHidden && !hasDialog) {
    const target = state._roomReturnFocus?.getClientRects().length ? state._roomReturnFocus : $("more-btn");
    target?.focus({ preventScroll: true });
  }
  syncQuietRoom();
  if (on && id === "settings-sheet" && window.EvieCapabilities) {
    window.EvieCapabilities.refresh({ api: (path) => api(path, { _useDeviceToken: true }) });
  }
  if (on && id === "settings-sheet") fillSense().catch(() => {});
  if (on && id === "settings-sheet") fillPrivacyStance().catch(() => {});
  if (on && id === "conversation-sheet") loadTurnHistory().catch(() => {});
  if (on && id === "conversation-sheet") loadMemoryBrowser().catch(() => {});

  if (on && id === "more-sheet") loadQuickActions().catch(() => {});
}

/* Cycle 65 — EV Sense: the consented-sensor panel, rendered from the
   server-computed /sense read. Values are shown as STATE, not data —
   health numbers themselves never leave the phone. */
async function fillSense() {
  const body = await api("/v1/device-gateway/sense", { _useDeviceToken: true }).catch(() => null);
  if (!body || body.ok === false) return;
  const hk = body.healthkit || {};
  const fmtBytes = (n) => (typeof n === "number" && n > 0 ? Math.round(n / 1e9) + " GB free" : "not reported");
  const nudges = body.nudges || {};
  fillDl("sense-meta", [
    ["Health snapshot", (hk.available ? "shared · " + (hk.freshness || "reported") : "not shared") + " · never sent to a model"],
    ["Battery", typeof body.battery_percent === "number" ? Math.round(body.battery_percent) + "%" : "not reported"],
    ["Storage", fmtBytes(body.storage_free_bytes)],
    ["Camera", body.camera_capability ? "allowed for Look" : "not granted"],
    ["Push", String(body.push_delivery || "poll")],
    ["Nudges", (nudges.enabled === false ? "off" : "on") + (nudges.quiet_now ? " · quiet hours now" : " · quiet " + (nudges.quiet_start || "") + "–" + (nudges.quiet_end || ""))],
    ["Heading out", headingLabel(body)],
    ["People", body.people_count ? body.people_count + " enrolled" : "roster — tap to add"],
    ["Voice", body.voice_enrolled ? "enrolled — tap to re-check" : "not enrolled — tap to enroll"],
  ]);
  if ($("voice-enroll")) {
    $("voice-enroll").hidden = false;
  }
  const peopleRow = document.querySelector("#sense-meta dt:last-of-type");
  if (peopleRow) peopleRow.onclick = () => enrollPerson().catch(() => {});
  const voiceRow = document.querySelector("#sense-meta dt:last-of-type");
  if (voiceRow) voiceRow.onclick = () => (body.voice_enrolled ? verifyVoice() : enrollVoice()).catch(() => {});
}

/* Cycle 68 — enrolled people: the owner names who matters; no biometrics.
   The roster lives in the owner's memory graph and feeds every Look. */
async function enrollPerson() {
  const name = (prompt("Person's name:") || "").trim();
  if (!name) return;
  const relation = (prompt("Relation (friend, family, colleague, other):") || "other").trim() || "other";
  const res = await api("/v1/device-gateway/people/enroll", {
    method: "POST",
    body: JSON.stringify({ name, relation }),
  });
  if (res.ok === false) {
    pushActivity("Could not enroll: " + String(res.error || "unknown relation"));
    return;
  }
  pushActivity(name + " enrolled");
  fillSense().catch(() => {});
}

/* Cycle 69 — voice enrollment from the phone: 5 short spoken clips,
   consent explicit, raw audio never stored (encrypted voiceprint only).
   Same runtime as the owner-trust API. */
async function enrollVoice() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("No microphone on this phone");
  }
  if (!confirm("Record 5 short clips of your voice? A voiceprint is stored encrypted; the recordings are not kept.")) return;
  const samples = [];
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } });
  try {
    for (let i = 0; i < 5; i += 1) {
      textOf($("sense-hint") || {}, `Sample ${i + 1} of 5 — speak now…`);
      const chunks = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => chunks.push(e.data);
      const done = new Promise((resolve) => { rec.onstop = resolve; });
      rec.start();
      await new Promise((r) => setTimeout(r, 1500));
      rec.stop();
      await done;
      const blob = new Blob(chunks);
      const buf = await blob.arrayBuffer();
      // Re-encode to 16k mono PCM16 WAV via the existing audio path if present.
      const wavB64 = (window.EvieAudio && window.EvieAudio.toWavB64) ? await window.EvieAudio.toWavB64(buf) : btoa(String.fromCharCode(...new Uint8Array(buf)));
      samples.push(wavB64);
    }
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
  const res = await api("/v1/device-gateway/voice/enroll", {
    method: "POST",
    body: JSON.stringify({ samples, consent: true }),
  });
  if (res.ok === false) {
    pushActivity("Voice enrollment failed: " + String(res.detail || res.error || "unknown"));
    return;
  }
  pushActivity("Voice enrolled · v" + res.version);
  fillSense().catch(() => {});
}

/* Cycle 70 — spoken voice check: one clip against the enrolled voiceprint;
   success opens a 120 s window for consequential sends (text/call). */
async function verifyVoice() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("No microphone on this phone");
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } });
  try {
    const chunks = [];
    const rec = new MediaRecorder(stream);
    rec.ondataavailable = (e) => chunks.push(e.data);
    const done = new Promise((resolve) => { rec.onstop = resolve; });
    rec.start();
    await new Promise((r) => setTimeout(r, 1800));
    rec.stop();
    await done;
    const buf = await new Blob(chunks).arrayBuffer();
    const wavB64 = (window.EvieAudio && window.EvieAudio.toWavB64) ? await window.EvieAudio.toWavB64(buf) : btoa(String.fromCharCode(...new Uint8Array(buf)));
    const res = await api("/v1/device-gateway/voice/verify", {
      method: "POST",
      body: JSON.stringify({ audio_b64: wavB64 }),
    });
    pushActivity(res.ok ? "Voice check passed · 2 min" : "Voice check failed — try again");
    return !!res.ok;
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

/* Cycle 66 — heading-out: opt-in, foreground-only geofence against the
   Home Station's home anchor. Tapping the row asks for consent + location
   permission once; while consented and the page is visible, position
   samples post to /heading-out. */
function headingLabel(body) {
  const ho = body.heading_out || {};
  if (!ho.consent) return "off · tap to enable";
  return "on · " + (ho.state || "unknown") + (ho.quiet_now ? "" : "");
}

async function toggleHeadingOut() {
  const current = await api("/v1/device-gateway/heading-out", { _useDeviceToken: true }).catch(() => null);
  const consented = !!(current && current.consent);
  if (consented) {
    await api("/v1/device-gateway/heading-out", {
      method: "POST",
      body: JSON.stringify({ consent: false }),
    });
    pushActivity("Heading out off");
    return;
  }
  if (!navigator.geolocation) throw new Error("No location on this phone");
  const pos = await new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 10000, maximumAge: 60000 });
  });
  await api("/v1/device-gateway/heading-out", {
    method: "POST",
    body: JSON.stringify({ consent: true, lat: pos.coords.latitude, lng: pos.coords.longitude }),
  });
  pushActivity("Heading out on · foreground only");
  startHeadingOutWatcher();
}

let headingWatchId = null;
function startHeadingOutWatcher() {
  if (headingWatchId !== null || !navigator.geolocation) return;
  headingWatchId = navigator.geolocation.watchPosition(
    (pos) => {
      api("/v1/device-gateway/heading-out", {
        method: "POST",
        body: JSON.stringify({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
      }).catch(() => {});
    },
    () => {},
    { enableHighAccuracy: false, maximumAge: 120000, timeout: 20000 }
  );
}

/* Cycle 72 — recent turns on this phone, from the durable turn receipts.
   Each turn carries provenance chips (tool · route · executed). */
async function loadTurnHistory() {
  const body = await api("/v1/device-gateway/history?limit=20", { _useDeviceToken: true }).catch(() => null);
  const host = $("turn-history");
  if (!host) return;
  host.replaceChildren();
  const turns = (body && body.turns) || [];
  if (!turns.length) {
    const p = document.createElement("p");
    p.className = "quiet";
    p.textContent = "No turns recorded yet on this phone.";
    host.appendChild(p);
    return;
  }
  turns.forEach((turn) => {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    const text = document.createElement("div");
    text.className = "turn-text";
    text.textContent = turn.text || turn.kind || "";
    const chips = document.createElement("div");
    chips.className = "turn-chips";
    (turn.chips || []).forEach((chip) => {
      const c = document.createElement("span");
      c.className = "chip" + (chip.executed ? "" : " off");
      c.textContent = [chip.tool, chip.route, chip.executed ? "done" : "not done"].filter(Boolean).join(" · ");
      chips.appendChild(c);
    });
    wrap.appendChild(text);
    wrap.appendChild(chips);
    host.appendChild(wrap);
  });
}

/* Cycle 73 — read-only memory browser: what Evie remembers, recent first.
   No edit verbs on this surface; corrections live in the privacy center. */
async function loadMemoryBrowser() {
  const body = await api("/v1/device-gateway/memory?limit=25", { _useDeviceToken: true }).catch(() => null);
  const host = $("memory-browser");
  if (!host) return;
  host.replaceChildren();
  if (body && body.sandbox) {
    const p = document.createElement("p");
    p.className = "quiet";
    p.textContent = body.note || "Personal memory is off on this device.";
    host.appendChild(p);
    return;
  }
  const memories = (body && body.memories) || [];
  if (!memories.length) {
    const p = document.createElement("p");
    p.className = "quiet";
    p.textContent = "Nothing remembered yet.";
    host.appendChild(p);
    return;
  }
  memories.forEach((memory) => {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    const text = document.createElement("div");
    text.className = "turn-text";
    text.textContent = memory.text || "";
    const chips = document.createElement("div");
    chips.className = "turn-chips";
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = [memory.kind, memory.provenance].filter(Boolean).join(" · ");
    chips.appendChild(chip);
    wrap.appendChild(text);
    wrap.appendChild(chips);
    host.appendChild(wrap);
  });
}

/* Cycle 74 — tactical brief page: right-now system state, read only. */
async function loadTactical() {
  const body = await api("/v1/device-gateway/tactical", { _useDeviceToken: true }).catch(() => null);
  const host = $("tactical-body");
  if (!host) return;
  host.replaceChildren();
  if (!body || body.ok === false) {
    const p = document.createElement("p");
    p.className = "quiet";
    p.textContent = "Brief unavailable right now.";
    host.appendChild(p);
    return;
  }
  const rows = [
    ["Timers", (body.timers || []).map((t) => t.label || t.id).join(", ") || "none running"],
    ["Inbox unread", String(body.inbox_unread)],
    ["Devices", body.devices_online + " of " + body.devices_total + " online"],
    ["Heading out", String(body.heading_out)],
    ["Voice lease", body.voice_lease ? "held" : "free"],
    ["Nudges", (body.nudges && body.nudges.enabled === false ? "off" : "on") + (body.nudges && body.nudges.quiet_now ? " · quiet now" : "")],
  ];
  rows.forEach(([k, v]) => {
    const wrap = document.createElement("div");
    wrap.className = "turn";
    const line = document.createElement("div");
    line.className = "turn-text";
    line.textContent = k + ": " + v;
    wrap.appendChild(line);
    host.appendChild(wrap);
  });
}

/* Cycle 85 — "What Evie keeps": one honest privacy answer, server-composed. */
async function fillPrivacyStance() {
  const body = await api("/v1/device-gateway/privacy", { _useDeviceToken: true }).catch(() => null);
  const host = $("privacy-stance");
  if (!host) return;
  host.replaceChildren();
  if (!body || body.ok === false) return;
  const add = (label, items, cls) => {
    if (!items || !items.length) return;
    const h = document.createElement("p");
    h.className = "quiet";
    h.textContent = label;
    host.appendChild(h);
    items.forEach((item) => {
      const d = document.createElement("div");
      d.className = cls;
      d.textContent = (cls === "turn-text" ? "· " : "× ") + item;
      host.appendChild(d);
    });
  };
  add("Kept", body.kept, "turn-text");
  add("Never kept", body.never_kept, "quiet");
  const controls = document.createElement("p");
  controls.className = "quiet";
  controls.textContent = "Your controls: " + (body.controls || []).join(" · ");
  host.appendChild(controls);
}

/* Cycle 51 — one-tap quick actions: server-computed, capability-gated;
   tapping a chip sends its utterance through the same trusted text path a
   spoken turn would take. No new authority lives client-side. */
async function loadQuickActions() {
  const host = $("qa-chips");
  if (!host) return;
  const body = await api("/v1/device-gateway/quick-actions").catch(() => null);
  host.textContent = "";
  (body && body.actions ? body.actions : []).forEach((action) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip chip-plain qa-chip";
    chip.textContent = action.label || action.id;
    chip.title = action.hint || "";
    chip.addEventListener("click", () => {
      showSheet("more-sheet", false);
      sendText(action.utterance);
    });
    host.appendChild(chip);
  });
}

document.addEventListener("keydown", (event) => {
  const dialog = document.querySelector('.sheet[role="dialog"]:not([hidden])');
  if (!dialog) return;
  if (event.key === "Escape") {
    const close = dialog.querySelector("[data-close]");
    if (close) { event.preventDefault(); close.click(); }
  }
  if (event.key === "Tab") {
    const controls = Array.from(dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href], [tabindex="0"]')).filter(el => el.getClientRects().length);
    if (!controls.length) return;
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !controls.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
  }
});

function anySheetOpen() {
  return ["conversation-sheet", "devices-sheet", "activity-sheet", "inbox-sheet", "mission-sheet", "settings-sheet", "more-sheet", "today-sheet", "weather-sheet", "health-sheet", "looks-sheet", "people-sheet", "routines-sheet", "queue-sheet", "capture-sheet", "search-sheet", "memory-sheet", "camera-sheet", "welcome"]
    .some((id) => {
      const el = $(id);
      return !!(el && !el.hidden);
    });
}

/* Whole-page slide language: when a swipe commits, the presence page glides
   aside while the destination sheet slides in from the same edge; when the
   sheet closes, the page glides back home. */
const STAGE_OUT_CURVE = "cubic-bezier(0.32, 0.72, 0.22, 1)";
const STAGE_HOME_CURVE = "cubic-bezier(0.16, 1, 0.3, 1)";

function stageSlideAside(direction) {
  const stage = $("ready-ui");
  if (!stage) return;
  stage.style.willChange = "transform, opacity";
  stage.style.transition =
    "transform 460ms " + STAGE_OUT_CURVE + ", opacity 400ms " + STAGE_OUT_CURVE;
  stage.style.transform = "translate3d(" + (direction * 12) + "%,0,0) scale(0.97)";
  stage.style.opacity = "0.55";
}

function stageReturn() {
  const stage = $("ready-ui");
  if (!stage) return;
  stage.style.transition =
    "transform 460ms " + STAGE_HOME_CURVE + ", opacity 360ms " + STAGE_HOME_CURVE;
  stage.style.transform = "";
  stage.style.opacity = "";
  window.setTimeout(() => {
    stage.style.transition = "";
    stage.style.willChange = "";
  }, 480);
}

/* Horizontal swipe navigation on the presence surface:
   left → Conversation, right → Privacy. Rubber-bands with the finger,
   locks to horizontal intent only, never fights vertical scroll,
   and ignores every interactive region. */
function initSwipes(openSurface) {
  const stage = $("ready-ui");
  if (!stage) return;
  const OPEN_AT = 72;          /* travel that commits a swipe */
  const FLICK_VELOCITY = 0.45; /* px/ms — a quick flick commits early */
  const FLICK_MIN_TRAVEL = 24; /* …but only if it genuinely moved */
  const MAX_DRAG = 110;        /* visual rubber-band cap */
  const RUBBER = 0.42;         /* finger→pixel follow ratio */
  const SETTLE_CURVE = "cubic-bezier(0.16, 1, 0.3, 1)";
  let startX = 0;
  let startY = 0;
  let dx = 0;
  let lastDx = 0;
  let lastT = 0;
  let velocity = 0;
  let locked = null;
  let tracking = false;
  let rafId = 0;
  let pendingShift = null;

  function interactive(target) {
    return !!(target && target.closest &&
      target.closest("button, input, a, textarea, select, form, .sheet, .scrim, .camera-ask, .choice-list, .quick-row"));
  }

  /* Paint at most once per frame, on the compositor (translate3d). */
  function paint() {
    rafId = 0;
    if (pendingShift === null) return;
    const eased = pendingShift;
    stage.style.transform = "translate3d(" + eased.toFixed(1) + "px,0,0)";
    stage.style.opacity = String(1 - (Math.abs(eased) / MAX_DRAG) * 0.18);
  }

  function follow(rawDx) {
    pendingShift = Math.sign(rawDx) * Math.min(Math.abs(rawDx) * RUBBER, MAX_DRAG);
    if (!rafId) rafId = requestAnimationFrame(paint);
  }

  function stopPaint() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    pendingShift = null;
  }

  function settle() {
    stopPaint();
    stage.style.transition = "transform 420ms " + SETTLE_CURVE + ", opacity 340ms " + SETTLE_CURVE;
    stage.style.transform = "";
    stage.style.opacity = "";
    window.setTimeout(() => { stage.style.transition = ""; stage.style.willChange = ""; }, 440);
  }

  function reset() {
    tracking = false;
    locked = null;
    dx = 0;
    lastDx = 0;
    lastT = 0;
    velocity = 0;
  }

  stage.addEventListener("touchstart", (ev) => {
    if (ev.touches.length !== 1 || anySheetOpen() || interactive(ev.target)) {
      tracking = false;
      return;
    }
    const t = ev.touches[0];
    tracking = true;
    startX = t.clientX;
    startY = t.clientY;
    dx = 0;
    lastDx = 0;
    lastT = 0;
    velocity = 0;
    locked = null;
  }, { passive: true });

  stage.addEventListener("touchmove", (ev) => {
    if (!tracking) return;
    if (ev.touches.length !== 1) {
      reset();
      settle();
      return;
    }
    const t = ev.touches[0];
    dx = t.clientX - startX;
    const dy = t.clientY - startY;
    const now = ev.timeStamp || performance.now();
    if (lastT) velocity = velocity * 0.6 + ((dx - lastDx) / Math.max(1, now - lastT)) * 0.4;
    lastDx = dx;
    lastT = now;
    if (!locked && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) {
      locked = Math.abs(dx) > Math.abs(dy) * 1.35 ? "h" : "v";
      if (locked === "h") stage.style.willChange = "transform, opacity";
      else tracking = false;
    }
    if (locked !== "h") return;
    if (ev.cancelable) ev.preventDefault();
    stage.style.transition = "none";
    follow(dx);
  }, { passive: false });

  function endGesture() {
    if (!tracking) return;
    const horizontal = locked === "h";
    const travel = dx;
    const v = velocity;
    reset();
    if (!horizontal) return;
    const flick = Math.abs(v) > FLICK_VELOCITY && Math.abs(travel) >= FLICK_MIN_TRAVEL;
    const goLeft = travel <= -OPEN_AT || (flick && v < 0);
    const goRight = travel >= OPEN_AT || (flick && v > 0);
    if (!goLeft && !goRight) {
      settle();
      return;
    }
    /* Commit: the page keeps travelling in the swipe direction while the
       destination sheet slides in from that same edge. */
    stopPaint();
    if (goLeft) {
      stageSlideAside(-1);
      openSurface("conversation", "from-right");
    } else {
      stageSlideAside(1);
      openSurface("privacy", "from-left");
    }
  }
  stage.addEventListener("touchend", endGesture, { passive: true });
  stage.addEventListener("touchcancel", endGesture, { passive: true });
}

/* Swipe-to-close inside the swipe-opened sheets: Conversation returns on a
   rightward drag, Privacy on a leftward one — mirroring how they opened.
   Vertical scrolling inside sheets stays native; horizontal drags drag the
   whole sheet with the finger, with flick-to-commit. */
function initSheetGestures() {
  const SHEET_CURVE = "cubic-bezier(0.32, 0.72, 0.22, 1)";
  const HOME_CURVE = "cubic-bezier(0.16, 1, 0.3, 1)";
  const CLOSE_AT = 96;
  const FLICK_VELOCITY = 0.45;
  const FLICK_MIN_TRAVEL = 24;
  const configs = [
    { id: "conversation-sheet", closeDir: 1 },
    { id: "settings-sheet", closeDir: -1 },
  ];

  configs.forEach((cfg) => {
    const sheet = $(cfg.id);
    if (!sheet) return;
    let startX = 0;
    let startY = 0;
    let dx = 0;
    let lastDx = 0;
    let lastT = 0;
    let velocity = 0;
    let locked = null;
    let tracking = false;
    let rafId = 0;
    let pendingShift = null;
    let closing = false;

    function interactive(target) {
      return !!(target && target.closest &&
        target.closest("button, input, a, textarea, select, .camera-ask, .choice-list"));
    }

    function paint() {
      rafId = 0;
      if (pendingShift === null) return;
      sheet.style.transform = "translate3d(" + pendingShift.toFixed(1) + "px,0,0)";
    }

    function follow(rawDx) {
      const towardClose = rawDx * cfg.closeDir;
      const travel = towardClose >= 0
        ? Math.min(towardClose, window.innerWidth * 0.8)
        : towardClose * 0.16; /* resisting the wrong way feels rubbery */
      pendingShift = travel * cfg.closeDir;
      if (!rafId) rafId = requestAnimationFrame(paint);
    }

    function stopPaint() {
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      pendingShift = null;
    }

    function clearDragStyles() {
      sheet.style.transition = "";
      sheet.style.transform = "";
      sheet.classList.remove("from-left", "from-right");
    }

    sheet.addEventListener("touchstart", (ev) => {
      if (closing || ev.touches.length !== 1 || interactive(ev.target)) {
        tracking = false;
        return;
      }
      const t = ev.touches[0];
      tracking = true;
      startX = t.clientX;
      startY = t.clientY;
      dx = 0;
      lastDx = 0;
      lastT = 0;
      velocity = 0;
      locked = null;
    }, { passive: true });

    sheet.addEventListener("touchmove", (ev) => {
      if (!tracking) return;
      if (ev.touches.length !== 1) {
        tracking = false;
        locked = null;
        stopPaint();
        sheet.style.transition = "transform 340ms " + HOME_CURVE;
        sheet.style.transform = "translate3d(0,0,0)";
        return;
      }
      const t = ev.touches[0];
      dx = t.clientX - startX;
      const dy = t.clientY - startY;
      const now = ev.timeStamp || performance.now();
      if (lastT) velocity = velocity * 0.6 + ((dx - lastDx) / Math.max(1, now - lastT)) * 0.4;
      lastDx = dx;
      lastT = now;
      if (!locked && (Math.abs(dx) > 10 || Math.abs(dy) > 10)) {
        locked = Math.abs(dx) > Math.abs(dy) * 1.35 ? "h" : "v";
        if (locked === "h") sheet.style.willChange = "transform";
        else tracking = false; /* vertical scroll stays native */
      }
      if (locked !== "h") return;
      if (ev.cancelable) ev.preventDefault();
      sheet.style.transition = "none";
      follow(dx);
    }, { passive: false });

    function endGesture() {
      if (!tracking) return;
      const horizontal = locked === "h";
      const travel = dx;
      const v = velocity;
      tracking = false;
      locked = null;
      dx = 0;
      lastDx = 0;
      lastT = 0;
      velocity = 0;
      stopPaint();
      if (!horizontal) return;
      const towardClose = travel * cfg.closeDir;
      const flickTowardClose = v * cfg.closeDir > FLICK_VELOCITY && Math.abs(travel) >= FLICK_MIN_TRAVEL;
      if (towardClose > CLOSE_AT || flickTowardClose) {
        closing = true;
        stageReturn();
        sheet.style.transition = "transform 300ms " + SHEET_CURVE;
        sheet.style.transform = "translate3d(" + (cfg.closeDir * 110) + "%,0,0)";
        window.setTimeout(() => {
          showSheet(cfg.id, false);
          closing = false;
        }, 290);
        window.setTimeout(clearDragStyles, 320);
      } else {
        sheet.style.transition = "transform 360ms " + HOME_CURVE;
        sheet.style.transform = "translate3d(0,0,0)";
        window.setTimeout(() => {
          sheet.style.transition = "";
          sheet.style.willChange = "";
        }, 380);
      }
    }
    sheet.addEventListener("touchend", endGesture, { passive: true });
    sheet.addEventListener("touchcancel", endGesture, { passive: true });
  });
}

function db() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("evie-pwa", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("cred");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbPut(key, value) {
  const idb = await db();
  await new Promise((resolve, reject) => {
    const tx = idb.transaction("cred", "readwrite");
    tx.objectStore("cred").put(value, key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function idbGet(key) {
  const idb = await db();
  return new Promise((resolve, reject) => {
    const tx = idb.transaction("cred", "readonly");
    const req = tx.objectStore("cred").get(key);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

async function idbDel(key) {
  const idb = await db();
  await new Promise((resolve, reject) => {
    const tx = idb.transaction("cred", "readwrite");
    tx.objectStore("cred").delete(key);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function saveToken(token) {
  await idbPut("device_token", token);
}

async function loadToken() {
  return idbGet("device_token");
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "content-type": "application/json" }, opts.headers || {});
  const useDevice = !!opts._useDeviceToken;
  const bearer = useDevice ? state.deviceToken : (state.accessToken || state.deviceToken);
  if (bearer) headers.Authorization = "Bearer " + bearer;
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) abort();
    else opts.signal.addEventListener("abort", abort, { once: true });
  }
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; abort(); }, opts._timeoutMs || 20000);
  const request = Object.assign({}, opts, { headers, signal: controller.signal });
  delete request._timeoutMs;
  delete request._retried;
  delete request._useDeviceToken;
  let res, body;
  try {
    res = await fetch(path, request);
    body = await res.json().catch(err => { if (controller.signal.aborted) throw err; return {}; });
  } catch (err) {
    if (timedOut) throw new Error("Home Station took too long to respond. Please try again.");
    throw err;
  } finally {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener("abort", abort);
  }
  if (res.status === 401 && state.deviceToken && !opts._retried) {
    let refreshed;
    try {
      refreshed = await api("/v1/device-gateway/session", {
        method: "POST", _retried: true, _useDeviceToken: true,
        signal: opts.signal, _timeoutMs: opts._timeoutMs,
      });
    } catch (err) {
      // A transient refresh failure is not evidence that pairing was revoked.
      if (err.status !== 401 && err.status !== 403) throw err;
      await idbDel("device_token").catch(() => {});
      await idbDel("access_token").catch(() => {});
      state.deviceToken = null;
      state.accessToken = null;
      throw err;
    }
    if (refreshed && refreshed.access_token) {
      state.accessToken = refreshed.access_token;
      await idbPut("access_token", refreshed.access_token).catch(() => {});
      if (refreshed.device) state.device = refreshed.device;
      if (refreshed.status) state.status = refreshed.status;
      return api(path, Object.assign({}, opts, { _retried: true }));
    }
    throw new Error("Home Station could not renew this session. Retry connection; your pairing is preserved.");
  }
  if (!res.ok) {
    const err = new Error(detailOf(body, res.statusText || "request failed"));
    err.status = res.status;
    err.body = body;
    err.error_code = res.headers.get("X-Error-Code") || body.error_code || (body.detail && body.detail.error_code);
    if (err.error_code && body && typeof body === "object") body.error_code = err.error_code;
    const detail = body && body.detail;
    if (detail && typeof detail === "object") {
      err.failed_stage = detail.failed_stage;
      err.provider_status = detail.provider_status;
      err.provider_code = detail.provider_code;
      err.provider_message = detail.provider_message;
    }
    throw err;
  }
  return body;
}

function detectPlatform() {
  if (window.EvieNativeShell) return "ios";
  const ua = navigator.userAgent || "";
  if (/iPhone|iPad|iPod/.test(ua)) return "ios";
  return "web";
}

function isStandalonePwa() {
  return !!(window.navigator.standalone || (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches));
}

function cameraRoleLabel() {
  if (state.cameraRole === "pro") return "preferred (16 Pro)";
  if (state.cameraRole === "standard") return "fallback (SE)";
  return "not set";
}

function setCameraRole(role) {
  const next = role === "pro" || role === "standard" ? role : "unknown";
  state.cameraRole = next;
  try { localStorage.setItem("evie_camera_role", next); } catch (_err) {}
  paintCameraRole();
  if (next === "unknown") pushActivity("Camera role cleared");
  else pushActivity("Camera set · " + cameraRoleLabel());
  hello().catch(() => {});
}

function paintCameraRole() {
  document.querySelectorAll("[data-camera-role]").forEach((btn) => {
    const on = btn.getAttribute("data-camera-role") === state.cameraRole;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
  const ask = $("camera-ask");
  if (ask) ask.hidden = !state.deviceToken || state.cameraRole !== "unknown" || !!state.talking;
  const line = $("camera-role-line");
  if (line) {
    line.hidden = !state.deviceToken || state.cameraRole === "unknown";
    line.textContent = "Camera · " + cameraRoleLabel();
  }
  const status = $("camera-role-status");
  if (status) status.textContent = "Current: " + cameraRoleLabel();
}

function cameraHardware() {
  const base = (function () {
    if (state.cameraRole === "pro") {
      return { camera_quality: "pro", camera_preference_rank: 0, provenance: "owner_declared" };
    }
    if (state.cameraRole === "standard") {
      return { camera_quality: "standard", camera_preference_rank: 10, provenance: "owner_declared" };
    }
    return { camera_quality: "unknown", camera_preference_rank: 50, provenance: "undeclared" };
  })();
  // Media capability is a browser fact, reported so the server never offers
  // recording this phone cannot honour. Safari on iOS usually cannot record
  // video from a live camera stream; when it cannot, we send timestamped stills.
  const media = {
    still: true,
    burst: true,
    video: videoRecordingSupported(),
  };
  const mime = preferredClipMime();
  if (mime) media.clip_mime = mime;
  return Object.assign(base, { media: media });
}

const CLIP_MIME_CANDIDATES = [
  "video/mp4;codecs=h264",
  "video/mp4",
  "video/webm;codecs=vp8",
  "video/webm",
];

function preferredClipMime() {
  if (typeof window.MediaRecorder === "undefined") return null;
  if (typeof window.MediaRecorder.isTypeSupported !== "function") return null;
  for (let i = 0; i < CLIP_MIME_CANDIDATES.length; i += 1) {
    if (window.MediaRecorder.isTypeSupported(CLIP_MIME_CANDIDATES[i])) {
      return CLIP_MIME_CANDIDATES[i];
    }
  }
  return null;
}

function videoRecordingSupported() {
  return preferredClipMime() !== null;
}

function refreshInstallHint() {
  const el = $("install-hint");
  if (!el) return;
  const ios = detectPlatform() === "ios" || /iPhone|iPad|iPod/.test(navigator.userAgent || "");
  el.hidden = !(ios && !isStandalonePwa());
}

function showUpdateLine() {
  const el = $("update-line");
  if (!el) return;
  const toast = window.EvieUpdate && typeof window.EvieUpdate.toast === "function"
    ? window.EvieUpdate.toast({ latest_web_build: state.updateAvailable && state.updateAvailable.latest }, CLIENT_BUILD)
    : null;
  if (state.updateAvailable && state.updateAvailable.latest) {
    el.hidden = false;
    if (toast) {
      el.textContent = toast.title + " · " + toast.body + " · " + toast.cta;
    } else {
      const ready = state.updateAvailable.sw_ready ? "Update ready" : "Update available";
      el.textContent = ready + " · tap to reload (" + state.updateAvailable.latest + ")";
    }
  } else {
    el.hidden = true;
    el.textContent = "";
  }
}

function watchServiceWorkerUpdate(registration) {
  if (!registration) return;
  const markReady = () => {
    if (state.updateAvailable && state.updateAvailable.sw_ready) return;
    if (!state.updateAvailable) {
      state.updateAvailable = { latest: "Home Station" };
    }
    state.updateAvailable.sw_ready = true;
    pushActivity("Update ready · tap to reload");
    showUpdateLine();
  };
  if (registration.waiting && navigator.serviceWorker.controller) markReady();
  if (!registration.addEventListener) return;
  registration.addEventListener("updatefound", () => {
    const installing = registration.installing;
    if (!installing || !installing.addEventListener) return;
    installing.addEventListener("statechange", () => {
      if (installing.state === "installed" && navigator.serviceWorker.controller) {
        markReady();
      }
    });
  });
}

async function nativeSnapshot() {
  if (!(window.EvieNativeShell && window.EvieNativeShell.post)) {
    return {
      capabilities: ["foreground_voice", "camera", "text", "notification"],
      hardware: cameraHardware(),
      permissions: {},
      native_shell: false,
      standalone: isStandalonePwa(),
    };
  }
  const reply = await window.EvieNativeShell.post({ type: "capabilities" }).catch(() => null);
  const caps = (reply && reply.endpoint_capabilities) || (reply && reply.capabilities) || [];
  const endpoint = ["foreground_voice", "camera", "text", "notification", "microphone", "location", "clipboard"];
  caps.forEach((name) => {
    if (endpoint.indexOf(name) === -1 && ["foreground_voice", "camera", "text", "notification", "microphone", "location", "clipboard"].indexOf(name) >= 0) {
      endpoint.push(name);
    }
  });
  return {
    capabilities: endpoint,
    hardware: Object.assign(cameraHardware(), (reply && reply.hardware) || {}),
    permissions: (reply && (reply.permissions || reply.permission_evidence)) || {},
    native_shell: true,
    standalone: isStandalonePwa(),
  };
}

// ---- Boot-stage diagnostics (semantic separation: auth ≠ compatibility) ----
//   B00 APP_BOOT · B01 ASSET_INTEGRITY · B02 VERSION_COMPATIBILITY
//   A00 DEVICE_CREDENTIAL · A01 AUTH_REQUEST · A02 AUTHENTICATED
function bootFail(stage, kind, mood, detail) {
  let extra = "";
  try {
    extra = detail ? " · " + JSON.stringify(detail) : "";
  } catch (_err) {}
  state.caption = "FAILED AT " + stage + " · " + kind + extra;
  setMood(mood || "Evie couldn't start.");
  setConn("DISCONNECTED");
  render();
}

function oneShot(key) {
  // Returns true the first time a repair is attempted per page session.
  try {
    if (sessionStorage.getItem(key)) return false;
    sessionStorage.setItem(key, "1");
    return true;
  } catch (_err) {
    return true; // storage unavailable: still bounded by caller behavior
  }
}

async function updateServiceWorkerOnce() {
  // Bounded SW update: ask once, reload once. Returns false when the budget
  // is spent so callers show a terminal error instead of looping.
  try {
    if (!navigator.serviceWorker || !navigator.serviceWorker.getRegistration) return false;
    const reg = await navigator.serviceWorker.getRegistration("/evie/");
    if (reg && reg.update) await reg.update();
  } catch (_err) {}
  if (!oneShot("evie_sw_reload")) return false;
  setTimeout(() => location.reload(), 500);
  return true;
}

async function repairAssetsOnce() {
  // MIXED_ASSET_BUILD repair: drop every SW cache for this origin, refresh the
  // registration, and reload exactly once to re-fetch a coherent asset set.
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister().catch(() => {})));
    }
    if (window.caches && caches.keys) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k).catch(() => {})));
    }
  } catch (_err) {}
  if (!oneShot("evie_asset_repair")) return false;
  setTimeout(() => location.reload(), 500);
  return true;
}

function backgroundUpdateServiceWorker() {
  // Fire-and-forget: fetches the new SW so it activates on next launch.
  // Never reloads the current session mid-use.
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistration) {
      navigator.serviceWorker
        .getRegistration("/evie/")
        .then((reg) => {
          watchServiceWorkerUpdate(reg);
          return reg && reg.update && reg.update();
        })
        .catch(() => {});
    }
  } catch (_err) {}
}
const HELLO_RECHECK_MS = 15 * 60 * 1000;
function scheduleHelloRecheck() {
  // Long-lived tabs miss deploys: re-run hello every 15min while READY so a
  // redeployed Home Station surfaces via the display-only update notice.
  // Skips while talking, unauthenticated, or not READY; never throws.
  if (state.helloRecheckTimer) return;
  state.helloRecheckTimer = setInterval(() => {
    try {
      if (state.conn !== "READY" || state.talking || state._talkInflight) return;
      if (!state.deviceToken) return;
      hello().catch(() => {});
    } catch (_err) {}
  }, HELLO_RECHECK_MS);
}

async function hello() {
  setConn("AUTHENTICATING");
  // ---- B01 ASSET_INTEGRITY ----------------------------------------------
  // Served HTML must belong to the same release as this app.js. A mismatch
  // means a mixed/partial deploy: repair caches ONCE, then give up with a
  const metaEl = document.querySelector('meta[name="evie-build"]');
  const metaBuild = (metaEl && metaEl.content) || CLIENT_BUILD;
  if (metaBuild !== CLIENT_BUILD) {
    if (!(await repairAssetsOnce())) {
      return bootFail(
        "B01",
        "MIXED_ASSET_BUILD",
        "Evie update couldn't complete.",
        { client: CLIENT_BUILD, served_html: metaBuild }
      );
    }
    return; // repairAssetsOnce scheduled exactly one reload
  }

  // ---- A01 AUTH_REQUEST -------------------------------------------------
  const native = await nativeSnapshot();
  let body;
  try {
    body = await api("/v1/device-gateway/hello", {
      method: "POST",
      body: JSON.stringify({
        protocol_version: PROTOCOL_VERSION,
        client_build: CLIENT_BUILD,
        instance_id: state.instanceId,
        capabilities: native.capabilities,
        foreground: !document.hidden,
        platform: detectPlatform(),
        hardware: native.hardware,
        permissions: native.permissions,
        native_shell: !!native.native_shell,
      }),
    });
  } catch (err) {
    // Structured protocol rejection: credentials may be fine; the client is
    // simply too old for this server. One bounded SW update + reload, then a
    // terminal, honest message — never an authenticate/reload loop.
    const code = (err && err.body && err.body.detail && err.body.detail.error_code) || "";
    if (err.status === 409 || code === "CLIENT_PROTOCOL_UNSUPPORTED") {
      if (!(await updateServiceWorkerOnce())) {
        return bootFail("B02", "CLIENT_UPDATE_REQUIRED", "Evie needs an update to connect to this Home Station.", { reason: code || "INCOMPATIBLE_PROTOCOL" });
      }
      return; // one reload scheduled
    }
    if (err.status === 401 || err.status === 403) {
      return bootFail("A01", "AUTHENTICATION_FAILED", "Couldn't authenticate this device.", { status: err.status });
    }
    throw err;
  }

  // ---- A02 AUTHENTICATED · B02 VERSION_COMPATIBILITY ---------------------
  // Build identity is NOT authorization. An older-but-protocol-compatible
  // client reaches READY and merely learns an update exists.
  if (body.update_required) {
    if (!(await updateServiceWorkerOnce())) {
      return bootFail(
        "B02",
        body.update_reason || "CLIENT_UPDATE_REQUIRED",
        "Evie needs an update to connect to this Home Station.",
        { latest: body.latest_web_build }
      );
    }
    return;
  }
  if (body.latest_web_build && body.latest_web_build !== CLIENT_BUILD) {
    // Non-blocking: stay READY now; updated assets activate on next launch.
    state.updateAvailable = { latest: body.latest_web_build };
    pushActivity("Update available · server build " + body.latest_web_build);
    backgroundUpdateServiceWorker();
    showUpdateLine();
  }
  try {
    sessionStorage.removeItem("evie_build_reload");
  } catch (_err) {}
  // Deploy notice: a changed asset_manifest_hash across hellos in one tab
  // means the Home Station redeployed under us. Display only — auth and
  // READY are unaffected; the new assets activate on next launch.
  const seenManifest = (body && body.asset_manifest_hash) || "";
  if (seenManifest && state.assetManifestHash && seenManifest !== state.assetManifestHash) {
    pushActivity("Home Station updated · reload when convenient to pick up the new build");
  }
  if (seenManifest) state.assetManifestHash = seenManifest;
  state.hello = body;
  state.device = body.device;
  state.status = body.status || body.session_context || null;
  state.mediaBackend = body.recommended_backend || "auto";
  // A12: native capability handshake is optional and must never block READY.
  if (window.EvieMobileActions) {
    window.EvieMobileActions.configure({
      api: api,
      instanceId: state.instanceId,
      onActivity: (line) => pushActivity(line),
      onPresent: () => syncQuietRoom(),
      onStatus: () => syncQuietRoom(),
    });
    window.EvieMobileActions.handshake().then((snap) => {
      if (snap && snap.status) {
        body.mobile_actions = snap.status;
        fillMobileActions(body);
      }
    }).catch(() => {});
  }
  setMood("Ready");
  setConn("READY");
  subscribeWebPush().catch(() => {});
  await syncPhoneLife().catch(() => {});
}

/* Cycle 49 — Web Push (VAPID): subscribe when the PWA has notification
   permission (installed PWAs on iOS 16.4+/Android/desktop). Never blocks
   READY, never requests permission without a user-visible context, no-ops
   on unsupported browsers or when the server has no VAPID keys yet. */
async function subscribeWebPush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return;
  const keyBody = await api("/v1/device-gateway/vapid-public-key").catch(() => null);
  const key = keyBody && keyBody.application_server_key;
  if (!key) return;
  const keyBytes = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const existing = await reg.pushManager.getSubscription();
  const sub = existing || await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: keyBytes.buffer,
  });
  const json = sub.toJSON();
  if (!json.endpoint || !json.keys) return;
  await api("/v1/device-gateway/push/web-subscription", {
    method: "POST",
    body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
  });
  pushActivity("Push notifications on");
}

async function pair() {
  const token = $("pair-token").value.trim();
  if (!token) throw new Error("Enter the pairing code shown on your Mac.");
  setConn("AUTHENTICATING");
  const native = await nativeSnapshot();
  const body = await api("/v1/device-gateway/pair", {
    method: "POST",
    body: JSON.stringify({
      pairing_token: token,
      protocol_version: PROTOCOL_VERSION,
      client_version: CLIENT_BUILD,
      instance_id: state.instanceId,
      capabilities: native.capabilities,
      platform: detectPlatform(),
      hardware: native.hardware,
      permissions: native.permissions,
      native_shell: !!native.native_shell,
    }),
  });
  state.deviceToken = body.device_token;
  state.accessToken = body.access_token;
  state.device = body.device;
  await saveToken(body.device_token);
  await idbPut("access_token", body.access_token).catch(() => {});
  if (window.EvieNativeShell && window.EvieNativeShell.post) {
    window.EvieNativeShell.post({ type: "bind_session", token: body.device_token });
  }
  $("pair-token").value = "";
  await hello();
}

async function sendText(text, requestIdOverride) {
  // Replies and action confirmations belong in the room, not behind an inert
  // background while People/Today/Conversation is still open.
  if (document.querySelector('.sheet[role="dialog"]:not([hidden])')) openSurface("home");
  const requestId = requestIdOverride || crypto.randomUUID();
  markTtfaStart();
  state.userLine = text;
  state.caption = "…";
  pushHistory("user", text);
  setMood("Thinking");
  paintLive();
  // Cycle 54 — streamed states first (routing → thinking → reply). Falls
  // back to the classic request/response turn when SSE is unavailable.
  try {
    const streamed = await sendTextStreamed(text, requestId);
    if (streamed) return streamed;
  } catch (_err) {
    // fall through to the classic path; it owns the error surface
  }
  let body;
  try {
    body = await api("/v1/device-gateway/text", {
      method: "POST",
      body: JSON.stringify({
        text,
        instance_id: state.instanceId,
        request_id: requestId,
        idempotency_key: requestId,
      }),
    });
  } catch (err) {
    setMood(state.talking ? "Listening" : "Ready");
    throw err;
  }
  state.caption = body.reply || "";
  // Cycle 78 — cross-device handoff: when the thread was continued from
  // another device, say so quietly instead of pretending nothing moved.
  if (body.handoff && body.handoff.active_device_id) {
    pushActivity("Continued from your other device");
  }
  pushHistory("evie", body.reply || "");
  applyTurnOutcome(body);
  if (body.conversation_moved) await stopTalk();
  if (body.needs_camera) await captureCamera(body);
  else if (body.camera_request_id) await waitForCameraReceipt(body);
  if (body.phone_action && window.EvieMobileActions) {
    window.EvieMobileActions.present(body.phone_action);
  }
  setMood(state.talking ? "Listening" : "Ready");
  paintLive();
  return body;
}

const CLIP_CAPTURE_SECONDS = 4;
const BURST_FRAMES = 5;
const BURST_GAP_MS = 750;

function isRecordAction(action) {
  const value = String(action || "").toLowerCase();
  return value === "record" || value === "record_clip" || value === "record_video";
}

function cameraCopyFor(action) {
  if (!isRecordAction(action)) return "Opening camera";
  return videoRecordingSupported()
    ? "Recording a short clip on this phone"
    : "Capturing a short sequence of still frames (this browser cannot record video)";
}

async function grabStill(video, canvas) {
  await new Promise((r) => requestAnimationFrame(r));
  canvas.width = Math.min(video.videoWidth || 640, 1280);
  canvas.height = Math.min(video.videoHeight || 480, 720);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.7).split(",")[1];
}

/** Record a real clip with MediaRecorder when this browser supports it. */
async function recordClipBlob(stream) {
  const mime = preferredClipMime();
  if (!mime || typeof window.MediaRecorder === "undefined") return null;
  let recorder;
  try {
    recorder = new MediaRecorder(stream, { mimeType: mime });
  } catch (_err) {
    return null;
  }
  const chunks = [];
  const done = new Promise((resolve) => { recorder.onstop = resolve; });
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size) chunks.push(event.data);
  };
  recorder.start(250);
  const posters = [];
  const canvas = document.createElement("canvas");
  const wanted = BURST_FRAMES;
  for (let index = 0; index < wanted; index += 1) {
    await new Promise((r) => setTimeout(r, (CLIP_CAPTURE_SECONDS * 1000) / wanted));
    try {
      posters.push(await grabStill(streamVideo(), canvas));
    } catch (_err) {
      break;
    }
  }
  if (recorder.state !== "inactive") recorder.stop();
  await done;
  if (!chunks.length) return null;
  return { blob: new Blob(chunks, { type: mime }), posters: posters };
}

let _clipVideoEl = null;
function streamVideo() {
  return _clipVideoEl;
}

/* Cycle 54 — POST + ReadableStream SSE parse (EventSource cannot POST).
   state events update the caption honestly; the reply event completes the
   turn through the SAME code path as the classic response. */
async function sendTextStreamed(text, requestId) {
  const res = await fetch("/v1/device-gateway/text/stream", {
    method: "POST",
    headers: Object.assign({ "content-type": "application/json" }, state.deviceToken ? { Authorization: "Bearer " + state.deviceToken } : {}),
    body: JSON.stringify({
      text,
      instance_id: state.instanceId,
      request_id: requestId,
      idempotency_key: requestId,
    }),
  });
  if (!res.ok || !res.body || !/text\/event-stream/.test(res.headers.get("content-type") || "")) {
    return null;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalBody = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const lines = chunk.split("\n");
      const event = (lines.find((l) => l.startsWith("event:")) || "").replace(/^event:\s*/, "").trim();
      const dataLine = lines.find((l) => l.startsWith("data:")) || "";
      let data = {};
      try { data = JSON.parse(dataLine.replace(/^data:\s*/, "")); } catch (_err) { data = {}; }
      if (event === "state" && data.stage === "routing") {
        state.caption = "Routing your request…";
        paintLive();
      } else if (event === "state" && data.stage === "thinking") {
        state.caption = "Thinking…";
        paintLive();
      } else if (event === "tts" && data.audio_b64) {
        // Cycle 57 — the typed answer speaks: queued sentence WAVs play in
        // order. Skipped while a live voice session owns the speaker.
        if (!state.talking) playTypedTts(data.audio_b64, data.content_type || "audio/wav");
      } else if (event === "reply") {
        finalBody = data;
      } else if (event === "error") {
        finalBody = { reply: "That didn't complete — try again." };
      }
    }
  }
  if (!finalBody) return null;
  state.caption = finalBody.reply || "";
  pushHistory("evie", finalBody.reply || "");
  setMood(state.talking ? "Listening" : "Ready");
  paintLive();
  return finalBody;
}

/* Cycle 57 — sequential playback of typed-path sentence WAVs. One shared
   element, chained ended-events; a new reply replaces any queue still
   playing. */
const typedTts = { audio: null, queue: [], playing: false };
function playTypedTts(audioB64, contentType) {
  if (!typedTts.audio) {
    typedTts.audio = new Audio();
    typedTts.audio.addEventListener("ended", () => {
      typedTts.playing = false;
      const next = typedTts.queue.shift();
      if (next) playTypedTts(next.audioB64, next.contentType);
    });
  }
  if (typedTts.playing) {
    typedTts.queue.push({ audioB64, contentType });
    return;
  }
  try {
    typedTts.audio.src = "data:" + contentType + ";base64," + audioB64;
    typedTts.playing = true;
    const done = typedTts.audio.play();
    if (done && done.catch) {
      done.catch(() => {
        typedTts.playing = false;
        const next = typedTts.queue.shift();
        if (next) playTypedTts(next.audioB64, next.contentType);
      });
    }
  } catch (_err) {
    typedTts.playing = false;
  }
}

async function captureCamera(body, facing) {
  const action = (body && (body.camera_action || body.action)) || "look_once";
  const wantsClip = isRecordAction(action);
  const generation = (state._roomCameraGeneration || 0) + 1;
  state._roomCameraGeneration = generation;
  showSheet("camera-sheet", true);
  textOf($("camera-copy"), cameraCopyFor(action));
  setMood("Camera");
  $("camera-sheet").hidden = false;
  const REASONS = {
    explicit_this_phone: "You asked this phone to look",
    preferred_camera: "Your preferred camera",
    origin_preferred: "Your preferred camera",
    only_ready_phone: "Only phone awake right now",
  };
  const why = body && body.reason ? REASONS[body.reason] || body.reason.replace(/_/g, " ") : "";
  textOf(
    $("camera-copy"),
    (action === "record_clip" ? "Recording a short clip" : "Opening perception") + (why ? " · " + why : "")
  );
  setMood(action === "record_clip" ? "Clip" : "Camera");
  const video = $("preview");
  const canvas = $("snap");
  let stream;
  let jpeg;
  let clip = null;
  let burst = [];
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing || "environment" }, width: { max: 1280 }, height: { max: 720 } },
      audio: false,
    });
    if (generation !== state._roomCameraGeneration) throw new Error("Camera cancelled.");
    video.srcObject = stream;
    video.hidden = false;
    await video.play();
    if (wantsClip) {
      _clipVideoEl = video;
      clip = await recordClipBlob(stream);
      _clipVideoEl = null;
      if (generation !== state._roomCameraGeneration) throw new Error("Camera cancelled.");
      if (clip && clip.posters.length) {
        burst = clip.posters;
        jpeg = clip.posters[Math.floor(clip.posters.length / 2)] || clip.posters[0];
      } else {
        // Honest fallback: timestamped stills, never called a video.
        burst = await captureBurst(video, canvas, generation);
        jpeg = burst.length ? burst[Math.floor(burst.length / 2)] : null;
      }
    } else {
      jpeg = await grabStill(video, canvas);
    }
  } finally {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (generation === state._roomCameraGeneration) {
      video.hidden = true;
      video.srcObject = null;
      showSheet("camera-sheet", false);
    }
  }
  const hasClip = !!(clip && clip.blob);
  if (body && body.camera_request_id) {
    if (hasClip) {
      await uploadClip({
        requestId: body.camera_request_id,
        blob: clip.blob,
        durationMs: CLIP_CAPTURE_SECONDS * 1000,
        posters: burst,
        action: action,
      });
    } else {
      const images = burst.length ? burst : [jpeg];
      const receipt = await postCameraFrames({
        requestId: body.camera_request_id,
        images: images.filter(Boolean),
        action: action,
        mediaKind: burst.length > 1 ? "burst" : "frame",
        hasClip: false,
      });
      applyCameraReceipt(receipt);
    }
    await api("/v1/device-gateway/camera/result", {
      method: "POST",
      body: JSON.stringify({ request_id: body.camera_request_id, jpeg_b64: jpeg, action: action }),
    });
    if (action !== "remember") lastLook = { jpeg };
    if ($("keep-chip")) {
      $("keep-chip").hidden = action === "remember";
      $("keep-chip").onclick = () => keepLastLook().catch(() => {});
    }
  }
  if (state.talking) setMood("Listening");
  else setMood("Ready");
  return jpeg;
}

async function captureBurst(video, canvas, generation) {
  const frames = [];
  for (let index = 0; index < BURST_FRAMES; index += 1) {
    if (generation !== state._roomCameraGeneration) break;
    frames.push(await grabStill(video, canvas));
    if (index + 1 < BURST_FRAMES) {
      await new Promise((r) => setTimeout(r, BURST_GAP_MS));
    }
  }
  return frames;
}

async function postCameraFrames({ requestId, images, action, mediaKind, hasClip }) {
  const clipSupported = videoRecordingSupported();
  const total = images.length;
  const span = mediaKind === "burst" ? CLIP_CAPTURE_SECONDS * 1000 : 0;
  for (let index = 0; index < total; index += 1) {
    const capturedAt = total > 1 ? Math.round((span * index) / (total - 1)) : 0;
    const payload = {
      request_id: requestId,
      jpeg_b64: images[index],
      action: action,
      media_kind: mediaKind,
      has_clip: !!hasClip,
      clip_supported: clipSupported,
      captured_at_ms: capturedAt,
      sequence: index,
      last: index === total - 1,
    };
    if (index === total - 1) {
      const receipt = await api("/v1/device-gateway/camera/result", {
        method: "POST",
        body: JSON.stringify(payload),
        _timeoutMs: 45000,
      });
      if (!receipt || !receipt.ok) throw new Error("Camera upload failed. Try Look again.");
      return receipt;
    }
    // Non-final frames only need to reach the server; failures are non-fatal.
    await api("/v1/device-gateway/camera/result", {
      method: "POST",
      body: JSON.stringify(payload),
      _timeoutMs: 45000,
    }).catch(() => null);
  }
  return null;
}

async function uploadClip({ requestId, blob, durationMs, posters, action }) {
  const form = new FormData();
  form.append("file", blob, "ev-clip." + (blob.type.indexOf("webm") >= 0 ? "webm" : "mp4"));
  form.append("duration_ms", String(durationMs));
  form.append("request_id", requestId);
  form.append("analyze", "true");
  const receipt = await api("/v1/vision/clip", {
    method: "POST",
    body: form,
    headers: {},
    _timeoutMs: 90000,
  });
  const spoken = (receipt && receipt.spoken) || "Clip stored.";
  state.caption = spoken;
  pushHistory("evie", spoken);
  showCameraStatus(
    receipt && receipt.frames
      ? "clip saved to memory · " + receipt.frames + " moments"
      : "clip saved to memory"
  );
  // Posters ride along as the live frame for the same request, so the pending
  // request resolves with what the camera actually saw.
  if (posters && posters.length) {
    await postCameraFrames({
      requestId: requestId,
      images: posters,
      action: action,
      mediaKind: "video",
      hasClip: true,
    }).catch(() => null);
  }
}

function applyCameraReceipt(receipt) {
  if (!receipt) return;
  const vision = receipt.vision || {};
  const description = vision.spoken || vision.description || vision.caption || vision.summary || receipt.ocr_text;
  state.caption = typeof description === "string" && description.trim()
    ? description
    : "Image received. " + (receipt.vision ? "No description was returned." : "Visual analysis is unavailable for this phone’s current access.");
  if (description) pushHistory("evie", state.caption);
  showCameraStatus(receipt.persisted_to_memory_os ? "image saved to memory" : "image received · not saved to personal memory");
}

async function waitForCameraReceipt(body) {
  const requestId = String((body && body.camera_request_id) || "").trim();
  if (!requestId) return null;
  const generation = Number(state._cameraReceiptGeneration || 0) + 1;
  state._cameraReceiptGeneration = generation;
  const current = () => generation === Number(state._cameraReceiptGeneration || 0);
  if (String((body && body.freshness) || "").toUpperCase() === "OFFLINE") {
    if (current()) showCameraStatus("preferred phone offline · look queued");
    return null;
  }
  if (current()) showCameraStatus("waiting for the preferred phone");
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    try {
      const receipt = await api("/v1/device-gateway/camera/" + encodeURIComponent(requestId));
      if (receipt && receipt.request_id === requestId && receipt.expired) {
        if (current()) showCameraStatus("camera request expired · ask me to look again");
        return receipt;
      }
      if (receipt && receipt.request_id === requestId && receipt.has_frame) {
        if (current()) showCameraStatus("frame received · Evie is analyzing");
        return receipt;
      }
    } catch (_err) {
      // The request may still be queued or the target may be offline.
    }
  }
  if (current()) showCameraStatus("still waiting · no frame received yet");
  return null;

/* Cycle 67 — "remember this": the owner keeps what Evie just looked at.
   The SAME frame re-posts with action=remember; the server marks it as an
   explicit owner keep with provenance. Optional note names the thing. */
let lastLook = null;
async function keepLastLook() {
  if (!lastLook) return;
  const note = (prompt("Name it (optional):") || "").trim();
  await api("/v1/device-gateway/camera/result", {
    method: "POST",
    body: JSON.stringify({ request_id: crypto.randomUUID(), jpeg_b64: lastLook.jpeg, action: "remember", note: note || null }),
  });
  lastLook = null;
  pushActivity("Kept to memory");
}
}

function downsample(float32, fromRate, toRate) {
  if (fromRate === toRate) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i += 1) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const ratio = fromRate / toRate;
  const length = Math.max(1, Math.round(float32.length / ratio));
  const out = new Int16Array(length);
  for (let i = 0; i < length; i += 1) {
    const s = Math.max(-1, Math.min(1, float32[Math.floor(i * ratio)] || 0));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

function playEncodedFallback(msg, gen) {
  const el = $("encoded-out");
  if (!el || gen !== state.sessionGen || !state.audioLeader) return;
  if (state.encodedUrl) URL.revokeObjectURL(state.encodedUrl);
  const bytes = b64ToBytes(msg.audio_b64);
  state.encodedUrl = URL.createObjectURL(
    new Blob([bytes], { type: msg.content_type || "audio/mpeg" })
  );
  el.srcObject = null;
  el.src = state.encodedUrl;
  const finish = () => {
    state.encodedPlaying = false;
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: "playback", active: false }));
    }
    if (state.talking && gen === state.sessionGen) setMood("Listening");
    render();
  };
  el.onended = finish;
  el.onerror = finish;
  state.encodedPlaying = true;
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "playback", active: true }));
  }
  setMood("Speaking");
  el.play().catch(() => {
    state.caption = "Voice connected — tap to enable audio";
    finish();
  });
}

async function attachCapture(ws, stream) {
  // Cycle 76 — SE performance profile: a bigger capture batch cuts
  // per-second WS frame count (CPU + radio wakeups) on SE-class phones.
  const sePerf = !!(window.EvieAudioProfile && window.EvieAudioProfile.se);
  const BATCH_S = sePerf ? 0.04 : 0.02;
  const ctx = new AudioContext({ latencyHint: sePerf ? "playback" : "interactive" });
  const current = () => state.ws === ws && state.talking;
  if (ctx.state === "suspended") await ctx.resume();
  if (!current()) { stream.getTracks().forEach(track => track.stop()); void ctx.close(); return; }
  const source = ctx.createMediaStreamSource(stream);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  const sourceRate = ctx.sampleRate;
  // Muse Voice (Meta ASR) clocks ingress at realtime. AudioWorklet posts
  // 128-sample quanta (~2.7 ms @48k); sending each as its own WS frame is
  // ~370 tiny frames/sec of jitter that Meta rejects as slower-than-realtime.
  // Accumulate to 20 ms (320 samples @16k = 640 bytes) before sending so the
  // backend forwards steady realtime frames. ScriptProcessor already emits
  // ~85 ms frames and bypasses the accumulator.
  const FRAME_SAMPLES = Math.floor(TARGET_RATE * BATCH_S);
  let pending = new Int16Array(0);
  const sendPcmBatched = (float32) => {
    if (!state.talking || ws.readyState !== WebSocket.OPEN) {
      pending = new Int16Array(0);
      return;
    }
    if (engine.halfDuplex && engine.playing) {
      pending = new Int16Array(0);
      return;
    }
    const pcm = downsample(float32, sourceRate, TARGET_RATE);
    if (pcm.length >= FRAME_SAMPLES) {
      // Large callback (ScriptProcessor): send immediately in 20 ms slices
      // so one 85 ms burst does not arrive as a single jumbo frame.
      const merged = new Int16Array(pending.length + pcm.length);
      merged.set(pending, 0);
      merged.set(pcm, pending.length);
      pending = new Int16Array(0);
      let off = 0;
      while (off + FRAME_SAMPLES <= merged.length) {
        if (!state.talking || ws.readyState !== WebSocket.OPEN) break;
        if (engine.halfDuplex && engine.playing) break;
        ws.send(merged.slice(off, off + FRAME_SAMPLES).buffer);
        off += FRAME_SAMPLES;
      }
      if (off < merged.length) {
        pending = merged.slice(off);
      }
      return;
    }
    const merged = new Int16Array(pending.length + pcm.length);
    merged.set(pending, 0);
    merged.set(pcm, pending.length);
    pending = merged;
    while (pending.length >= FRAME_SAMPLES) {
      if (!state.talking || ws.readyState !== WebSocket.OPEN) {
        pending = new Int16Array(0);
        return;
      }
      if (engine.halfDuplex && engine.playing) {
        pending = new Int16Array(0);
        return;
      }
      ws.send(pending.slice(0, FRAME_SAMPLES).buffer);
      pending = pending.slice(FRAME_SAMPLES);
    }
  };
  const sendPcm = (float32) => sendPcmBatched(float32);
  if (ctx.audioWorklet) {
    try {
      await ctx.audioWorklet.addModule("/evie/pcm-worklet.js" + ASSET_V);
      if (!current()) { source.disconnect(); stream.getTracks().forEach(track => track.stop()); void ctx.close(); return; }
      const node = new AudioWorkletNode(ctx, "pcm-capture");
      node.port.onmessage = (ev) => sendPcm(ev.data);
      source.connect(node);
      node.connect(mute);
      mute.connect(ctx.destination);
      state.capture = "audioworklet";
      state._audio = { stream, ctx, source, node, mute, kind: "worklet" };
      return;
    } catch (_err) {
      state.capture = "scriptprocessor";
    }
  }
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  proc.onaudioprocess = (ev) => sendPcm(ev.inputBuffer.getChannelData(0));
  source.connect(proc);
  proc.connect(mute);
  mute.connect(ctx.destination);
  state.capture = "scriptprocessor";
  state._audio = { stream, ctx, source, proc, mute, kind: "script" };
}

function handlePhoneHud(event) {
  if (!event || typeof event !== "object") return;
  const hud = event.hud && typeof event.hud === "object" ? event.hud : event;
  const actions = window.EvieMobileActions;
  // Both transports must deliver the same actionable card. A spoken reply
  // alone cannot expose the required confirmation/OS handoff controls.
  const action = hud.phone_action;
  if (actions && action && typeof action === "object" && (action.card || action.action_id)) {
    actions.present(action);
    render();
    return;
  }
  if (actions && actions.presentFromHud(event)) { render(); return; }
  if (showHomeStationResult(hud)) { render(); return; }
  if (hud.kind === "progress" || event.kind === "progress") {
    setMood("Working");
    textOf($("action-card"), hud.title || hud.name || "Working…");
    $("action-card").hidden = false;
    render();
  }
}

async function handleLiveMessage(gen, ev) {
  if (gen !== state.sessionGen) return;
  if (typeof ev.data !== "string") return;
  const msg = JSON.parse(ev.data);
  if (msg.type === "ready") setMood("Listening");
  if (msg.type === "final_transcript" && msg.text) {
    state.userLine = msg.text;
    pushHistory("user", msg.text);
    const line = $("user-line");
    if (line) {
      line.classList.remove("partial");
      line.classList.add("final");
    }
    if (window.EvieMobileActions?.onTranscript) window.EvieMobileActions.onTranscript(msg.text);
    setMood("Thinking");
    render();
  }
  if (msg.type === "partial" && msg.text) {
    state.userLine = msg.text;
    textOf($("user-line"), msg.text);
    const line = $("user-line");
    if (line) {
      line.classList.remove("final");
      line.classList.add("partial");
    }
  }
  if (msg.type === "barge_in" && engine) engine.stop();
  if (msg.type === "hud") {
    handlePhoneHud(msg);
    const hud = msg.hud || msg;
    const kind = hud.kind || msg.kind || (hud.meta && hud.meta.kind) || "";
    if (kind === "progress") {
      setMood("Working on MacBook");
      textOf($("action-card"), "MacBook · working");
      $("action-card").hidden = false;
      pushActivity("Working on MacBook");
    } else if ((kind === "result" || kind === "tool_result") && window.EvieMobileActions) {
      // Cycle 45 — provenance chips ride the same card renderer on PCM path.
      window.EvieMobileActions.presentFromHud(hud);
    }
  }
  if (msg.type === "reply" && msg.text) {
    state.caption = msg.text;
    pushHistory("evie", msg.text);
    if (engine && engine.endStream) engine.endStream();
    const userLineEl = $("user-line");
    if (userLineEl) {
      userLineEl.classList.remove("partial", "final");
      state.userLine = "";
    }
    render();
  }
  if (msg.type === "tts_chunk" && msg.audio_b64) {
    if (state.activeBackend === "webrtc" || state.activeBackend === "webrtc_strict") return;
    if (!state.audioLeader || !engine) return;
    const contentType = msg.content_type || "audio/pcm";
    if (contentType.indexOf("mpeg") >= 0 || contentType.indexOf("mp3") >= 0) {
      playEncodedFallback(msg, gen);
      return;
    }
    if (msg.index === 0) {
      // A zero index opens a provider response, but NOT always a new audible
      // turn: tool continuations restart the chunk counter while preamble
      // audio is still draining. Flushing there drops the queued tail
      // mid-word on every tool call — adopt appends to the one stream.
      const turn = {
        socketGeneration: gen,
        responseId: msg.response_id || msg.responseId || null,
      };
      if (engine.playing && engine.adoptTurn) engine.adoptTurn(turn);
      else engine.beginTurn(turn);
    }
    await engine.enqueuePcm16({
      bytes: b64ToBytes(msg.audio_b64),
      seq: msg.index,
      socketGeneration: gen,
      sampleRate: msg.sample_rate || TARGET_RATE,
      responseId: msg.response_id || msg.responseId || null,
    });
    if (engine.playing) setMood("Speaking");
    return;
  }
  if (msg.audio_b64 && msg.type !== "tts_chunk") return;
  if (msg.type === "camera_request") {
    await handleCameraRequest(msg);
  }
  if (msg.type === "conversation_moved") loseAudio("conversation_moved");
  if (msg.type === "error" && !msg.fatal) {
    // Voice pipeline failures were previously silent: Thinking flashed then
    // Listening returned with no caption. Surface Muse ASR/TTS/pipeline
    // errors as the reply line so a quiet mic or rejected format is visible.
    const code = String(msg.code || "");
    const text = String(msg.text || msg.message || "").trim();
    if (code.indexOf("asr") === 0 || code === "voice_pipeline" || code === "control_rejected" || code === "asr_unavailable") {
      if (text) {
        state.caption = text;
        pushHistory("evie", text);
      } else if (code === "asr_no_speech") {
        state.caption = "I didn't catch that — say it again.";
      }
      if (state.talking) setMood("Listening");
      render();
    }
    return;
  }
  if (msg.type === "error" && msg.fatal) await stopTalk();
}

async function handleCameraRequest(msg) {
  try {
    const jpeg = await captureCamera(msg);
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({
        type: "look_frame",
        request_id: msg.request_id,
        jpeg_b64: jpeg,
        last: true,
      }));
    } else if (state.sessionId) {
      const live = state.webrtc && state.webrtc._liveBody
        ? state.webrtc._liveBody({ request_id: msg.request_id, jpeg_b64: jpeg, last: true, action: msg.action || "look_once" })
        : {
            session_id: state.sessionId,
            instance_id: state.instanceId,
            lease_id: state.leaseId,
            request_id: msg.request_id,
            jpeg_b64: jpeg,
            last: true,
            action: msg.action || "look_once",
          };
      const body = await api("/v1/device-gateway/live/look-frame", {
        method: "POST",
        body: JSON.stringify(live),
      });
      // Cycle 44 — close the look loop: tell the owner what the look did.
      if (body && body.vision && body.vision.ok) {
        const kept = body.persisted_to_memory_os || body.vision.persisted_to_memory_os;
        const seen = String(
          body.vision.spoken
            || (body.vision.labels && body.vision.labels.slice(0, 4).join(", "))
            || (body.vision.ocr_text ? body.vision.ocr_text.slice(0, 120) : "")
        ).trim();
        state.caption = kept
          ? "Seen" + (seen ? " — " + seen.slice(0, 140) : "") + ". Kept to memory."
          : seen
            ? "Seen — " + seen.slice(0, 140)
            : "Seen.";
        render();
      }
    }
  } catch (_err) {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({
        type: "look_frame",
        request_id: msg.request_id,
        error: "permission_denied",
        permission: "denied",
        last: true,
      }));
    }
    state.caption = "I don't have camera access on this phone.";
    render();
  }
}

/* Cycle 80 — Wake Lock ambient mode: the screen stays on while the live
   session is active (a hands-free conversation dies when the phone sleeps).
   Released on stop or when the owner backgrounds the page; reacquired on
   return while still talking. Unsupported browsers: honest no-op. */
let wakeLockHandle = null;
async function acquireWakeLock() {
  if (!navigator.wakeLock || wakeLockHandle) return;
  try {
    wakeLockHandle = await navigator.wakeLock.request("screen");
    wakeLockHandle.addEventListener("release", () => { wakeLockHandle = null; });
    pushActivity("Screen staying on");
  } catch (_err) {
    wakeLockHandle = null;
  }
}
async function releaseWakeLock() {
  if (!wakeLockHandle) return;
  try { await wakeLockHandle.release(); } catch (_err) {}
  wakeLockHandle = null;
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.talking) acquireWakeLock().catch(() => {});
  if (document.visibilityState === "hidden") releaseWakeLock().catch(() => {});
});

async function talk() {
  if (state._talkInflight) return stopTalk();
  if (state.talking) {
    if (state.webrtc && state.webrtc.playBlocked) {
      try {
        await state.webrtc.enableAudio();
        state.caption = "";
        setConn("ACTIVE");
        setMood("Listening");
      } catch (err) {
        state.caption = "Voice connected — tap to enable audio";
      }
      render();
      return;
    }
    await stopTalk();
    return;
  }
  const attempt = (state._voiceAttempt || 0) + 1;
  state._voiceAttempt = attempt;
  const current = () => state._voiceAttempt === attempt;
  const controller = new AbortController();
  state._voiceAbort = controller;
  state._talkInflight = true;
  // Prime playback in the original tap, before any network awaits. Actual
  // remote playback remains independently checked by EvieWebRTC.
  const output = $("webrtc-out");
  if (output) { try { const play = output.play(); if (play) play.catch(() => {}); } catch (_err) {} }
  markTtfaStart();
  render();
  try {
    if (state._voiceCleanup) await state._voiceCleanup;
    if (!current()) return;
    if (window.EvieFeedback) window.EvieFeedback.emit("conversationStart", $("talk"));
    claimAudioLeader();
    setConn("ACTIVE");
    setMood("Connecting microphone…");
    $("talk").textContent = voiceMode() === "ptt" ? "Hold" : "Stop";
    const opened = await api("/v1/device-gateway/live/open", {
      method: "POST",
      signal: controller.signal,
      body: JSON.stringify({
        instance_id: state.instanceId,
        method: "manual",
        media_backend: "webrtc_strict",
        client_generation: (state.sessionGen || 0) + 1,
        // Cycle 79 — a wake tap (or the second tap after a refusal) is
        // explicit takeover intent.
        takeover: !!(state._wakeTakeover || state._takeoverArmed),
      }),
    });
    if (!current()) return;
    state._takeoverArmed = false;
    if (opened.ok === false && opened.refused === "lease_active") {
      state._takeoverArmed = true;
      state.caption = opened.spoken || "Evie is talking on another device — tap again to take over.";
      setMood("Busy elsewhere");
      render();
      return;
    }
    state.sessionId = opened.session_id;
    acquireWakeLock().catch(() => {});
    if (window.EvieMobileActions) window.EvieMobileActions.setSession(opened.session_id);
    state.leaseId = opened.lease_id || (opened.lease && opened.lease.lease_id);
    const want = opened.media_backend || "webrtc_strict";
    const strict = opened.strict_webrtc === true || want === "webrtc_strict" || !opened.ws_ticket;
    if ((want === "webrtc" || want === "webrtc_strict") && window.RTCPeerConnection && window.EvieWebRTC) {
      try {
        setMood("Connecting voice…");
        await startWebRTC(opened, attempt);
        return;
      } catch (err) {
        if (!current()) return;
        const diag = (err && err.diag) || (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot());
        state.connectionDiag = diag || {
          failed_stage: err && err.failed_stage,
          error_message: String(err && err.message || err),
          http_status: err && (err.provider_status || err.status),
        };
        if (err && err.audio_blocked) {
          state.talking = true;
          setConn("ACTIVE");
          state.caption = "Voice connected — tap to enable audio";
          setMood("Voice connected — tap to enable audio");
          render();
          return;
        }
        if (state.webrtc) {
          state.webrtc.stop();
          state.webrtc = null;
        }
        const stage = (diag && diag.failed_stage) || (err && err.failed_stage) || "";
        const msgLower = String(err && err.message || "").toLowerCase();
        if (stage === "M02") state.caption = "Microphone access denied.";
        else if (stage === "M14" || stage === "M15") state.caption = "Network connection failed.";
        else if (msgLower.includes("mic") && msgLower.includes("ended")) state.caption = "Microphone ended.";
        else if (msgLower.includes("auth") || msgLower.includes("revoked") || String(diag && diag.error_message || "").toLowerCase().includes("revoked")) state.caption = "Session expired — reconnecting.";
        else if (stage === "M09" || stage === "M10") state.caption = "Couldn't connect to Evie Voice.";
        else state.caption = "Couldn't connect to Evie Voice.";
        setMood(stage === "M14" || stage === "M15" ? "Reconnecting" : "Voice unavailable");
        await stopTalk();
        return;
      }
    }
    if (strict) {
      state.caption = "Couldn't connect to Evie Voice.";
      setMood("Voice unavailable");
      await stopTalk();
      return;
    }
    await startPcm(opened, attempt);
  } catch (err) {
    if (!current()) return;
    state.caption = String(err.message || err);
    await stopTalk();
    setMood("Voice unavailable");
  } finally {
    if (current()) { state._talkInflight = false; state._voiceAbort = null; }
    render();
  }
}

function scheduleVoiceRecovery(attempt, delay) {
  if (state._recoverInflight || state._talkInflight) return;
  state._recoverInflight = true;
  setMood("Reconnecting");
  setConn("RECONNECTING");
  state._voiceRecoveryTimer = setTimeout(async () => {
    state._voiceRecoveryTimer = null;
    if (state._voiceAttempt !== attempt || !state.talking) return;
    const cleanup = stopTalk({ preserveLease: true });
    const stoppedAttempt = state._voiceAttempt;
    await cleanup;
    if (state._voiceAttempt === stoppedAttempt) await talk();
  }, delay);
}

async function startWebRTC(opened, attempt) {
  closeActiveBackend();
  const encoded = $("encoded-out");
  if (encoded) {
    encoded.pause();
    encoded.removeAttribute("src");
    encoded.srcObject = null;
  }
  const local = $("mic-check-out");
  if (local) {
    local.pause();
    local.removeAttribute("src");
  }
  state.activeBackend = "webrtc_strict";
  const rtc = new window.EvieWebRTC({
    api: api,
    instanceId: state.instanceId,
    leaseId: state.leaseId || opened.lease_id,
    miniThinks: !((state.hello && state.hello.cognitive && state.hello.cognitive.muse_kernel)),
    audioEl: $("webrtc-out"),
    onState: (label) => {
      if (state._voiceAttempt !== attempt) return;
      if (label === "listening") setMood("Listening");
      if (label === "thinking") setMood("Thinking");
      if (label === "speaking") setMood("Speaking");
      if (label === "moved") loseAudio("conversation_moved");
      if (label === "mic_ended") {
        // Bounded mic reacquisition: if iOS policy allows, one auto-recover
        // without extra Talk press; otherwise surface truthful gesture need.
        if (state.talking && !state._recoverInflight) {
          scheduleVoiceRecovery(attempt, 600);
          return;
        }
        setMood("Voice unavailable");
        state.caption = "Microphone ended.";
      }
      if (label === "audio_blocked") {
        setMood("Voice connected — tap to enable audio");
        state.caption = "Voice connected — tap to enable audio";
      }
      if (label === "failed") {
        // Automatic bounded recovery for established READY session that
        // dropped. Single generation, no Talk press required unless iOS
        // demands a new gesture (handled as mic_ended above).
        if (state.talking && !state._recoverInflight && !state._talkInflight) {
          scheduleVoiceRecovery(attempt, 800);
        }
      }
    },
    onTranscript: (text, meta) => {
      if (state._voiceAttempt !== attempt) return;
      state.lastAsr = text;
      state.lastAsrConfidence = meta && meta.confidence;
      state.userLine = text;
      pushHistory("user", text);
      if (window.EvieMobileActions && window.EvieMobileActions.onTranscript) {
        window.EvieMobileActions.onTranscript(text);
      }
      paintLive();
    },
    onCaption: (text, done) => {
      if (state._voiceAttempt !== attempt) return;
      if (!done && !state._captionStreaming) {
        state.caption = "";
        state._captionStreaming = true;
      }
      state.caption = done ? text : (state.caption + text);
      if (done) {
        state._captionStreaming = false;
        pushHistory("evie", state.caption);
      }
      paintLive();
    },
    onEnvelope: (amp) => {
      if (state.orb) state.orb.setAmp(amp);
    },
    onCamera: (ev) => handleCameraRequest(ev),
    onHud: (hud) => {
      if (state._voiceAttempt === attempt) handlePhoneHud(hud);
    },
    onHealth: (snap) => {
      state.voiceHealth = snap;
      state.talkPhase = String((snap && snap.runtime) || state.talkPhase || "IDLE");
      if (snap && snap.connection) state.connectionDiag = snap.connection;
      scheduleHealthRender();
    },
  });
  state.webrtc = rtc;
  if (voiceMode() === "ptt") rtc.setPtt(true);
  state.talking = true;
  const signaling = /voice_signaling=ephemeral/.test(location.search) ? "ephemeral_direct" : "unified_calls";
  const mic = await rtc.start(opened, { signaling: signaling });
  if (state._voiceAttempt !== attempt || state.webrtc !== rtc) { rtc.stop(); return; }
  state.connectionDiag = rtc.diag ? rtc.diag.snapshot() : null;
  state.capture = "webrtc_native_track";
  state.captureSettings = (mic.settings && mic.settings.actual) || mic.settings || {};
  setMood("Listening");
  setConn("ACTIVE");
}

async function startPcm(opened, attempt) {
  closeActiveBackend();
  if (!opened.ws_ticket) throw new Error("Home Station did not mint a live ticket.");
  state.activeBackend = "pcm_ws";
  const playback = pcmEngine();
  try {
    await playback.ensure();
  } catch (_err) {
    setMood("Tap to enable voice");
    throw _err;
  }
  if (state._voiceAttempt !== attempt) return;
  playback.flushReconnect();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl =
    proto +
    "//" +
    location.host +
    "/v1/voice/live?session_id=" +
    encodeURIComponent(opened.session_id) +
    "&ticket=" +
    encodeURIComponent(opened.ws_ticket);
  const gen = (state.sessionGen += 1);
  playback.socketGeneration = gen;
  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  state.talking = true;
  let queue = Promise.resolve();
  ws.onmessage = (ev) => {
    queue = queue.then(() => { if (gen === state.sessionGen) return handleLiveMessage(gen, ev); }).catch(err => {
      if (gen === state.sessionGen) { state.caption = String(err.message || err); render(); }
    });
  };
  ws.onclose = () => {
    if (state.talking && gen === state.sessionGen) {
      state.caption = "Voice connection closed. Tap Talk to reconnect.";
      void stopTalk();
    }
  };
  playback.onPlayingChange = (active) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "playback", active: !!active }));
    }
    if (active) setMood("Speaking");
    else if (state.talking) setMood("Listening");
    render();
  };
  playback.onEnvelope = (amp) => {
    if (state.orb) state.orb.setAmp(amp);
  };
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });
  if (state._voiceAttempt !== attempt || state.ws !== ws || ws.readyState === WebSocket.CLOSED) {
    stream.getTracks().forEach(track => track.stop());
    return;
  }
  state._pcmMic = stream;
  const track = stream.getAudioTracks()[0];
  state.captureSettings = track && track.getSettings ? track.getSettings() : {};
  await attachCapture(ws, stream);
  if (state._voiceAttempt !== attempt) return;
  setMood("Listening");
  setConn("ACTIVE");
}

function closeActiveBackend() {
  if (state.webrtc) {
    state.webrtc.stop();
    state.webrtc = null;
  }
  if (state.ws) { state.ws.onclose = null; state.ws.onmessage = null; state.ws.close(); }
  state.ws = null;
  const encoded = $("encoded-out");
  if (encoded) {
    encoded.pause();
    encoded.removeAttribute("src");
    encoded.srcObject = null;
    encoded.onended = null;
    encoded.onerror = null;
  }
  if (state.encodedUrl) URL.revokeObjectURL(state.encodedUrl);
  state.encodedUrl = null;
  state.encodedPlaying = false;
  if (engine) engine.stop();
  state.activeBackend = "none";
}

async function stopTalk(options) {
  const preserveLease = !!(options && options.preserveLease);
  releaseWakeLock().catch(() => {});
  if (window.EvieFeedback) window.EvieFeedback.emit("conversationStop", $("talk"));
  state._voiceAttempt = (state._voiceAttempt || 0) + 1;
  if (state._voiceAbort) state._voiceAbort.abort();
  state._voiceAbort = null;
  clearTimeout(state._voiceRecoveryTimer);
  state._voiceRecoveryTimer = null;
  state._recoverInflight = false;
  state._talkInflight = false;
  state.sessionGen += 1;
  if (engine) engine.socketGeneration = state.sessionGen;
  if (engine) engine.flushReconnect();
  state.talking = false;
  state.audioLeader = false;
  const sessionId = state.sessionId;
  const instanceId = state.instanceId;
  state.sessionId = null;
  state.leaseId = null;
  if (window.EvieMobileActions) window.EvieMobileActions.setSession(null);
  closeActiveBackend();
  if (state._pcmMic) { state._pcmMic.getTracks().forEach(track => track.stop()); state._pcmMic = null; }
  if (state._audio) {
    if (state._audio.node) state._audio.node.disconnect();
    if (state._audio.proc) state._audio.proc.disconnect();
    if (state._audio.mute) state._audio.mute.disconnect();
    state._audio.source.disconnect();
    state._audio.stream.getTracks().forEach((t) => t.stop());
    void state._audio.ctx.close().catch(() => {});
    state._audio = null;
  }
  $("action-card").hidden = true;
  if (state.conn === "ACTIVE" || state.conn === "RECONNECTING") {
    setMood("Ready");
    setConn("READY");
  }
  paintLive();
  // Local media/UI stops synchronously. Serialize remote cleanup before a
  // subsequent open, since the server lease is scoped to this instance.
  const previous = state._voiceCleanup;
  const cleanup = (async () => {
    if (previous) await previous;
    await api("/v1/device-gateway/live/close", {
    method: "POST",
    _timeoutMs: 5000,
    body: JSON.stringify({
      instance_id: instanceId,
      session_id: sessionId,
      preserve_lease: preserveLease,
    }),
  }).catch(() => {});
  if (!preserveLease) {
    await api("/v1/device-gateway/conversation/release", {
      method: "POST",
      _timeoutMs: 5000,
      body: JSON.stringify({ instance_id: instanceId }),
    }).catch(() => {});
  }
  })();
  state._voiceCleanup = cleanup;
  await cleanup;
  if (state._voiceCleanup === cleanup) state._voiceCleanup = null;
}

function scheduleReconnect() {
  if (state.reconnectTimer) return;
  setConn("RECONNECTING");
  setMood("Reconnecting");
  const delay = Math.min(15000, 600 * Math.pow(1.7, state.reconnects));
  state.reconnects += 1;
  state.reconnectTimer = setTimeout(async () => {
    state.reconnectTimer = null;
    try {
      await hello();
      state.reconnects = 0;
    } catch (err) {
      state.caption = err.status === 401
        ? "This device is not paired or was revoked."
        : "Home Station is offline.";
      setMood("Home Station is offline.");
      scheduleReconnect();
    }
  }, delay);
}

function addTestRow(list, label, ok, detail) {
  const item = document.createElement("li");
  item.textContent = (ok ? "✓  " : "✕  ") + label + (detail ? " — " + detail : "");
  list.appendChild(item);
}

async function runSelfTest() {
  const list = $("self-test");
  list.hidden = false;
  while (list.firstChild) list.removeChild(list.firstChild);
  const checks = [];
  checks.push(["HTTPS", window.isSecureContext, ""]);
  checks.push(["WebRTC", typeof RTCPeerConnection === "function", ""]);
  try {
    const health = await api("/v1/device-gateway/health");
    checks.push(["Mac Home Station", health.device_gateway_ready === true, ""]);
    checks.push(["Sandbox memory", health.production_memory_enabled === false, ""]);
  } catch (_err) {
    checks.push(["Mac Home Station", false, "unreachable"]);
  }
  try {
    const stream = await (window.EvieMobileVoice
      ? window.EvieMobileVoice.acquireProductionMic()
      : navigator.mediaDevices.getUserMedia({ audio: true }));
    const track = stream.getAudioTracks()[0];
    const inspect = window.EvieMobileVoice ? window.EvieMobileVoice.inspectTrack(track) : {};
    state.forensic.mic = inspect;
    stream.getTracks().forEach((t) => t.stop());
    checks.push(["Microphone", true, inspect.actual ? JSON.stringify(inspect.actual) : ""]);
  } catch (_err) {
    checks.push(["Microphone", false, "denied"]);
  }
  checks.push(["Voice output element", !!$("webrtc-out"), "one <audio>"]);
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    stream.getTracks().forEach((t) => t.stop());
    checks.push(["Camera", true, ""]);
  } catch (_err) {
    checks.push(["Camera", false, "denied"]);
  }
  state.preflight = Object.fromEntries(checks.map((row) => [row[0], row[1] ? "PASS" : "FAIL"]));
  checks.forEach((row) => addTestRow(list, row[0], row[1], row[2]));
  render();
}

async function micCheck() {
  const mv = window.EvieMobileVoice;
  if (!mv) throw new Error("Mobile voice runtime missing.");
  state.caption = "Mic Check: say “Turn off the Wi-Fi after I finish this sentence.”";
  render();
  const rec = (state.webrtc && state.webrtc.mic)
    ? await mv.recordStream(state.webrtc.mic, 8, { clone: true, live: true })
    : await mv.recordProductionMic(8);
  state.forensic.mic = rec.inspect;
  state.captureSettings = rec.inspect.actual || {};
  const url = URL.createObjectURL(rec.blob);
  const el = $("mic-check-out");
  el.src = url;
  await el.play();
  state.forensic.localRecording = { mime: rec.mime, bytes: rec.blob.size, settings: rec.inspect };
  state.caption = "Local recording playing. Was it clear?";
  render();
  return rec;
}

async function asrCheck() {
  const rec = await micCheck();
  const b64 = await window.EvieMobileVoice.blobToBase64(rec.blob);
  const result = await api("/v1/device-gateway/mobile-voice/asr-oracle", {
    method: "POST",
    body: JSON.stringify({
      audio_b64: b64,
      mime: rec.mime,
      phrase_hint: "Turn off the Wi-Fi after I finish this sentence.",
    }),
  });
  state.lastIndependentAsr = result.transcript || "";
  state.forensic.independentAsr = result;
  state.userLine = "INDEPENDENT ASR · " + (result.transcript || "");
  state.caption = "This is independent ASR, not what Realtime heard.";
  render();
}

async function understandingCheck() {
  if (state.webrtc && !state.webrtc.closed && state.webrtc.dc?.readyState === "open") {
    state.webrtc.perceptionProbe();
    state.caption = "Understanding Check: text-only probe submitted. Waiting for a response.";
    render();
    return;
  }
  state.caption = "Start Talk first, then tap Understanding Check.";
  render();
}

async function outputCheck() {
  if (state.talking) {
    state.caption = "Stop Talk first. Output Check uses the same speaker element.";
    render();
    return;
  }
  const el = $("webrtc-out");
  el.pause();
  el.srcObject = null;
  el.src = "/evie/diag-speech.wav" + ASSET_V;
  await el.play();
  state.forensic.outputCheck = { element: "webrtc-out", pcm: "off", tts: "off" };
  state.caption = "Voice Output Check: one local sentence on the live speaker element.";
  render();
}

async function reportMisheard() {
  const intended = $("misheard-intended").value.trim();
  if (!intended) throw new Error("Enter what you actually said before sending the report.");
  await api("/v1/device-gateway/mobile-voice/misheard", {
    method: "POST",
    body: JSON.stringify({
      intended: intended,
      asr_transcript: state.lastAsr,
      independent_asr: state.lastIndependentAsr,
      model_caption: state.caption,
      confidence: state.lastAsrConfidence,
      runtime: state.voiceHealth && state.voiceHealth.runtime,
      stats: (state.voiceHealth && state.voiceHealth.stats) || {},
    }),
  });
  state.caption = "Misheard report saved (no audio stored).";
  render();
}

async function runAudioDiagnostic() {
  if (state.talking) {
    state.caption = "Stop Talk before the diagnostic. Strict WebRTC cannot share the speaker.";
    render();
    return;
  }
  const report = { d0: "skip", d1: "skip", d2: "skip", d4: "skip", d5: "skip", mode: "webrtc_strict" };
  try {
    await outputCheck();
    report.d4 = "PASS";
  } catch (_err) {
    report.d4 = "FAIL";
  }
  report.d5 = typeof RTCPeerConnection === "function" ? "READY" : "UNAVAILABLE";
  report.engine = "not_started_in_strict_mode";
  state.preflight.audio_diag = report;
  state.caption = "Speaker playback: " + report.d4 + ". Browser WebRTC support: " + report.d5 + ". This is not an end-to-end voice test.";
  render();
}

async function reportGlitch() {
  const mark = state.webrtc && state.webrtc.markGlitch ? state.webrtc.markGlitch() : { at: Date.now() };
  const inbound = mark.stats && mark.stats.inbound ? mark.stats.inbound : {};
  const incident = {
    at: mark.at,
    backend: state.activeBackend,
    runtime: mark.runtime || (state.voiceHealth && state.voiceHealth.runtime),
    packets_lost: inbound.packetsLost,
    jitter: inbound.jitter,
    concealed_samples: inbound.concealedSamples,
  };
  state.incidents.push(incident);
  await api("/v1/device-gateway/audio-diag/incident", {
    method: "POST",
    body: JSON.stringify(incident),
  });
  state.caption = "Audio issue report saved.";
  render();
}

async function resetLocal(unpair) {
  if (resetLocal._pending) return;
  resetLocal._pending = true;
  const warnings = [];
  try {
    cleanupEvieVoiceNote({ discard: !!unpair });
    submitCapture._pending = null;
    submitCapture._draft = null;
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
    clearInterval(state.helloRecheckTimer);
    state.helloRecheckTimer = 0;
    if (state.talking || state._talkInflight || state._recoverInflight || state.ws || state.webrtc) {
      try { await stopTalk(); } catch (_err) { warnings.push("Voice cleanup could not complete."); }
    }
    state.instanceId = crypto.randomUUID();
    try { sessionStorage.setItem("evie_instance", state.instanceId); }
    catch (_err) { warnings.push("Session storage could not be updated."); }
    if (unpair) {
      state.deviceToken = null;
      state.accessToken = null;
      state.device = null;
      state.hello = null;
      state.status = null;
      state.history = [];
      state.activity = [];
      state.inbox = [];
      state.queue = [];
      state.syncCursor = null;
      state.drainedCaptures = {};
      state.userLine = "";
      state.caption = "";
      state.lastAsr = "";
      state.lastAsrConfidence = null;
      state.lastIndependentAsr = "";
      state.forensic = {};
      state.preflight = {};
      state.incidents = [];
      state.voiceHealth = null;
      state.connectionDiag = null;
      state.captureSettings = {};
      state.capture = "none";
      state._lastHomeStationResult = null;
      state._lastCameraStatus = null;
      state._roomSetAside = false;
      peopleCache = [];
      routineTimes = [];
      const detail = $("memory-detail");
      if (detail) { detail._evieMemoryRequest = null; detail.hidden = true; }
      document.querySelectorAll(".sheet").forEach(sheet => { sheet.hidden = true; });
      ["history", "activity", "inbox-list", "queue-list", "memory-list", "memory-detail-text", "memory-detail-meta", "memory-versions", "memory-sources", "people-list", "looks-list", "search-memories", "search-events", "search-reminders", "search-contacts", "today-calendar", "today-reminders", "today-memories", "today-health", "health-chips", "health-series", "routines-times", "capture-meta", "conv-export-meta", "inbox-secondary-status", "queue-meta", "memory-meta", "user-line", "reply", "diag"].forEach(id => textOf($(id), ""));
      ["capture-text", "text", "pair-token", "memory-q", "search-q"].forEach(id => { if ($(id)) $(id).value = ""; });
      ["mobile-action-card", "action-card", "room-exchange", "today-hud", "camera-ask"].forEach(id => { if ($(id)) $(id).hidden = true; });
      for (const key of ["device_token", "access_token"]) {
        try { await idbDel(key); }
        catch (_err) { warnings.push("Could not remove persisted " + key + "; clear this site's data before sharing this phone."); }
      }
      for (const key of [OFFLINE_QUEUE_KEYS, "evie_trust_seen", "evie_camera_role"]) {
        try { localStorage.removeItem(key); }
        catch (_err) { warnings.push("Could not clear " + key + "."); }
      }
      state.cameraRole = "unknown";
    }
    if (window.caches) {
      try {
        const keys = await caches.keys();
        await Promise.all(keys.filter(key => key.indexOf("evie-static-") === 0).map(key => caches.delete(key)));
      } catch (_err) { warnings.push("Cached assets could not be cleared."); }
    }
    state.surface = "presence";
    if (state.deviceToken) {
      state.hello = null;
      state.status = null;
      try { await hello(); }
      catch (err) { setConn("DISCONNECTED"); warnings.push("Reconnect failed: " + String(err.message || err)); }
    } else {
      setMood("Pair this iPhone");
      setConn("DISCONNECTED");
    }
    state.caption = (unpair
      ? "Forgot this phone locally. Server trust was not revoked. Pair again to reconnect."
      : "Local client reset. Pairing kept; connection settings requested again.") + (warnings.length ? " " + warnings.join(" ") : "");
    render();
  } finally {
    resetLocal._pending = false;
    if (state.deviceToken) scheduleHelloRecheck();
  }
}

async function runControl(id, work) {
  const button = $(id);
  if (button?.getAttribute("aria-busy") === "true") return;
  button?.setAttribute("aria-busy", "true");
  try { return await work(); }
  catch (err) { state.caption = String(err.message || err); render(); }
  finally { button?.removeAttribute("aria-busy"); }
}

async function boot() {
  await ensureAudioModules();
  arrangeRoomTools();
  try {
    const storedRole = localStorage.getItem("evie_camera_role");
    if (storedRole === "pro" || storedRole === "standard" || storedRole === "unknown") {
      state.cameraRole = storedRole;
    }
  } catch (_err) {}
  restoreOfflineQueue();
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const Presence = window.EviePresence || window.EvieOrb;
  state.orb = new Presence($("orb"));
  state.orb.setReduced(reduce);
  state.orb.start();
  window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", event => state.orb.setReduced(event.matches));
  window.addEventListener("resize", () => { if (!state.orb.raf) state.orb.draw(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncRoomTheme);
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker
      .register("/evie/sw.js", { scope: "/evie/" })
      .then((reg) => watchServiceWorkerUpdate(reg))
      .catch(() => {});
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!state.updateAvailable) return;
      if (!oneShot("evie_sw_controller")) return;
      location.reload();
    });
  }
  scheduleHelloRecheck();
  function pairErrorCopy(err) {
    const body = err && err.body;
    const detail = body && body.detail;
    if (typeof detail === "string") return detail;
    const code = body && body.error_code;
    if (code === "pair_rate_limited") return "Too many attempts from this network — wait a few minutes.";
    if (err && err.status === 401) return "That code is invalid or expired. Ask the Mac for a fresh one.";
    if (err && err.status === 403) return "Pairing is blocked from this origin — open Evie over Tailscale.";
    return String(err.message || err);
  }
  $("pair-btn").addEventListener("click", () => {
    const errorEl = $("pair-error");
    if (errorEl) errorEl.hidden = true;
    pair().catch((err) => {
      const copy = pairErrorCopy(err);
      if (errorEl) {
        errorEl.textContent = copy;
        errorEl.hidden = false;
      }
      state.caption = copy;
      setConn("DISCONNECTED");
    });
  });
  $("text-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = $("text").value.trim();
    if (!text) return;
    state._roomSetAside = false;
    $("text").value = "";
    sendText(text).catch((err) => {
      if (!$("text").value) $("text").value = text;
      state.caption = String(err.message || err);
      $("room-exchange").open = true;
      render();
    });
  });
  $("talk").addEventListener("click", () => {
    state._roomSetAside = false;
    if (window.EvieFeedback) window.EvieFeedback.visualPress($("talk"));
    const action = state.talking || state._talkInflight ? stopTalk() : talk();
    action.catch((err) => {
      state.caption = String(err.message || err);
      render();
      stopTalk();
    });
  });
  $("look-btn").addEventListener("click", () => {
    openSurface("home");
    sendText("Look at this.").catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("type-btn").addEventListener("click", () => {
    const visible = $("text-form").hidden;
    $("text-form").hidden = !visible;
    $("ready-ui").dataset.writing = String(visible);
    $("type-btn").setAttribute("aria-expanded", String(visible));
    if (visible) $("text").focus();
  });
  $("room-write-close").addEventListener("click", () => {
    $("text-form").hidden = true;
    $("ready-ui").dataset.writing = "false";
    $("type-btn").setAttribute("aria-expanded", "false");
    $("type-btn").focus();
  });
  $("room-exchange").addEventListener("toggle", () => {
    state._roomSetAside = !$("room-exchange").open;
    $("ready-ui").dataset.exchange = $("room-exchange").open ? "open" : "closed";
    textOf($("room-exchange").querySelector(".room-fold-hint"), $("room-exchange").open ? "Set aside" : "Read");
  });
  $("room-enable-audio").addEventListener("click", () => {
    if (state.webrtc && state.webrtc.playBlocked) talk().catch(err => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("room-stop-session").addEventListener("click", () => stopTalk().catch(err => {
    state.caption = String(err.message || err);
    render();
  }));
  $("room-tool-search").addEventListener("input", () => {
    const query = $("room-tool-search").value.trim().toLocaleLowerCase();
    let matches = 0;
    $("room-tool-list").querySelectorAll("button").forEach(button => {
      button.hidden = !button.textContent.toLocaleLowerCase().includes(query);
      if (!button.hidden) matches += 1;
    });
    $("room-tool-empty").hidden = matches > 0;
    $("room-tool-list").querySelectorAll(".room-tool-group").forEach(group => {
      group.hidden = !group.querySelector("button:not([hidden])");
    });
  });
  document.querySelectorAll("[data-chip]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.getAttribute("data-chip");
      if (kind === "note" || kind === "memory") {
        $("text-form").hidden = true;
        $("ready-ui").dataset.writing = "false";
        openSurface(kind === "note" ? "capture" : "memory");
        return;
      }
      if (kind === "look") {
        $("text-form").hidden = true;
        $("ready-ui").dataset.writing = "false";
        openSurface("home");
        sendText("Look at this.").catch((err) => {
          state.caption = String(err.message || err);
          paintLive();
        });
        return;
      }
      const prompts = {
        remind: "Remind me in 30 minutes",
        timer: "Start a 10 minute timer",
        day: "What's today looking like",
        weather: "What's the weather",
        messages: "what message do I have last",
        mail: "what mail do I have last",
      };
      const prompt = prompts[kind];
      if (!prompt) return;
      $("text-form").hidden = true;
      $("ready-ui").dataset.writing = "false";
      sendText(prompt).catch((err) => {
        state.caption = String(err.message || err);
        paintLive();
      });
    });
  });
  $("more-btn").addEventListener("click", () => {
    const sheet = $("more-sheet");
    if (sheet && !sheet.hidden) {
      showSheet("more-sheet", false);
      stageReturn();
      return;
    }
    openSurface("more", undefined, $("more-btn"));
  });
  document.querySelectorAll("[data-quick]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.getAttribute("data-quick");
      if (window.EvieFeedback) window.EvieFeedback.emit("tapPrimary", btn);
      if (kind === "chat") {
        openSurface("conversation");
        return;
      }
      if (kind === "inbox") {
        openSurface("inbox");
        return;
      }
      if (kind === "tactical") {
        openSurface("tactical");
        return;
      }
      if (kind === "today") {
        openSurface("today");
        return;
      }
      if (kind === "weather") {
        openSurface("weather");
        return;
      }
      const prompt = "";
      if (!prompt) return;
      sendText(prompt).catch((err) => {
        state.caption = String(err.message || err);
        paintLive();
      });
  });
  document.querySelectorAll("[data-surface]").forEach((btn) => {
    btn.addEventListener("click", () => {
      openSurface(btn.getAttribute("data-surface"), undefined, btn);
    });
  });
  const memoryForm = $("memory-search-form");
  if (memoryForm) {
    memoryForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const q = $("memory-q");
      refreshMemories(q ? q.value.trim() : "");
    });
  }
  const searchForm = $("search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const q = $("search-q");
      runSearch(q ? q.value : "");
    });
  }
  const capturePrivacy = $("capture-privacy");
  if (capturePrivacy) {
    capturePrivacy.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      capturePrivacy.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    });
  }
  const captureForm = $("capture-form");
  if (captureForm) {
    captureForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      submitCapture();
    });
  }
  const peopleFilter = $("people-filter-form");
  if (peopleFilter) {
    peopleFilter.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const q = $("people-q");
      refreshPeople(q ? q.value : "");
    });
  }
  const routinesEnabled = $("routines-enabled");
  if (routinesEnabled) {
    routinesEnabled.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      routinesEnabled.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
    });
  }
  const routinesAdd = $("routines-add-form");
  if (routinesAdd) {
    routinesAdd.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const input = $("routines-new-time");
      const value = input && input.value;
      if (value && routineTimes.length < 4 && routineTimes.indexOf(value) === -1) {
        routineTimes.push(value);
        renderRoutineTimes();
      }
      if (input) input.value = "";
    });
  }
  const routinesSave = $("routines-save-btn");
  if (routinesSave) {
    routinesSave.addEventListener("click", () => saveRoutines());
  }
  const weatherForm = $("weather-form");
  if (weatherForm) {
    weatherForm.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const place = $("weather-place");
      refreshWeather(place ? place.value : "");
    });
  }
  const todayNote = $("today-note-btn");
  if (todayNote) {
    todayNote.addEventListener("click", () => openSurface("capture"));
  }
  const todayLook = $("today-look-btn");
  if (todayLook) {
    todayLook.addEventListener("click", () => {
      sendText("Look at this.").catch((err) => {
        state.caption = String(err.message || err);
        render();
      });
    });
  }
  const inboxAckAll = $("inbox-ack-all-btn");
  if (inboxAckAll) {
    inboxAckAll.addEventListener("click", () => markAllInboxRead());
  }
  const convCopy = $("conv-copy-btn");
  if (convCopy) {
    convCopy.addEventListener("click", () => copyConversation());
  }
  const convShare = $("conv-share-btn");
  if (convShare) {
    convShare.addEventListener("click", () => shareConversation());
  }
  const convQ = $("conv-q");
  if (convQ) {
    convQ.addEventListener("input", () => paintConversation(convQ.value));
  }
  const queueRefresh = $("queue-refresh-btn");
  if (queueRefresh) {
    queueRefresh.addEventListener("click", () => refreshQueue());
  }
  const voiceNoteBtn = $("voice-note-btn");
  if (voiceNoteBtn) {
    voiceNoteBtn.addEventListener("click", () => toggleVoiceNote());
  }
  // The quiet room uses visible navigation; horizontal drags remain native.
  });

/* Cycle 60 — the Today card: server-computed morning brief rendered into
   the conversation surface. Read-only data the phone already owns. */
async function showBrief() {
  const body = await api("/v1/device-gateway/brief");
  const lines = [];
  lines.push(body.date || "");
  if (body.greeting_name) lines[0] += " · " + body.greeting_name;
  (body.calendar_today || []).forEach((ev) => {
    const hhmm = String(ev.start || "").slice(11, 16);
    lines.push((hhmm ? hhmm + " " : "") + (ev.title || "Event"));
  });
  if (!(body.calendar_today || []).length) lines.push("No events on the calendar today.");
  lines.push("Inbox: " + (body.inbox_unread || 0) + " unread");
  (body.nudges || []).forEach((n) => lines.push("• " + (n.title || "") + (n.body ? " — " + n.body : "")));
  if (typeof body.battery_percent === "number" && body.battery_percent <= 20) {
    lines.push("Battery " + Math.round(body.battery_percent) + "% — consider charging.");
  }
  state.caption = lines.join(" · ").slice(0, 600);
  pushHistory("evie", lines.join("\n"));
  openSurface("conversation");
  render();
}

function voiceMode() {
  return localStorage.getItem("evie-voice-mode") || "continuous";
}

  $("talk").addEventListener("click", () => {
    if (window.EvieFeedback) window.EvieFeedback.visualPress($("talk"));
    if (voiceMode() === "ptt" && state.talking) return; // hold-to-talk owns the control
    talk().catch((err) => {
      state.caption = String(err.message || err);
      render();
      stopTalk();
    });
  });
  const talkBtn = $("talk");
  talkBtn.addEventListener("pointerdown", () => {
    if (voiceMode() !== "ptt" || !state.talking || !state.webrtc || !state.webrtc.pttMode) return;
    if (window.EvieFeedback) window.EvieFeedback.haptic(10);
    talkBtn.classList.add("holding");
    state.webrtc.holdToTalk();
  });
  const releaseTalk = () => {
    talkBtn.classList.remove("holding");
    if (voiceMode() !== "ptt" || !state.talking || !state.webrtc || !state.webrtc.pttMode) return;
    state.webrtc.releaseToTalk();
  };
  talkBtn.addEventListener("pointerup", releaseTalk);
  talkBtn.addEventListener("pointercancel", releaseTalk);
  talkBtn.addEventListener("pointerleave", releaseTalk);
  initSwipes(openSurface);
  initSheetGestures();
  document.querySelectorAll(".sheet-close").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.id === "room-camera-close") {
        state._roomCameraGeneration = (state._roomCameraGeneration || 0) + 1;
        const video = $("preview");
        if (video.srcObject) video.srcObject.getTracks().forEach(track => track.stop());
        video.srcObject = null;
        video.hidden = true;
      }
      showSheet(btn.getAttribute("data-close"), false);
      stageReturn();
    });
  });
  const appearance = $("appearance");
  if (appearance) {
    const saved = localStorage.getItem("evie-appearance") || "system";
    applyAppearance(saved);
    appearance.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (btn) applyAppearance(btn.getAttribute("data-appearance"));
    });
  }
  const enableAlerts = $("enable-alerts-btn");
  if (enableAlerts) {
    enableAlerts.addEventListener("click", () => runControl("enable-alerts-btn", enableLocalAlerts));
  }
  $("self-test-btn").addEventListener("click", () => runControl("self-test-btn", runSelfTest));
  /* Cycle 75 — density: auto (≤380px = SE compact), compact, comfortable. */
  /* Cycle 79 — push-to-wake: the service worker (or the ?wake=1 entry
     link) tells the app to open the live session on arrival, with the
     explicit takeover flag from lease arbitration. */
  const wakeNow = () => {
    if (state.talking) return;
    state._wakeTakeover = true;
    talk().finally(() => { state._wakeTakeover = false; }).catch(() => {});
  };
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener("message", (ev) => {
      if (ev.data && ev.data.type === "wake_live") wakeNow();
    });
  }
  try {
    const wakeParam = new URLSearchParams(location.search).get("wake");
    if (wakeParam === "1") {
      history.replaceState(null, "", location.pathname);
      setTimeout(wakeNow, 800);
    }
  } catch (_err) {}
  /* Cycle 83 — dev overlay entry: triple-tap the mood line. */
  const moodEl = $("mood");
  if (moodEl) {
    let taps = 0;
    let timer = 0;
    moodEl.addEventListener("click", () => {
      taps += 1;
      clearTimeout(timer);
      timer = setTimeout(() => { taps = 0; }, 900);
      if (taps >= 3) {
        taps = 0;
        toggleLatencyOverlay();
      }
    });
  }
  const densitySeg = $("density");
  const applyDensity = (mode) => {
    const compact = mode === "compact" || (mode === "auto" && Math.min(window.innerWidth || 999, window.screen && window.screen.width || 999) <= 380);
    document.body.classList.toggle("compact", compact);
    localStorage.setItem("evie-density", mode);
    const buttons = (densitySeg && densitySeg.querySelectorAll("button")) || [];
    for (let i = 0; i < buttons.length; i += 1) {
      buttons[i].classList.toggle("on", buttons[i].getAttribute("data-density") === mode);
    }
  };
  if (densitySeg) {
    applyDensity(localStorage.getItem("evie-density") || "auto");
    densitySeg.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (btn) applyDensity(btn.getAttribute("data-density"));
    });
    window.addEventListener("resize", () => {
      applyDensity(localStorage.getItem("evie-density") || "auto");
    });
  }
  /* Cycle 56 — voice mode: continuous (server VAD auto-responds) vs
     hold-to-talk (provider auto-response off; owner holds Talk, release
     commits + requests the response). */
  const voiceModeSeg = $("voice-mode");
  if (voiceModeSeg) {
    const savedMode = localStorage.getItem("evie-voice-mode") || "continuous";
    Array.prototype.forEach.call(voiceModeSeg.querySelectorAll("button"), (btn) => {
      btn.classList.toggle("on", btn.getAttribute("data-voice-mode") === savedMode);
    });
    voiceModeSeg.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const next = btn.getAttribute("data-voice-mode") || "continuous";
      localStorage.setItem("evie-voice-mode", next);
      Array.prototype.forEach.call(voiceModeSeg.querySelectorAll("button"), (b) => {
        b.classList.toggle("on", b === btn);
      });
      if (state.webrtc) state.webrtc.setPtt(next === "ptt");
    });
  }
  const installBridge = $("install-bridge-btn");
  if (installBridge) {
    installBridge.addEventListener("click", () => {
      if (!window.EvieMobileActions) return;
      window.EvieMobileActions.installBridge().catch((err) => {
        state.caption = String(err.message || err);
        render();
      });
    });
  }
  const bridgeReady = $("bridge-ready-btn");
  if (bridgeReady) {
    bridgeReady.addEventListener("click", () => {
      if (!window.EvieMobileActions) return;
      window.EvieMobileActions.markInstalled([
        "create_timer",
        "create_reminder",
        "call_contact",
        "message_contact",
        "start_directions",
        "open_maps",
        "facetime_contact",
        "create_alarm",
        "create_calendar_event",
        "self_test",
      ]);
      window.EvieMobileActions.handshake().then(() => hello()).catch(() => {});
    });
  }
  $("ma-go").addEventListener("click", () => {
    if (window.EvieMobileActions) runControl("ma-go", () => window.EvieMobileActions.run());
  });
  $("ma-cancel").addEventListener("click", () => {
    if (window.EvieMobileActions) runControl("ma-cancel", () => window.EvieMobileActions.cancel());
  });
  $("retry-btn").addEventListener("click", () => runControl("retry-btn", async () => {
    try { await hello(); } catch (err) { scheduleReconnect(); throw err; }
  }));
  document.addEventListener("click", (ev) => {
    const node = ev.target;
    if (!node || !node.closest) return;
    const btn = node.closest("[data-camera-role]");
    if (!btn) return;
    setCameraRole(btn.getAttribute("data-camera-role") || "unknown");
  });
  const cameraLine = $("camera-role-line");
  if (cameraLine) {
    cameraLine.addEventListener("click", () => openSurface("privacy"));
  }
  paintCameraRole();
  const updateLine = $("update-line");
  if (updateLine) {
    updateLine.addEventListener("click", () => {
      runControl("update-line", async () => {
        if (!(await updateServiceWorkerOnce())) {
          state.caption = "Automatic update is unavailable. Close Evie and reopen it to load the latest version.";
          render();
        }
      });
    });
  }
  $("copy-voice-diag-btn").addEventListener("click", () => runControl("copy-voice-diag-btn", copyVoiceDiagnostic));
  $("copy-phone-diag-btn").addEventListener("click", () => runControl("copy-phone-diag-btn", copyPhoneDiagnostic));
  $("retry-voice-btn").addEventListener("click", () => {
    (state.talking ? stopTalk() : Promise.resolve()).then(() => talk()).catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("reset-btn").addEventListener("click", () => runControl("reset-btn", () => resetLocal(false)));
  $("unpair-btn").addEventListener("click", () => runControl("unpair-btn", () => resetLocal(true)));
  $("glitch-btn").addEventListener("click", () => runControl("glitch-btn", reportGlitch));
  $("diag-run-btn").addEventListener("click", () => runControl("diag-run-btn", runAudioDiagnostic));
  $("mic-check-btn").addEventListener("click", () => micCheck().catch((err) => {
    state.caption = String(err.message || err);
    render();
  }));
  $("asr-check-btn").addEventListener("click", () => asrCheck().catch((err) => {
    state.caption = String(err.message || err);
    render();
  }));
  $("understand-btn").addEventListener("click", () => understandingCheck());
  $("output-check-btn").addEventListener("click", () => outputCheck().catch((err) => {
    state.caption = String(err.message || err);
    render();
  }));
  $("tap-done-btn").addEventListener("click", () => {
    if (state.webrtc?.dc?.readyState === "open" && state.webrtc.commitTurn) {
      state.webrtc.commitTurn();
      state.caption = "Turn submitted. Waiting for Evie.";
    } else state.caption = "Connect Talk before ending a turn.";
    render();
  });
  $("misheard-btn").addEventListener("click", () => reportMisheard().catch((err) => {
    state.caption = String(err.message || err);
    render();
  }));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.talking) { /* keep live; do not auto-stop */ }
    if (!document.hidden) {
      syncPhoneLife().catch(() => {});
    }
    if (!document.hidden && window.EvieMobileActions) {
      const checkpoint = window.EvieMobileActions.onForeground();
      if (checkpoint && state.talking && state.webrtc && state.webrtc.pc) {
        const cs = state.webrtc.pc.connectionState;
        if (cs === "failed" || cs === "closed" || cs === "disconnected") {
          setMood("Reconnecting");
          stopTalk({ preserveLease: true }).then(() => talk()).catch(() => {});
        }
      }
    }
  });
  try {
    state.deviceToken = await loadToken();
    const storedAccess = await idbGet("access_token");
    if (storedAccess) state.accessToken = storedAccess;
    if (state.deviceToken && window.EvieNativeShell && window.EvieNativeShell.post) {
      window.EvieNativeShell.post({ type: "bind_session", token: state.deviceToken });
    }
    if (state.deviceToken) {
      try {
        const refreshed = await api("/v1/device-gateway/session", {
          method: "POST",
          _useDeviceToken: true,
          _retried: true,
        });
        if (refreshed && refreshed.access_token) {
          state.accessToken = refreshed.access_token;
          await idbPut("access_token", refreshed.access_token).catch(() => {});
          state.device = refreshed.device || state.device;
          state.status = refreshed.status || state.status;
        }
      } catch (err) {
        if (err && err.status === 401) {
          await idbDel("device_token").catch(() => {});
          await idbDel("access_token").catch(() => {});
          state.deviceToken = null;
          state.accessToken = null;
          bootFail("A00", "DEVICE_REVOKED", "This phone is no longer trusted.", { status: 401 });
          return;
        }
      }
      await hello();
    } else setConn("DISCONNECTED");
  } catch (err) {
    state.caption = "Couldn’t connect: " + String(err.message || err) + " Open Tools → Settings → Retry connection.";
    setConn("DISCONNECTED");
  }
  render();
}

boot();
// Cycle 04 — iPhone-only transcript-export helper. Backward compatible: new
// window.EvieTranscript namespace only; no existing code modified. toText and
// toBlob are pure (turns[] -> string/Blob, no DOM dependency); download is a
// thin DOM helper kept separate so tests can use the pure path.
window.EvieTranscript = (function () {
  function lineOf(turn) {
    var role = turn && turn.role != null ? String(turn.role) : "unknown";
    var text = turn && turn.text != null ? String(turn.text) : "";
    return role + ": " + text;
  }
  function toText(turns) {
    if (!Array.isArray(turns) || turns.length === 0) return "";
    return turns.map(lineOf).join("\n");
  }
  function toBlob(turns) {
    return new Blob([toText(turns)], { type: "text/plain;charset=utf-8" });
  }
  function download(turns, filename) {
    var name = filename || "evie-transcript.txt";
    var url = URL.createObjectURL(toBlob(turns));
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    return name;
  }
  return { toText: toText, toBlob: toBlob, download: download };
})();
// Cycle 05 — iPhone-only conversation-search filter. Backward compatible: new
// window.EvieSearch namespace only; filterTurns is a pure function over
// turns[] with no DOM dependency and no existing-code changes.
window.EvieSearch = (function () {
  function filterTurns(turns, query) {
    if (!Array.isArray(turns)) return [];
    var q = String(query == null ? "" : query).trim().toLowerCase();
    if (!q) return turns.slice();
    return turns.filter(function (t) {
      var role = t && t.role != null ? String(t.role) : "";
      var text = t && t.text != null ? String(t.text) : "";
      return (role + " " + text).toLowerCase().indexOf(q) !== -1;
    });
  }
  return { filterTurns: filterTurns };
})();
// Cycle 06 — iPhone-only quick-action row model. Backward compatible: new
// window.EvieQuickActions namespace only; actions() returns a fresh array of
// {id,label,hint} rows (briefing/look/memory) so callers cannot mutate it.
window.EvieQuickActions = (function () {
  var ACTIONS = [
    { id: "briefing", label: "Briefing", hint: "Catch up on today" },
    { id: "look", label: "Look", hint: "Share what the camera sees" },
    { id: "memory", label: "Memory", hint: "Recall saved context" }
  ];
  function actions() {
    return ACTIONS.map(function (a) {
      return { id: a.id, label: a.label, hint: a.hint };
    });
  }
  return { actions: actions };
})();
