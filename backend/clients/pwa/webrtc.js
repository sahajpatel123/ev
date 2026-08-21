/* Mobile Voice Core. One PC, one mic track, one remote track, one <audio>.
   WebRTC owns media. Never tap remote audio through Web Audio. */
(function (root) {
  const PRODUCTION_MIC_CONSTRAINTS = {
    audio: {
      channelCount: { ideal: 1 },
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  };

  const STATES = [
    "IDLE",
    "ACQUIRING_MIC",
    "SIGNALING",
    "CONNECTING_MEDIA",
    "VOICE_READY",
    "OWNER_SPEAKING",
    "PROCESSING",
    "EVIE_SPEAKING",
    "TOOL_RUNNING",
    "RECONNECTING",
    "FAILED",
    "ENDED",
  ];

  const NONCE_A = ["Violet", "Amber", "Cobalt", "Ivory", "Cedar", "Maple"];
  const NONCE_B = ["Seven", "Four", "Nine", "Three", "Eight", "Two"];

  function parseEvent(raw) {
    try {
      return JSON.parse(raw);
    } catch (_err) {
      return null;
    }
  }

  function abbreviateId(id) {
    const s = String(id || "");
    return s ? s.slice(0, 8) : "";
  }

  function noncePhrase() {
    const a = NONCE_A[Math.floor(Math.random() * NONCE_A.length)];
    const b = NONCE_B[Math.floor(Math.random() * NONCE_B.length)];
    const c = NONCE_B[Math.floor(Math.random() * NONCE_B.length)];
    const d = NONCE_B[Math.floor(Math.random() * NONCE_B.length)];
    return "My test phrase is " + a + " " + b + " " + c + " " + d + ".";
  }

  function inspectTrack(track) {
    if (!track) {
      return { present: false };
    }
    const settings = track.getSettings ? track.getSettings() : {};
    const requested = track.getConstraints ? track.getConstraints() : {};
    return {
      present: true,
      requested: requested,
      actual: {
        sampleRate: settings.sampleRate || null,
        channelCount: settings.channelCount || null,
        echoCancellation: settings.echoCancellation,
        noiseSuppression: settings.noiseSuppression,
        autoGainControl: settings.autoGainControl,
        deviceId: settings.deviceId ? abbreviateId(settings.deviceId) : null,
        groupId: settings.groupId ? abbreviateId(settings.groupId) : null,
      },
      muted: !!track.muted,
      enabled: !!track.enabled,
      readyState: track.readyState,
      kind: track.kind,
    };
  }

  function classifyLevel(rms, peak, clipRatio) {
    if (clipRatio > 0.01 || peak >= 0.99) return "CLIPPING";
    if (rms < 0.012) return "TOO_QUIET";
    if (rms > 0.45) return "LOUD";
    return "NORMAL";
  }

  function measurePcm16(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength - (bytes.byteLength % 2));
    let sum = 0;
    let peak = 0;
    let clipped = 0;
    const n = view.byteLength / 2;
    for (let i = 0; i < n; i += 1) {
      const s = Math.abs(view.getInt16(i * 2, true) / 32768);
      sum += s * s;
      if (s > peak) peak = s;
      if (s >= 0.99) clipped += 1;
    }
    const rms = n ? Math.sqrt(sum / n) : 0;
    const clipRatio = n ? clipped / n : 0;
    return {
      rms: Number(rms.toFixed(4)),
      peak: Number(peak.toFixed(4)),
      clipRatio: Number(clipRatio.toFixed(5)),
      samples: n,
      level: classifyLevel(rms, peak, clipRatio),
    };
  }

  function exclusivePlaybackIllegal(webrtcActive, pcmActive) {
    return !!(webrtcActive && pcmActive);
  }

  function pickStat(report, pred) {
    if (!report || typeof report.forEach !== "function") return null;
    let found = null;
    report.forEach(function (row) {
      if (!found && pred(row)) found = row;
    });
    return found;
  }

  function sanitizeStats(report) {
    const out = pickStat(report, function (r) {
      return r.type === "outbound-rtp" && (r.kind === "audio" || r.mediaType === "audio");
    });
    const inn = pickStat(report, function (r) {
      return r.type === "inbound-rtp" && (r.kind === "audio" || r.mediaType === "audio");
    });
    const codec = pickStat(report, function (r) { return r.type === "codec" && r.mimeType; });
    function num(row, key) {
      if (!row || row[key] == null) return null;
      const v = Number(row[key]);
      return Number.isFinite(v) ? v : null;
    }
    return {
      outbound: out
        ? {
            packetsSent: num(out, "packetsSent"),
            bytesSent: num(out, "bytesSent"),
            audioLevel: num(out, "audioLevel"),
            targetBitrate: num(out, "targetBitrate"),
          }
        : null,
      inbound: inn
        ? {
            packetsReceived: num(inn, "packetsReceived"),
            packetsLost: num(inn, "packetsLost"),
            bytesReceived: num(inn, "bytesReceived"),
            jitter: num(inn, "jitter"),
            jitterBufferDelay: num(inn, "jitterBufferDelay"),
            concealedSamples: num(inn, "concealedSamples"),
            concealmentEvents: num(inn, "concealmentEvents"),
            silentConcealedSamples: num(inn, "silentConcealedSamples"),
            insertedSamplesForDeceleration: num(inn, "insertedSamplesForDeceleration"),
            removedSamplesForAcceleration: num(inn, "removedSamplesForAcceleration"),
            audioLevel: num(inn, "audioLevel"),
          }
        : null,
      codec: codec && codec.mimeType ? String(codec.mimeType) : null,
    };
  }

  function voiceHealth(snap) {
    const mic = !!(snap.micActive && snap.micReadyState === "live");
    const senders = snap.audioSenders === 1;
    const pc = snap.peerConnections === 1;
    const remote = snap.remoteAudioTracks === 1;
    const el = snap.audioElements === 1;
    const pcm = snap.pcmFallback === "off";
    const tts = snap.fallbackTts === "off";
    const ice = snap.ice === "connected" || snap.ice === "completed";
    const ready = mic && senders && pc && remote && el && pcm && tts && ice && !snap.failed;
    return {
      ready: ready,
      mic: mic ? "GOOD" : "BAD",
      uplink: ice && senders ? "GOOD" : "BAD",
      asr: snap.asr || "UNCERTAIN",
      realtime: snap.sessionActive ? "GOOD" : "BAD",
      downlink: remote && ice ? "GOOD" : "BAD",
      playbackOwner: snap.playbackOwner || "NONE",
      fallback: snap.pcmFallback === "off" && snap.fallbackTts === "off" ? "OFF" : "ILLEGAL",
    };
  }

  function countAudioElements() {
    if (typeof document === "undefined") return 0;
    return document.querySelectorAll("audio").length;
  }

  const RUNTIME_VERSION = "unified-calls-v1";
  const STAGES = [
    ["M00", "USER_GESTURE"],
    ["M01", "SECURE_CONTEXT"],
    ["M02", "MIC_PERMISSION"],
    ["M03", "MIC_TRACK_READY"],
    ["M04", "PEER_CREATED"],
    ["M05", "DATA_CHANNEL_CREATED"],
    ["M06", "LOCAL_TRACK_ADDED"],
    ["M07", "OFFER_CREATED"],
    ["M08", "LOCAL_DESCRIPTION_SET"],
    ["M09", "SIGNALING_REQUEST_STARTED"],
    ["M10", "SIGNALING_RESPONSE_RECEIVED"],
    ["M11", "SDP_ANSWER_VALIDATED"],
    ["M12", "REMOTE_DESCRIPTION_SET"],
    ["M13", "ICE_CONNECTING"],
    ["M14", "ICE_CONNECTED"],
    ["M15", "PEER_CONNECTED"],
    ["M16", "DATA_CHANNEL_OPEN"],
    ["M17", "REALTIME_SESSION_CREATED"],
    ["M18", "SESSION_CONFIG_ACKNOWLEDGED"],
    ["M19", "REMOTE_AUDIO_TRACK_RECEIVED"],
    ["M20", "AUDIO_PLAYBACK_READY"],
    ["M21", "VOICE_READY"],
  ];

  function stageName(id) {
    for (let i = 0; i < STAGES.length; i += 1) {
      if (STAGES[i][0] === id) return STAGES[i][1];
    }
    return id;
  }

  function nextAttemptId() {
    return "mv_" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
  }

  function summarizeSdp(sdp) {
    const text = String(sdp || "");
    let direction = "unknown";
    if (/^a=sendrecv\b/m.test(text)) direction = "sendrecv";
    else if (/^a=recvonly\b/m.test(text)) direction = "recvonly";
    else if (/^a=sendonly\b/m.test(text)) direction = "sendonly";
    return {
      audio_mline: /^m=audio\b/m.test(text),
      application_mline: /^m=application\b/m.test(text),
      opus: /opus\/48000/i.test(text),
      ice: /a=ice-ufrag:/.test(text) && /a=ice-pwd:/.test(text),
      fingerprint: /a=fingerprint:/.test(text),
      direction: direction,
      bytes: text.length,
      lines: text.split(/\r?\n/).length,
    };
  }

  async function sha256Hex(text) {
    if (!window.crypto || !window.crypto.subtle) return "";
    const buf = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(String(text || "")));
    return Array.from(new Uint8Array(buf)).map(function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  }

  function ConnectionDiag(attemptId) {
    this.attempt_id = attemptId;
    this.peer_generation = 0;
    this.signaling = "unified_calls";
    this.stages = {};
    this.failed_stage = null;
    this.error_name = "";
    this.error_message = "";
    this.http_status = null;
    this.pc = {};
    this.mic = {};
    this.dc = "idle";
    this.remote_tracks = 0;
    this.audio_elements = 1;
    this.offer = null;
    this.answer = null;
    this.call_id = "";
    this.token_mode = "server";
    STAGES.forEach(function (row) {
      this.stages[row[0]] = { id: row[0], name: row[1], status: "pending" };
    }, this);
  }

  ConnectionDiag.prototype.pass = function pass(id, extra) {
    const row = this.stages[id];
    if (!row) return;
    row.status = "pass";
    row.at = Date.now();
    if (extra) {
      Object.keys(extra).forEach(function (k) { row[k] = extra[k]; });
    }
  };

  ConnectionDiag.prototype.fail = function fail(id, err, extra) {
    const row = this.stages[id];
    if (row) {
      row.status = "fail";
      row.at = Date.now();
    }
    this.failed_stage = id;
    this.error_name = err && err.name ? String(err.name) : "Error";
    this.error_message = err && err.message ? String(err.message) : String(err || "failed");
    if (err && err.status != null) this.http_status = err.status;
    if (err && err.provider_status != null) this.http_status = err.provider_status;
    if (extra) {
      if (extra.http_status != null) this.http_status = extra.http_status;
      if (extra.pc) this.pc = extra.pc;
    }
  };

  ConnectionDiag.prototype.snapshot = function snapshot() {
    return {
      attempt_id: this.attempt_id,
      peer_generation: this.peer_generation,
      signaling: this.signaling,
      token_mode: this.token_mode,
      failed_stage: this.failed_stage,
      failed_name: this.failed_stage ? stageName(this.failed_stage) : "",
      error_name: this.error_name,
      error_message: this.error_message,
      http_status: this.http_status,
      pc: this.pc,
      mic: this.mic,
      dc: this.dc,
      remote_tracks: this.remote_tracks,
      audio_elements: this.audio_elements,
      offer: this.offer,
      answer: this.answer,
      call_id: this.call_id ? String(this.call_id).slice(0, 20) : "",
      stages: this.stages,
    };
  };

  function formatConnectionDiag(diag, meta) {
    const d = diag || {};
    const extra = meta || {};
    const lines = [
      "Evie Voice Connection Diagnostic",
      "build: " + (extra.build || ""),
      "sw_build: " + (extra.sw_build || ""),
      "runtime: " + RUNTIME_VERSION,
      "audio_mode: " + (extra.audio_mode || "webrtc_strict"),
      "signaling: " + (d.signaling || extra.signaling || "unified_calls"),
      "token_mode: " + (d.token_mode || "server"),
      "attempt: " + (d.attempt_id || ""),
      "peer_generation: " + (d.peer_generation || 0),
      "failed_stage: " + (d.failed_stage ? (d.failed_stage + " " + stageName(d.failed_stage)) : "none"),
      "error: " + [d.error_name, d.error_message].filter(Boolean).join(" — "),
      "http_status: " + (d.http_status == null ? "—" : d.http_status),
      "pc.connectionState: " + ((d.pc && d.pc.connectionState) || "—"),
      "pc.iceConnectionState: " + ((d.pc && d.pc.iceConnectionState) || "—"),
      "pc.iceGatheringState: " + ((d.pc && d.pc.iceGatheringState) || "—"),
      "pc.signalingState: " + ((d.pc && d.pc.signalingState) || "—"),
      "dataChannel: " + (d.dc || "—"),
      "mic: " + JSON.stringify(d.mic || {}),
      "remote_tracks: " + (d.remote_tracks || 0),
      "audio_elements: " + (d.audio_elements || 0),
      "offer: " + JSON.stringify(d.offer || {}),
      "answer: " + JSON.stringify(d.answer || {}),
      "call_id: " + (d.call_id || ""),
    ];
    STAGES.forEach(function (row) {
      const st = (d.stages && d.stages[row[0]]) || {};
      lines.push(row[0] + " " + row[1] + " " + String(st.status || "pending").toUpperCase());
    });
    return lines.join("\n");
  }

  function pcStates(pc) {
    if (!pc) return {};
    return {
      connectionState: pc.connectionState,
      iceConnectionState: pc.iceConnectionState,
      iceGatheringState: pc.iceGatheringState,
      signalingState: pc.signalingState,
    };
  }

  function waitEvent(obj, ok, fail, timeoutMs, label) {
    if (ok()) return Promise.resolve();
    return new Promise(function (resolve, reject) {
      let done = false;
      const timer = window.setTimeout(function () {
        finish(new Error(label + " timeout"));
      }, timeoutMs);
      function finish(err) {
        if (done) return;
        done = true;
        window.clearTimeout(timer);
        off();
        if (err) reject(err);
        else resolve();
      }
      function onChange() {
        if (fail && fail()) {
          finish(new Error(label + " failed"));
          return;
        }
        if (ok()) finish();
      }
      const off = obj(onChange);
      onChange();
    });
  }

  function acquireProductionMic() {
    return navigator.mediaDevices.getUserMedia(PRODUCTION_MIC_CONSTRAINTS);
  }

  function MobileResponseController() {
    this.ownerTurn = 0;
    this.responseCreated = 0;
    this.responseDone = 0;
    this.lastResponseId = "";
    this.lastItemId = "";
    this.toolActive = false;
    this.lastAsr = "";
    this.lastAsrConfidence = null;
    this.lastCaption = "";
  }

  MobileResponseController.prototype.note = function note(msg) {
    const type = msg && msg.type;
    if (type === "input_audio_buffer.speech_started") this.ownerTurn += 1;
    if (type === "response.created") {
      this.responseCreated += 1;
      this.lastResponseId = msg.response && msg.response.id ? msg.response.id : (msg.response_id || "");
    }
    if (type === "response.done") this.responseDone += 1;
    if (type === "conversation.item.input_audio_transcription.completed") {
      this.lastAsr = msg.transcript || "";
      this.lastItemId = msg.item_id || "";
      this.lastAsrConfidence = logprobSummary(msg.logprobs);
    }
    if (type === "response.function_call_arguments.done") this.toolActive = true;
    if (type === "response.output_audio_transcript.done") {
      this.lastCaption = msg.transcript || msg.text || this.lastCaption;
      this.toolActive = false;
    }
  };

  function logprobSummary(logprobs) {
    if (!Array.isArray(logprobs) || !logprobs.length) return null;
    let sum = 0;
    let n = 0;
    for (let i = 0; i < logprobs.length; i += 1) {
      const row = logprobs[i];
      const v = typeof row === "number" ? row : (row && row.logprob);
      if (typeof v === "number") {
        sum += v;
        n += 1;
      }
    }
    if (!n) return null;
    const avg = sum / n;
    return Number(Math.max(0, Math.min(1, 1 + avg / 5)).toFixed(3));
  }

  function EvieWebRTC(opts) {
    this.api = opts.api;
    this.instanceId = opts.instanceId;
    this.audioEl = opts.audioEl;
    this.onState = opts.onState || function () {};
    this.onTranscript = opts.onTranscript || function () {};
    this.onCaption = opts.onCaption || function () {};
    this.onEnvelope = opts.onEnvelope || function () {};
    this.onCamera = opts.onCamera || function () {};
    this.onHud = opts.onHud || function () {};
    this.onHealth = opts.onHealth || function () {};
    this.pc = null;
    this.dc = null;
    this.mic = null;
    this.micTrack = null;
    this.remoteTrack = null;
    this.sessionId = null;
    this.poll = 0;
    this.statsTimer = 0;
    this.closed = true;
    this.playing = false;
    this.runtime = "IDLE";
    this.responses = new MobileResponseController();
    this.metrics = {
      ice: "idle",
      dc: "idle",
      backend: "webrtc",
      pcmFallback: "off",
      fallbackTts: "off",
      peerConnections: 0,
      audioSenders: 0,
      remoteAudioTracks: 0,
      extraRemoteTracks: 0,
      audioElements: 0,
      packetsSent: 0,
      packetsReceived: 0,
    };
    this.lastStats = { outbound: null, inbound: null, codec: null };
    this.captureInspect = { present: false };
    this.glitchMarks = [];
    this.generation = 0;
    this.attemptId = "";
    this.diag = new ConnectionDiag("");
    this.playBlocked = null;
    this.sessionCreated = false;
    this.sessionUpdated = false;
    this.sessionModel = "";
    this.signaling = "unified_calls";
  }

  EvieWebRTC.prototype._setRuntime = function _setRuntime(next) {
    this.runtime = next;
    this.onHealth(this.snapshot());
  };

  EvieWebRTC.prototype.start = async function start(opened, options) {
    const opts = options || {};
    const generation = this.generation + 1;
    this.stop();
    this.generation = generation;
    this.closed = false;
    this.sessionId = opened.session_id;
    this.responses = new MobileResponseController();
    this.sessionCreated = false;
    this.sessionUpdated = false;
    this.sessionModel = "";
    this.playBlocked = null;
    this.attemptId = nextAttemptId();
    this.signaling = opts.signaling || opened.signaling || "unified_calls";
    this.diag = new ConnectionDiag(this.attemptId);
    this.diag.peer_generation = generation;
    this.diag.signaling = this.signaling;
    this.diag.token_mode = this.signaling === "ephemeral_direct" ? "ephemeral" : "server";
    const self = this;
    const stillThis = function () { return !self.closed && self.generation === generation; };
    const fail = function (stage, err, extra) {
      const wrapped = err instanceof Error ? err : new Error(String(err || "failed"));
      self.diag.fail(stage, wrapped, extra);
      self.diag.pc = pcStates(self.pc);
      wrapped.failed_stage = stage;
      wrapped.diag = self.diag.snapshot();
      self._setRuntime("FAILED");
      throw wrapped;
    };

    this.diag.pass("M00", { gesture: true });
    if (!window.isSecureContext) fail("M01", new Error("Not a secure context"));
    this.diag.pass("M01");

    this.audioEl.autoplay = true;
    this.audioEl.setAttribute("playsinline", "true");
    this.audioEl.setAttribute("webkit-playsinline", "true");
    this.audioEl.volume = 1;
    this.audioEl.play().catch(function () { /* user-gesture unlock; real play is ontrack */ });

    this._setRuntime("ACQUIRING_MIC");
    try {
      this.mic = await acquireProductionMic();
    } catch (err) {
      const denied = err && (err.name === "NotAllowedError" || err.name === "PermissionDeniedError");
      fail(denied ? "M02" : "M03", err);
    }
    this.diag.pass("M02", { permission: "granted" });
    this.micTrack = this.mic.getAudioTracks()[0] || null;
    this.captureInspect = inspectTrack(this.micTrack);
    this.diag.mic = this.captureInspect;
    if (!this.micTrack || this.micTrack.readyState !== "live" || !this.micTrack.enabled) {
      fail("M03", new Error("Microphone track is not live"));
    }
    this.diag.pass("M03", { readyState: this.micTrack.readyState, enabled: this.micTrack.enabled });

    this.pc = new RTCPeerConnection();
    this.metrics.peerConnections = 1;
    this.diag.pass("M04");
    this.pc.onconnectionstatechange = function () {
      if (!stillThis()) return;
      self.metrics.ice = self.pc.connectionState;
      self.diag.pc = pcStates(self.pc);
      if (self.pc.connectionState === "connecting" || self.pc.iceConnectionState === "checking") {
        self.diag.pass("M13");
      }
      if (self.pc.connectionState === "failed") self._setRuntime("FAILED");
      self.onHealth(self.snapshot());
    };
    this.pc.oniceconnectionstatechange = function () {
      if (!stillThis() || !self.pc) return;
      self.diag.pc = pcStates(self.pc);
      if (self.pc.iceConnectionState === "connected" || self.pc.iceConnectionState === "completed") {
        self.diag.pass("M14");
      }
      self.onHealth(self.snapshot());
    };
    this.pc.ontrack = function (ev) {
      if (!stillThis() || !ev.track || ev.track.kind !== "audio") return;
      if (self.remoteTrack && self.remoteTrack !== ev.track && self.remoteTrack.readyState !== "ended") {
        self.metrics.extraRemoteTracks += 1;
        return;
      }
      self.remoteTrack = ev.track;
      self.metrics.remoteAudioTracks = 1;
      self.diag.remote_tracks = 1;
      self.diag.pass("M19", { kind: ev.track.kind, readyState: ev.track.readyState, streams: (ev.streams || []).length });
      const stream = new MediaStream([ev.track]);
      self.audioEl.srcObject = stream;
      self.audioEl.play().then(function () {
        self.playBlocked = null;
        self.diag.pass("M20");
        self.playing = true;
        self._maybeVoiceReady();
      }).catch(function (err) {
        self.playBlocked = err;
        self.playing = false;
        self.onState("audio_blocked");
        self._maybeVoiceReady();
      });
    };

    this.dc = this.pc.createDataChannel("oai-events");
    this.diag.dc = this.dc.readyState;
    this.diag.pass("M05", { label: this.dc.label, readyState: this.dc.readyState });
    this.dc.onopen = function () {
      if (!stillThis()) return;
      self.metrics.dc = "open";
      self.diag.dc = "open";
      self.diag.pass("M16");
      self._maybeVoiceReady();
    };
    this.dc.onclosing = function () { if (stillThis()) self.diag.dc = "closing"; };
    this.dc.onclose = function () { if (stillThis()) { self.metrics.dc = "closed"; self.diag.dc = "closed"; } };
    this.dc.onmessage = function (ev) { self._onProvider(parseEvent(ev.data)); };

    this.micTrack.onmute = function () { self.onHealth(self.snapshot()); };
    this.micTrack.onunmute = function () { self.onHealth(self.snapshot()); };
    this.micTrack.onended = function () {
      if (!stillThis()) return;
      self._setRuntime("FAILED");
      self.onState("mic_ended");
    };
    this.pc.addTrack(this.micTrack, this.mic);
    this.metrics.audioSenders = this.pc.getSenders().filter(function (s) {
      return s.track && s.track.kind === "audio";
    }).length;
    if (this.metrics.audioSenders !== 1) fail("M06", new Error("Expected one audio sender"));
    this.diag.pass("M06", { senders: this.metrics.audioSenders });
    this.metrics.audioElements = 1;
    this._setRuntime("SIGNALING");

    const offer = await this.pc.createOffer();
    if (!offer || !offer.sdp) fail("M07", new Error("createOffer returned empty SDP"));
    this.diag.offer = summarizeSdp(offer.sdp);
    if (!this.diag.offer.audio_mline || this.diag.offer.direction === "recvonly") {
      fail("M07", new Error("Offer is missing send audio"));
    }
    if (!this.diag.offer.application_mline) fail("M07", new Error("Offer is missing the data channel"));
    this.diag.pass("M07", this.diag.offer);
    await this.pc.setLocalDescription(offer);
    this.diag.pass("M08");
    const localSdp = (this.pc.localDescription && this.pc.localDescription.sdp) || offer.sdp;
    this.diag.offer = summarizeSdp(localSdp);
    this.diag.offer.sha256 = await sha256Hex(localSdp);
    this._setRuntime("CONNECTING_MEDIA");

    let answerSdp = "";
    let answerMeta = {};
    try {
      this.diag.pass("M09");
      if (this.signaling === "ephemeral_direct") {
        const minted = await this.api("/v1/device-gateway/live/webrtc/client-secret", {
          method: "POST",
          body: JSON.stringify({
            instance_id: this.instanceId,
            session_id: this.sessionId,
            attempt_id: this.attemptId,
          }),
        });
        const callsUrl = minted.calls_url;
        if (!callsUrl || !minted.value) fail("M09", new Error("Ephemeral credential missing"));
        const sdpRes = await fetch(callsUrl, {
          method: "POST",
          headers: {
            Authorization: "Bearer " + minted.value,
            "Content-Type": "application/sdp",
            Accept: "application/sdp",
          },
          body: localSdp,
        });
        const raw = await sdpRes.text();
        this.diag.http_status = sdpRes.status;
        this.diag.call_id = (sdpRes.headers.get("location") || "").split("/").pop() || "";
        if (!sdpRes.ok) {
          const err = new Error("Realtime signaling failed.");
          err.status = sdpRes.status;
          err.provider_status = sdpRes.status;
          fail("M10", err, { http_status: sdpRes.status });
        }
        this.diag.pass("M10", { http_status: sdpRes.status });
        answerSdp = raw;
      } else {
        const answer = await this.api("/v1/device-gateway/live/webrtc/sdp", {
          method: "POST",
          body: JSON.stringify({
            instance_id: this.instanceId,
            session_id: this.sessionId,
            sdp: localSdp,
            attempt_id: this.attemptId,
          }),
        });
        answerSdp = answer.sdp;
        this.diag.http_status = answer.provider_status || 201;
        this.diag.call_id = answer.call_id || "";
        this.diag.pass("M10", { http_status: this.diag.http_status, call_id: String(this.diag.call_id).slice(0, 20) });
        answerMeta = {
          offer_sha256: answer.offer_sha256,
          answer_sha256: answer.answer_sha256,
        };
      }
    } catch (err) {
      const stage = (err && err.failed_stage) || (err && err.status ? "M10" : "M09");
      fail(stage, err, { http_status: err && (err.provider_status || err.status) });
    }
    if (!String(answerSdp || "").replace(/^\s+/, "").startsWith("v=")) {
      fail("M11", new Error("SDP answer missing"));
    }
    this.diag.answer = summarizeSdp(answerSdp);
    this.diag.answer.sha256 = await sha256Hex(answerSdp);
    if (answerMeta.offer_sha256 && this.diag.offer.sha256 && answerMeta.offer_sha256 !== this.diag.offer.sha256) {
      this.diag.answer.proxy_mutated_offer = true;
    }
    this.diag.pass("M11", this.diag.answer);
    try {
      await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    } catch (err) {
      fail("M12", err);
    }
    this.diag.pass("M12");
    this.diag.pass("M13");
    this._pollEvents();
    this._pollStats();

    try {
      await waitEvent(
        function (cb) {
          self.pc.addEventListener("iceconnectionstatechange", cb);
          self.pc.addEventListener("connectionstatechange", cb);
          return function () {
            self.pc.removeEventListener("iceconnectionstatechange", cb);
            self.pc.removeEventListener("connectionstatechange", cb);
          };
        },
        function () {
          const ice = self.pc && (self.pc.iceConnectionState === "connected" || self.pc.iceConnectionState === "completed");
          const conn = self.pc && self.pc.connectionState === "connected";
          return !!(ice || conn);
        },
        function () { return self.pc && (self.pc.iceConnectionState === "failed" || self.pc.connectionState === "failed"); },
        20000,
        "ICE"
      );
    } catch (err) {
      const iceFail = self.pc && (self.pc.iceConnectionState === "failed" || self.pc.connectionState === "failed");
      fail(iceFail ? "M14" : "M15", err, { pc: pcStates(self.pc) });
    }
    this.diag.pass("M14");
    this.diag.pass("M15", pcStates(this.pc));

    try {
      await waitEvent(
        function (cb) {
          self.dc.addEventListener("open", cb);
          return function () { self.dc.removeEventListener("open", cb); };
        },
        function () { return self.dc && self.dc.readyState === "open"; },
        function () { return self.dc && self.dc.readyState === "closed"; },
        12000,
        "DataChannel"
      );
    } catch (err) {
      fail("M16", err);
    }
    this.diag.pass("M16");

    try {
      await waitEvent(
        function (cb) {
          const t = window.setInterval(cb, 80);
          return function () { window.clearInterval(t); };
        },
        function () { return self.sessionCreated; },
        null,
        12000,
        "session.created"
      );
    } catch (err) {
      fail("M17", err);
    }
    this.diag.pass("M17", { model: this.sessionModel });
    if (this.sessionUpdated) this.diag.pass("M18");
    else this.diag.pass("M18", { implied: true });

    try {
      await waitEvent(
        function (cb) {
          const t = window.setInterval(cb, 80);
          return function () { window.clearInterval(t); };
        },
        function () { return !!(self.remoteTrack && self.remoteTrack.readyState !== "ended"); },
        null,
        12000,
        "remote audio"
      );
    } catch (err) {
      fail("M19", err);
    }
    this.diag.pass("M19");
    if (!this.playBlocked) this.diag.pass("M20");
    this._maybeVoiceReady();
    if (this.playBlocked) {
      const blocked = new Error("Voice connected — tap to enable audio");
      blocked.failed_stage = "M20";
      blocked.audio_blocked = true;
      blocked.diag = this.diag.snapshot();
      this.onState("audio_blocked");
      throw blocked;
    }
    return { settings: this.captureInspect, diag: this.diag.snapshot(), attempt_id: this.attemptId };
  };

  EvieWebRTC.prototype._maybeVoiceReady = function _maybeVoiceReady() {
    if (this.closed || this.runtime === "FAILED") return;
    const micLive = !!(this.micTrack && this.micTrack.readyState === "live" && this.micTrack.enabled);
    const pcOk = this.pc && (this.pc.connectionState === "connected" || this.pc.iceConnectionState === "connected" || this.pc.iceConnectionState === "completed");
    const dcOk = this.dc && this.dc.readyState === "open";
    const remote = !!(this.remoteTrack && this.remoteTrack.readyState !== "ended");
    if (micLive && pcOk && dcOk && remote && this.sessionCreated) {
      this.diag.pass("M21");
      if (this.runtime !== "EVIE_SPEAKING" && this.runtime !== "OWNER_SPEAKING" && this.runtime !== "PROCESSING" && this.runtime !== "TOOL_RUNNING") {
        this._setRuntime("VOICE_READY");
        this.onState(this.playBlocked ? "audio_blocked" : "listening");
      }
    }
    this.onHealth(this.snapshot());
  };

  EvieWebRTC.prototype._onProvider = function _onProvider(msg) {
    if (!msg || this.closed) return;
    this.responses.note(msg);
    const type = msg.type || "";
    if (type === "session.created") {
      this.sessionCreated = true;
      this.sessionModel = (msg.session && msg.session.model) || this.sessionModel;
      this.diag.pass("M17", { model: this.sessionModel });
      this._maybeVoiceReady();
    }
    if (type === "session.updated") {
      this.sessionUpdated = true;
      this.sessionModel = (msg.session && msg.session.model) || this.sessionModel;
      this.diag.pass("M18", { model: this.sessionModel });
    }
    if (type === "input_audio_buffer.speech_started") {
      this._setRuntime("OWNER_SPEAKING");
      this.onState("listening");
    }
    if (type === "input_audio_buffer.speech_stopped") {
      this._setRuntime("PROCESSING");
      this.onState("thinking");
    }
    if (type === "conversation.item.input_audio_transcription.completed" && msg.transcript) {
      this.onTranscript(msg.transcript, {
        itemId: msg.item_id || "",
        confidence: this.responses.lastAsrConfidence,
        label: "TRANSCRIPT",
      });
      this._setRuntime("PROCESSING");
      this.onState("thinking");
    }
    if (type === "response.output_audio_transcript.delta" && msg.delta) this.onCaption(msg.delta, false);
    if (type === "response.output_audio_transcript.done" && (msg.transcript || msg.text)) {
      this.onCaption(msg.transcript || msg.text, true);
    }
    if (type === "output_audio_buffer.started" || type === "response.output_audio.delta") {
      this.playing = true;
      this._setRuntime("EVIE_SPEAKING");
      this.onState("speaking");
    }
    if (type === "output_audio_buffer.stopped") {
      this.playing = false;
      if (!this.closed) {
        this._setRuntime("VOICE_READY");
        this.onState("listening");
      }
    }
    if (type === "response.function_call_arguments.done") {
      this._setRuntime("TOOL_RUNNING");
      this._tool(msg);
    }
    if (type === "error" && msg.error) this.onCaption(String(msg.error.message || "Voice error"), true);
    this.onHealth(this.snapshot());
  };

  EvieWebRTC.prototype._tool = async function _tool(msg) {
    let args = {};
    try { args = JSON.parse(msg.arguments || "{}"); } catch (_err) { args = {}; }
    this.onHud({ kind: "progress", name: msg.name });
    try {
      const result = await this.api("/v1/device-gateway/live/tool", {
        method: "POST",
        body: JSON.stringify({
          instance_id: this.instanceId,
          session_id: this.sessionId,
          name: msg.name,
          call_id: msg.call_id,
          arguments: args,
        }),
      });
      this._send({
        type: "conversation.item.create",
        item: { type: "function_call_output", call_id: msg.call_id, output: result.output || "{}" },
      });
      this._send({ type: "response.create" });
      let parsed = {};
      try { parsed = JSON.parse(result.output || "{}"); } catch (_err) { parsed = {}; }
      if (parsed.card && window.EvieMobileActions) window.EvieMobileActions.present(parsed);
      this.onHud({ kind: "result", name: msg.name, ok: true, phone_action: parsed });
    } catch (err) {
      this._send({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: msg.call_id,
          output: JSON.stringify({ ok: false, spoken: String(err.message || "tool failed") }),
        },
      });
      this._send({ type: "response.create" });
      this.onHud({ kind: "result", name: msg.name, ok: false });
    }
  };

  EvieWebRTC.prototype._send = function _send(obj) {
    if (this.dc && this.dc.readyState === "open") this.dc.send(JSON.stringify(obj));
  };

  EvieWebRTC.prototype.commitTurn = function commitTurn() {
    this._send({ type: "input_audio_buffer.commit" });
    this._send({ type: "response.create" });
  };

  EvieWebRTC.prototype.perceptionProbe = function perceptionProbe() {
    this._send({
      type: "response.create",
      response: {
        output_modalities: ["text"],
        instructions:
          "Reply with JSON only, no speech: {\"heard\":\"<what the owner just asked>\",\"intent\":\"<short intent>\"}.",
      },
    });
  };

  EvieWebRTC.prototype._pollEvents = function _pollEvents() {
    const self = this;
    const tick = async function () {
      if (self.closed || !self.sessionId) return;
      try {
        const body = await self.api("/v1/device-gateway/live/events?session_id=" + encodeURIComponent(self.sessionId));
        const events = (body && body.events) || [];
        for (let i = 0; i < events.length; i += 1) {
          const ev = events[i];
          if (ev.type === "camera_request") await self.onCamera(ev);
          if (ev.type === "hud") self.onHud(ev);
          if (ev.type === "conversation_moved") self.onState("moved");
        }
      } catch (_err) { /* poll is best-effort */ }
      if (!self.closed) self.poll = window.setTimeout(tick, 280);
    };
    this.poll = window.setTimeout(tick, 200);
  };

  EvieWebRTC.prototype._pollStats = function _pollStats() {
    const self = this;
    const tick = async function () {
      if (self.closed || !self.pc) return;
      try {
        const report = await self.pc.getStats();
        self.lastStats = sanitizeStats(report);
        if (self.lastStats.outbound) self.metrics.packetsSent = self.lastStats.outbound.packetsSent;
        if (self.lastStats.inbound) self.metrics.packetsReceived = self.lastStats.inbound.packetsReceived;
        const amp = self.lastStats.inbound && self.lastStats.inbound.audioLevel;
        if (typeof amp === "number") self.onEnvelope(Math.min(1, amp * 4));
        self.onHealth(self.snapshot());
      } catch (_err) { /* Safari may omit fields */ }
      if (!self.closed) self.statsTimer = window.setTimeout(tick, 1000);
    };
    this.statsTimer = window.setTimeout(tick, 400);
  };

  EvieWebRTC.prototype.markGlitch = function markGlitch() {
    this.glitchMarks.push({ at: Date.now(), stats: this.lastStats, runtime: this.runtime });
    return this.glitchMarks[this.glitchMarks.length - 1];
  };

  EvieWebRTC.prototype.snapshot = function snapshot() {
    const micLive = !!(this.micTrack && this.micTrack.readyState === "live" && this.micTrack.enabled);
    const health = voiceHealth({
      micActive: micLive,
      micReadyState: this.micTrack ? this.micTrack.readyState : "none",
      audioSenders: this.metrics.audioSenders,
      peerConnections: this.pc ? 1 : 0,
      remoteAudioTracks: this.remoteTrack && this.remoteTrack.readyState !== "ended" ? 1 : 0,
      audioElements: 1,
      pcmFallback: "off",
      fallbackTts: "off",
      ice: this.metrics.ice,
      failed: this.runtime === "FAILED",
      sessionActive: !this.closed && !!this.sessionId,
      asr: this.responses.lastAsr ? "GOOD" : "UNCERTAIN",
      playbackOwner: this.closed ? "NONE" : "THIS_PHONE",
    });
    return {
      runtime: this.runtime,
      health: health,
      connection: this.diag ? this.diag.snapshot() : null,
      attempt_id: this.attemptId,
      signaling: this.signaling,
      session_model: this.sessionModel,
      capture: this.captureInspect,
      metrics: this.metrics,
      stats: this.lastStats,
      responses: {
        ownerTurns: this.responses.ownerTurn,
        created: this.responses.responseCreated,
        done: this.responses.responseDone,
        lastResponseId: abbreviateId(this.responses.lastResponseId),
        lastItemId: abbreviateId(this.responses.lastItemId),
        lastAsr: this.responses.lastAsr,
        lastAsrConfidence: this.responses.lastAsrConfidence,
      },
      extraRemoteTracks: this.metrics.extraRemoteTracks,
      audioDomCount: countAudioElements(),
    };
  };

  EvieWebRTC.prototype.enableAudio = function enableAudio() {
    const self = this;
    return this.audioEl.play().then(function () {
      self.playBlocked = null;
      self.diag.pass("M20");
      self.playing = true;
      self._maybeVoiceReady();
    });
  };

  EvieWebRTC.prototype.cancel = function cancel() {
    this._send({ type: "response.cancel" });
  };

  EvieWebRTC.prototype.stop = function stop() {
    this.closed = true;
    this.playing = false;
    this.generation += 1;
    this._setRuntime("ENDED");
    if (this.poll) window.clearTimeout(this.poll);
    this.poll = 0;
    if (this.statsTimer) window.clearTimeout(this.statsTimer);
    this.statsTimer = 0;
    if (this.dc) {
      try { this.dc.close(); } catch (_err) { /* already closed */ }
    }
    if (this.pc) {
      try { this.pc.close(); } catch (_err2) { /* already closed */ }
    }
    if (this.mic) this.mic.getTracks().forEach(function (t) { t.stop(); });
    this.remoteTrack = null;
    if (this.audioEl) {
      this.audioEl.pause();
      this.audioEl.srcObject = null;
    }
    this.pc = null;
    this.dc = null;
    this.mic = null;
    this.micTrack = null;
    this.metrics.peerConnections = 0;
    this.metrics.audioSenders = 0;
    this.metrics.remoteAudioTracks = 0;
  };

  async function recordStream(stream, seconds, opts) {
    const src = stream.getAudioTracks()[0];
    const inspect = inspectTrack(src);
    const track = opts && opts.clone && src.clone ? src.clone() : src;
    const recStream = track === src ? stream : new MediaStream([track]);
    const mime = MediaRecorder.isTypeSupported("audio/mp4")
      ? "audio/mp4"
      : (MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "");
    const chunks = [];
    const rec = new MediaRecorder(recStream, mime ? { mimeType: mime } : undefined);
    rec.ondataavailable = function (ev) {
      if (ev.data && ev.data.size) chunks.push(ev.data);
    };
    const stopped = new Promise(function (resolve) {
      rec.onstop = function () { resolve(); };
    });
    rec.start();
    await new Promise(function (resolve) { setTimeout(resolve, Math.round((seconds || 8) * 1000)); });
    rec.stop();
    await stopped;
    if (track !== src) track.stop();
    const blob = new Blob(chunks, { type: rec.mimeType || mime || "audio/mp4" });
    return { blob: blob, inspect: inspect, mime: blob.type, liveTrack: !!(opts && opts.live) };
  }

  async function recordProductionMic(seconds) {
    const stream = await acquireProductionMic();
    try {
      const result = await recordStream(stream, seconds);
      result.liveTrack = false;
      return result;
    } finally {
      stream.getTracks().forEach(function (t) { t.stop(); });
    }
  }

  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      const reader = new FileReader();
      reader.onload = function () {
        const s = String(reader.result || "");
        resolve(s.indexOf(",") >= 0 ? s.split(",")[1] : s);
      };
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
  }

  const api = {
    PRODUCTION_MIC_CONSTRAINTS: PRODUCTION_MIC_CONSTRAINTS,
    STATES: STATES,
    STAGES: STAGES,
    RUNTIME_VERSION: RUNTIME_VERSION,
    summarizeSdp: summarizeSdp,
    formatConnectionDiag: formatConnectionDiag,
    nextAttemptId: nextAttemptId,
    ConnectionDiag: ConnectionDiag,
    inspectTrack: inspectTrack,
    classifyLevel: classifyLevel,
    measurePcm16: measurePcm16,
    exclusivePlaybackIllegal: exclusivePlaybackIllegal,
    sanitizeStats: sanitizeStats,
    voiceHealth: voiceHealth,
    noncePhrase: noncePhrase,
    acquireProductionMic: acquireProductionMic,
    recordStream: recordStream,
    recordProductionMic: recordProductionMic,
    blobToBase64: blobToBase64,
    MobileResponseController: MobileResponseController,
    EvieWebRTC: EvieWebRTC,
  };

  root.EvieWebRTC = EvieWebRTC;
  root.EvieMobileVoice = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
