const CLIENT_BUILD = "2026.08.21.22";
const DESIGN_VERSION = "veil-1";
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
  return engine;
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
  audioLeader: false,
  ws: null,
  webrtc: null,
  mediaBackend: "webrtc_strict",
  activeBackend: "none",
  voiceHealth: null,
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
  else if (next === "READY") state.ui = "READY";
  else if (next === "DISCONNECTED" && !state.deviceToken) state.ui = "UNPAIRED";
  render();
}

function textOf(el, value) {
  if (el) el.textContent = value == null ? "" : String(value);
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

function render() {
  const unpaired = !state.deviceToken;
  $("welcome").hidden = !unpaired;
  $("ready-ui").hidden = unpaired;
  const online = state.conn === "READY" || state.conn === "ACTIVE";
  const offline = state.conn === "DISCONNECTED" || state.conn === "OFFLINE";
  textOf($("status"), offline && unpaired ? "Pair this device" : (online ? "Private" : state.conn));
  $("talk").disabled = !online || state.ui === "OFFLINE";
  $("talk").textContent = state.talking ? "Stop" : "Talk";
  $("talk").setAttribute("aria-label", state.talking ? "Stop talking" : "Talk to Evie");
  const hello = state.hello || {};
  const device = hello.device || state.device || {};
  textOf($("device-role"), prettyRole(device.role) || "—");
  textOf($("home-station"), homeLine(hello));
  textOf($("environment"), hello.environment || "SANDBOX");
  textOf($("sandbox-banner"), "Personal memory off");
  textOf($("user-line"), state.userLine);
  textOf($("reply"), state.caption);
  if (state.ui === "OFFLINE") setMood("Home Station is offline.");
  const st = hello.states || {};
  textOf($("states"), [
    st.tailnet ? "TAILNET " + String(st.tailnet).toUpperCase() : "",
    st.evie_core ? "EVIE CORE " + String(st.evie_core).toUpperCase() : "",
    st.realtime ? "REALTIME " + String(st.realtime).toUpperCase() : "",
  ].filter(Boolean).join(" · "));
  fillSettings(hello, device);
  fillPrivacy(hello, device);
  fillMobileActions(hello);
  fillDevices(hello, device);
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
    last_asr_label: "TRANSCRIPT",
    last_asr: state.lastAsr,
    last_asr_confidence: state.lastAsrConfidence,
    last_independent_asr: state.lastIndependentAsr,
    forensic: state.forensic,
    protocol_version: PROTOCOL_VERSION,
    server_build: hello.pwa_build || hello.server_build,
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

function copyVoiceDiagnostic() {
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
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(function () {});
  }
  state.caption = "Voice diagnostic copied.";
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
  const rows = [
    ["Bridge", ma.bridge_installed ? "Installed" : "Not installed"],
    ["This device", ma.this_device || "This iPhone"],
    ["Protocol", String(ma.protocol || 1)],
    ["Version", ma.bridge_version || "—"],
  ];
  caps.forEach((cap) => {
    rows.push([cap.title || cap.operation, cap.available ? "Ready" : (cap.reason || "Unavailable")]);
  });
  if (ma.last_action && ma.last_action.action_id) {
    rows.push(["Last action", (ma.last_action.operation || "") + " · " + (ma.last_action.state || "")]);
  }
  fillDl("mobile-actions-meta", rows);
}

function fillSettings(hello, device) {
  fillDl("settings-meta", [
    ["Device", device.display_name || prettyRole(device.role) || "—"],
    ["Role", prettyRole(device.role) || "—"],
    ["Connection", state.conn],
    ["Home Station", homeLine(hello)],
    ["PWA build", CLIENT_BUILD],
    ["Runtime", (window.EvieMobileVoice && window.EvieMobileVoice.RUNTIME_VERSION) || "—"],
    ["Signaling", hello.signaling_version || "unified-calls-v1"],
    ["Design", DESIGN_VERSION],
    ["Protocol", PROTOCOL_VERSION],
  ]);
}

function fillPrivacy(hello, device) {
  fillDl("privacy-meta", [
    ["Private connection", "On"],
    ["Public Funnel", "Off"],
    ["Personal memory", "Off"],
    ["Microphone", state.capture === "none" ? "Ask on Talk" : "Allowed"],
    ["Camera", $("camera-sheet").hidden ? "Ask on Look" : "Allowed"],
    ["This phone", prettyRole(device.role) || "Companion"],
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

function pushHistory(role, text) {
  if (!text) return;
  state.history.push({ role: role, text: text });
  if (state.history.length > 24) state.history.shift();
  const list = $("history");
  while (list.firstChild) list.removeChild(list.firstChild);
  state.history.forEach((item) => {
    const li = document.createElement("li");
    li.className = item.role === "user" ? "as-user" : "as-evie";
    li.textContent = (item.role === "user" ? "" : "") + item.text;
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

function showSheet(id, on) {
  const el = $(id);
  if (el) el.hidden = !on;
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
  const bearer = state.accessToken || state.deviceToken;
  if (bearer) headers.Authorization = "Bearer " + bearer;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  const body = await res.json().catch(() => ({}));
  if (res.status === 401 && state.deviceToken && !opts._retried) {
    const refreshed = await api("/v1/device-gateway/session", { method: "POST", _retried: true }).catch(() => null);
    if (refreshed && refreshed.access_token) {
      state.accessToken = refreshed.access_token;
      return api(path, Object.assign({}, opts, { _retried: true }));
    }
  }
  if (!res.ok) {
    const err = new Error(detailOf(body, res.statusText || "request failed"));
    err.status = res.status;
    err.body = body;
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

async function hello() {
  setConn("AUTHENTICATING");
  const body = await api("/v1/device-gateway/hello", {
    method: "POST",
    body: JSON.stringify({
      protocol_version: PROTOCOL_VERSION,
      client_build: CLIENT_BUILD,
      instance_id: state.instanceId,
      capabilities: ["foreground_voice", "camera", "text"],
      foreground: !document.hidden,
    }),
  });
  if (body.pwa_build && body.pwa_build !== CLIENT_BUILD) {
    textOf($("reply"), "Update Evie");
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistration) {
      const reg = await navigator.serviceWorker.getRegistration("/evie/");
      if (reg) await reg.update();
    }
    setTimeout(() => location.reload(), 500);
    return;
  }
  state.hello = body;
  state.device = body.device;
  state.mediaBackend = body.recommended_backend || "auto";
  if (window.EvieMobileActions) {
    window.EvieMobileActions.configure({
      api: api,
      instanceId: state.instanceId,
      onActivity: (line) => pushActivity(line),
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
}

async function pair() {
  const token = $("pair-token").value.trim();
  if (!token) return;
  setConn("AUTHENTICATING");
  const body = await api("/v1/device-gateway/pair", {
    method: "POST",
    body: JSON.stringify({
      pairing_token: token,
      protocol_version: PROTOCOL_VERSION,
      client_version: CLIENT_BUILD,
      instance_id: state.instanceId,
      capabilities: ["foreground_voice", "camera", "text"],
    }),
  });
  state.deviceToken = body.device_token;
  state.accessToken = body.access_token;
  state.device = body.device;
  await saveToken(body.device_token);
  $("pair-token").value = "";
  await hello();
}

async function sendText(text) {
  const body = await api("/v1/device-gateway/text", {
    method: "POST",
    body: JSON.stringify({
      text,
      instance_id: state.instanceId,
      request_id: crypto.randomUUID(),
    }),
  });
  state.caption = body.reply || "";
  pushHistory("user", text);
  pushHistory("evie", body.reply || "");
  if (body.conversation_moved) await stopTalk();
  if (body.needs_camera) await captureCamera(body);
  if (body.phone_action && window.EvieMobileActions) {
    window.EvieMobileActions.present(body.phone_action);
  }
  render();
  return body;
}

async function captureCamera(body, facing) {
  $("camera-sheet").hidden = false;
  textOf($("camera-copy"), "Opening perception");
  setMood("Camera");
  const video = $("preview");
  const canvas = $("snap");
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: { ideal: facing || "environment" }, width: { max: 1280 }, height: { max: 720 } },
    audio: false,
  });
  video.srcObject = stream;
  video.hidden = false;
  await video.play();
  await new Promise((r) => requestAnimationFrame(r));
  canvas.width = Math.min(video.videoWidth || 640, 1280);
  canvas.height = Math.min(video.videoHeight || 480, 720);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const jpeg = canvas.toDataURL("image/jpeg", 0.7).split(",")[1];
  stream.getTracks().forEach((t) => t.stop());
  video.hidden = true;
  video.srcObject = null;
  $("camera-sheet").hidden = true;
  if (body && body.camera_request_id) {
    await api("/v1/device-gateway/camera/result", {
      method: "POST",
      body: JSON.stringify({ request_id: body.camera_request_id, jpeg_b64: jpeg }),
    });
  }
  if (state.talking) setMood("Listening");
  else setMood("Ready");
  return jpeg;
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

async function attachCapture(ws, stream) {
  const ctx = new AudioContext();
  if (ctx.state === "suspended") await ctx.resume();
  const source = ctx.createMediaStreamSource(stream);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  const sourceRate = ctx.sampleRate;
  const sendPcm = (float32) => {
    if (!state.talking || ws.readyState !== WebSocket.OPEN) return;
    if (engine.halfDuplex && engine.playing) return;
    const pcm = downsample(float32, sourceRate, TARGET_RATE);
    ws.send(pcm.buffer);
  };
  if (ctx.audioWorklet) {
    try {
      await ctx.audioWorklet.addModule("/evie/pcm-worklet.js" + ASSET_V);
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

async function handleLiveMessage(gen, ev) {
  if (gen !== state.sessionGen) return;
  if (typeof ev.data !== "string") return;
  const msg = JSON.parse(ev.data);
  if (msg.type === "ready") setMood("Listening");
  if (msg.type === "final_transcript" && msg.text) {
    state.userLine = msg.text;
    pushHistory("user", msg.text);
    setMood("Thinking");
    render();
  }
  if (msg.type === "partial" && msg.text) {
    state.userLine = msg.text;
    textOf($("user-line"), msg.text);
  }
  if (msg.type === "barge_in" && engine) engine.stop();
  if (msg.type === "hud") {
    const kind = (msg.hud && msg.hud.kind) || msg.kind || "";
    if (kind === "progress") {
      setMood("Working on MacBook");
      textOf($("action-card"), "MacBook · working");
      $("action-card").hidden = false;
      pushActivity("Working on MacBook");
    }
  }
  if (msg.type === "reply" && msg.text) {
    state.caption = msg.text;
    pushHistory("evie", msg.text);
    if (engine && engine.endStream) engine.endStream();
    render();
  }
  if (msg.type === "tts_chunk" && msg.audio_b64) {
    if (state.activeBackend === "webrtc" || state.activeBackend === "webrtc_strict") return;
    if (!state.audioLeader || !engine) return;
    const contentType = msg.content_type || "audio/pcm";
    if (contentType.indexOf("mpeg") >= 0 || contentType.indexOf("mp3") >= 0) {
      if (engine.playing || engine.metrics.chunks) return;
      return;
    }
    if (msg.index === 0) {
      engine.beginTurn({
        socketGeneration: gen,
        responseId: msg.response_id || msg.responseId || null,
      });
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
  if (msg.type === "error" && msg.fatal) await stopTalk();
}

async function handleCameraRequest(msg) {
  try {
    const jpeg = await captureCamera(null);
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({
        type: "look_frame",
        request_id: msg.request_id,
        jpeg_b64: jpeg,
        last: true,
      }));
    } else if (state.sessionId) {
      await api("/v1/device-gateway/live/look-frame", {
        method: "POST",
        body: JSON.stringify({
          session_id: state.sessionId,
          request_id: msg.request_id,
          jpeg_b64: jpeg,
          last: true,
        }),
      });
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

async function talk() {
  if (state.talking) {
    if (state.webrtc && state.webrtc.playBlocked) {
      try {
        await state.webrtc.enableAudio();
        state.caption = "";
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
  if (window.EvieFeedback) window.EvieFeedback.emit("conversationStart", $("talk"));
  claimAudioLeader();
  setConn("ACTIVE");
  setMood("Connecting microphone…");
  $("talk").textContent = "Stop";
  const opened = await api("/v1/device-gateway/live/open", {
    method: "POST",
    body: JSON.stringify({
      instance_id: state.instanceId,
      method: "manual",
      media_backend: "webrtc_strict",
    }),
  });
  state.sessionId = opened.session_id;
  const want = opened.media_backend || "webrtc_strict";
  const strict = opened.strict_webrtc === true || want === "webrtc_strict" || !opened.ws_ticket;
  if ((want === "webrtc" || want === "webrtc_strict") && window.RTCPeerConnection && window.EvieWebRTC) {
    try {
      setMood("Connecting voice…");
      await startWebRTC(opened);
      return;
    } catch (err) {
      const diag = (err && err.diag) || (state.webrtc && state.webrtc.diag && state.webrtc.diag.snapshot());
      state.connectionDiag = diag || {
        failed_stage: err && err.failed_stage,
        error_message: String(err && err.message || err),
        http_status: err && (err.provider_status || err.status),
      };
      if (err && err.audio_blocked) {
        state.talking = true;
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
      if (stage === "M02") state.caption = "Microphone access denied.";
      else if (stage === "M09" || stage === "M10") state.caption = "Couldn't connect to Evie Voice.";
      else state.caption = "Couldn't connect to Evie Voice.";
      setMood("Voice unavailable");
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
  await startPcm(opened);
}

async function startWebRTC(opened) {
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
    audioEl: $("webrtc-out"),
    onState: (label) => {
      if (label === "listening") setMood("Listening");
      if (label === "thinking") setMood("Thinking");
      if (label === "speaking") setMood("Speaking");
      if (label === "moved") loseAudio("conversation_moved");
      if (label === "mic_ended") {
        setMood("Voice unavailable");
        state.caption = "Microphone ended.";
      }
      if (label === "audio_blocked") {
        setMood("Voice connected — tap to enable audio");
        state.caption = "Voice connected — tap to enable audio";
      }
    },
    onTranscript: (text, meta) => {
      state.lastAsr = text;
      state.lastAsrConfidence = meta && meta.confidence;
      state.userLine = "TRANSCRIPT · " + text;
      pushHistory("user", text);
      render();
    },
    onCaption: (text, done) => {
      state.caption = done ? text : (state.caption + text);
      if (done) pushHistory("evie", state.caption);
      render();
    },
    onEnvelope: (amp) => {
      if (state.orb) state.orb.setAmp(amp);
    },
    onCamera: (ev) => handleCameraRequest(ev),
    onHud: (hud) => {
      if (window.EvieMobileActions && window.EvieMobileActions.presentFromHud(hud)) {
        render();
        return;
      }
      if ((hud && hud.kind) === "progress" || hud.name) {
        setMood("Working on MacBook");
        $("action-card").hidden = false;
        textOf($("action-card"), "MacBook · " + (hud.name || "working"));
        pushActivity("MacBook · " + (hud.name || "working"));
      }
    },
    onHealth: (snap) => {
      state.voiceHealth = snap;
      if (snap && snap.connection) state.connectionDiag = snap.connection;
      render();
    },
  });
  state.webrtc = rtc;
  state.talking = true;
  if (window.EvieMobileActions) window.EvieMobileActions.setSession(opened.session_id);
  const signaling = /voice_signaling=ephemeral/.test(location.search) ? "ephemeral_direct" : "unified_calls";
  const mic = await rtc.start(opened, { signaling: signaling });
  state.connectionDiag = rtc.diag ? rtc.diag.snapshot() : null;
  state.capture = "webrtc_native_track";
  state.captureSettings = (mic.settings && mic.settings.actual) || mic.settings || {};
  setMood("Listening");
  render();
}

async function startPcm(opened) {
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
  const track = stream.getAudioTracks()[0];
  state.captureSettings = track && track.getSettings ? track.getSettings() : {};
  await attachCapture(ws, stream);
  let queue = Promise.resolve();
  ws.onmessage = (ev) => {
    queue = queue.then(() => handleLiveMessage(gen, ev)).catch(() => {});
  };
  ws.onclose = () => {
    if (state.talking && gen === state.sessionGen) {
      playback.flushReconnect();
      state.caption = "Reconnecting…";
      setMood("Reconnecting");
      stopTalk().then(() => scheduleReconnect());
    }
  };
}

function closeActiveBackend() {
  if (state.webrtc) {
    state.webrtc.stop();
    state.webrtc = null;
  }
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.close();
  state.ws = null;
  if (engine) engine.stop();
  state.activeBackend = "none";
}

async function stopTalk() {
  if (window.EvieFeedback) window.EvieFeedback.emit("conversationStop", $("talk"));
  state.sessionGen += 1;
  if (engine) engine.socketGeneration = state.sessionGen;
  if (engine) engine.flushReconnect();
  state.talking = false;
  state.audioLeader = false;
  $("talk").textContent = "Talk";
  const sessionId = state.sessionId;
  closeActiveBackend();
  if (state._audio) {
    if (state._audio.node) state._audio.node.disconnect();
    if (state._audio.proc) state._audio.proc.disconnect();
    if (state._audio.mute) state._audio.mute.disconnect();
    state._audio.source.disconnect();
    state._audio.stream.getTracks().forEach((t) => t.stop());
    await state._audio.ctx.close().catch(() => {});
    state._audio = null;
  }
  await api("/v1/device-gateway/live/close", {
    method: "POST",
    body: JSON.stringify({ instance_id: state.instanceId, session_id: sessionId }),
  }).catch(() => {});
  await api("/v1/device-gateway/conversation/release", {
    method: "POST",
    body: JSON.stringify({ instance_id: state.instanceId }),
  }).catch(() => {});
  state.sessionId = null;
  $("action-card").hidden = true;
  if (state.conn === "ACTIVE") {
    setMood("Ready");
    setConn("READY");
  }
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
    const health = await fetch("/v1/device-gateway/health").then((r) => r.json());
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
  if (state.webrtc && !state.webrtc.closed) {
    state.webrtc.perceptionProbe();
    state.caption = "Understanding Check: text-only probe sent. No extra speech should play.";
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
  await api("/v1/device-gateway/mobile-voice/misheard", {
    method: "POST",
    body: JSON.stringify({
      intended: "Turn off the Wi-Fi after I finish this sentence.",
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
  }).catch(() => {});
  render();
}

async function resetLocal(unpair) {
  if (state.talking) await stopTalk();
  sessionStorage.removeItem("evie_instance");
  state.instanceId = crypto.randomUUID();
  sessionStorage.setItem("evie_instance", state.instanceId);
  if (unpair) {
    await idbDel("device_token");
    state.deviceToken = null;
    state.accessToken = null;
    state.device = null;
    state.hello = null;
  }
  if (window.caches) {
    const keys = await caches.keys();
    await Promise.all(keys.filter((key) => key.indexOf("evie-static-") === 0).map((key) => caches.delete(key)));
  }
  state.caption = unpair ? "Device unpaired. Pair again." : "Local client reset. Pairing kept.";
  render();
}

async function boot() {
  await ensureAudioModules();
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const Presence = window.EviePresence || window.EvieOrb;
  state.orb = new Presence($("orb"));
  state.orb.setReduced(reduce);
  state.orb.start();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/evie/sw.js", { scope: "/evie/" }).catch(() => {});
  }
  $("pair-btn").addEventListener("click", () => pair().catch((err) => {
    state.caption = String(err.message || err);
    setConn("DISCONNECTED");
  }));
  $("text-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = $("text").value.trim();
    if (!text) return;
    $("text").value = "";
    sendText(text).catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("talk").addEventListener("click", () => {
    if (window.EvieFeedback) window.EvieFeedback.visualPress($("talk"));
    talk().catch((err) => {
      state.caption = String(err.message || err);
      render();
      stopTalk();
    });
  });
  $("look-btn").addEventListener("click", () => {
    sendText("Look at this.").catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("type-btn").addEventListener("click", () => {
    $("text-form").hidden = !$("text-form").hidden;
    if (!$("text-form").hidden) $("text").focus();
  });
  $("more-btn").addEventListener("click", () => {
    $("more-rail").hidden = !$("more-rail").hidden;
  });
  $("history-btn").addEventListener("click", () => {
    showSheet("conversation-sheet", true);
    $("more-rail").hidden = true;
  });
  document.querySelectorAll("[data-surface]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const surface = btn.getAttribute("data-surface");
      showSheet("conversation-sheet", surface === "conversation");
      showSheet("devices-sheet", surface === "devices");
      showSheet("activity-sheet", surface === "activity");
      showSheet("settings-sheet", surface === "privacy");
      $("more-rail").hidden = true;
    });
  });
  document.querySelectorAll(".sheet-close").forEach((btn) => {
    btn.addEventListener("click", () => showSheet(btn.getAttribute("data-close"), false));
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
  $("settings-close").addEventListener("click", () => showSheet("settings-sheet", false));
  $("self-test-btn").addEventListener("click", () => runSelfTest());
  $("install-bridge-btn").addEventListener("click", () => {
    if (!window.EvieMobileActions) return;
    window.EvieMobileActions.installBridge().catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("bridge-ready-btn").addEventListener("click", () => {
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
  $("ma-go").addEventListener("click", () => {
    if (window.EvieMobileActions) window.EvieMobileActions.run();
  });
  $("ma-cancel").addEventListener("click", () => {
    if (window.EvieMobileActions) window.EvieMobileActions.cancel();
  });
  $("retry-btn").addEventListener("click", () => hello().catch(() => scheduleReconnect()));
  $("copy-voice-diag-btn").addEventListener("click", () => copyVoiceDiagnostic());
  $("retry-voice-btn").addEventListener("click", () => {
    (state.talking ? stopTalk() : Promise.resolve()).then(() => talk()).catch((err) => {
      state.caption = String(err.message || err);
      render();
    });
  });
  $("reset-btn").addEventListener("click", () => resetLocal(false));
  $("unpair-btn").addEventListener("click", () => resetLocal(true));
  $("glitch-btn").addEventListener("click", () => reportGlitch());
  $("diag-run-btn").addEventListener("click", () => runAudioDiagnostic());
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
    if (state.webrtc && state.webrtc.commitTurn) state.webrtc.commitTurn();
  });
  $("misheard-btn").addEventListener("click", () => reportMisheard().catch((err) => {
    state.caption = String(err.message || err);
    render();
  }));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.talking) { /* keep live; do not auto-stop */ }
    if (!document.hidden && window.EvieMobileActions) {
      const checkpoint = window.EvieMobileActions.onForeground();
      if (checkpoint && state.talking && state.webrtc && state.webrtc.pc) {
        const cs = state.webrtc.pc.connectionState;
        if (cs === "failed" || cs === "closed" || cs === "disconnected") {
          setMood("Reconnecting");
          stopTalk().then(() => talk()).catch(() => {});
        }
      }
    }
  });
  try {
    state.deviceToken = await loadToken();
    if (state.deviceToken) await hello();
    else setConn("DISCONNECTED");
  } catch (_err) {
    setConn("DISCONNECTED");
  }
  render();
}

boot();
