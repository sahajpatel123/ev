/* EvieAudioPlaybackEngine v3 — PCM fallback. Production default is WebRTC. */
(function (root) {
  const AUDIO_ENGINE_VERSION = "4";
  const INPUT_RATE = 16000;
  // Cold-start prime: ~220 ms of real audio before the first word. The
  // provider streams at ~1x realtime, so the sustainable riding lead can
  // never exceed what was banked before play starts. A 90 ms prime rode at
  // ~90 ms and every routine arrival jitter ran the worklet dry — chronic
  // early-sentence stutter on EVERY response (then the adapt loop masks it
  // late). 220 ms absorbs normal jitter for ~130 ms of extra first-word
  // latency. Mid-response restarts never re-prime (the worklet stays primed
  // across transient starves); only true provider gaps re-buffer.
  const PRIME_S = 0.22;
  const SLIP_S = 0.012;
  const JITTER_MIN_S = 0.06;
  const JITTER_MAX_S = 0.28;

  function nextPlayTime(ctxNow, queuedUntil, duration, primeS) {
    const prime = primeS == null ? PRIME_S : primeS;
    const dur = Math.max(0.001, Number(duration) || 0.02);
    const now = Number(ctxNow) || 0;
    if (!queuedUntil) {
      const start = now + prime;
      return { start: start, queuedUntil: start + dur, underrun: false, first: true };
    }
    if (queuedUntil < now + SLIP_S) {
      const start = now + SLIP_S;
      return { start: start, queuedUntil: start + dur, underrun: true, first: false };
    }
    return { start: queuedUntil, queuedUntil: queuedUntil + dur, underrun: false, first: false };
  }

  function concatBytes(a, b) {
    if (!a || !a.length) return b;
    if (!b || !b.length) return a;
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function int16BytesToFloat32(bytes, remainder) {
    const incoming = bytes instanceof Uint8Array
      ? bytes
      : new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const raw = concatBytes(remainder || new Uint8Array(0), incoming);
    const even = raw.byteLength - (raw.byteLength % 2);
    const leftover = even < raw.byteLength ? raw.slice(even) : new Uint8Array(0);
    const aligned = new ArrayBuffer(even);
    new Uint8Array(aligned).set(raw.subarray(0, even));
    const samples = new Int16Array(aligned);
    const out = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      out[i] = samples[i] / 32768;
    }
    return { samples: out, remainder: leftover };
  }

  function pcm16ToFloat32(bytes, srcRate, dstRate) {
    const decoded = int16BytesToFloat32(bytes);
    const mono = decoded.samples;
    if (!mono.length) return new Float32Array(1);
    if (!dstRate || srcRate === dstRate) return mono;
    const resampler = new LinearResampler(srcRate, dstRate);
    return resampler.process(mono);
  }

  function maxAdjacentJump(samples) {
    let peak = 0;
    for (let i = 1; i < samples.length; i += 1) {
      const jump = Math.abs(samples[i] - samples[i - 1]);
      if (jump > peak) peak = jump;
    }
    return peak;
  }

  function LinearResampler(srcRate, dstRate) {
    this.srcRate = srcRate;
    this.dstRate = dstRate;
    this.step = srcRate / dstRate;
    this.phase = 0;
    this.carry = new Float32Array(0);
  }

  LinearResampler.prototype.reset = function reset() {
    this.phase = 0;
    this.carry = new Float32Array(0);
  };

  LinearResampler.prototype.process = function process(input) {
    if (!input || !input.length) return new Float32Array(0);
    if (Math.abs(this.srcRate - this.dstRate) < 0.01) {
      const copy = new Float32Array(input.length);
      copy.set(input);
      return copy;
    }
    const data = new Float32Array(this.carry.length + input.length);
    data.set(this.carry, 0);
    data.set(input, this.carry.length);
    const out = [];
    let pos = this.phase;
    while (pos + 1 < data.length) {
      const i = pos | 0;
      const f = pos - i;
      out.push(data[i] + (data[i + 1] - data[i]) * f);
      pos += this.step;
    }
    const keep = Math.max(0, pos | 0);
    this.carry = data.slice(keep);
    this.phase = pos - keep;
    const result = new Float32Array(out.length);
    for (let i = 0; i < out.length; i += 1) result[i] = out[i];
    return result;
  };

  function RingBuffer(capacity) {
    this.buf = new Float32Array(Math.max(8, capacity | 0));
    this.w = 0;
    this.r = 0;
    this.count = 0;
  }

  RingBuffer.prototype.clear = function clear() {
    this.w = 0;
    this.r = 0;
    this.count = 0;
  };

  RingBuffer.prototype.available = function available() {
    return this.count;
  };

  RingBuffer.prototype.push = function push(samples) {
    const cap = this.buf.length;
    let overflow = 0;
    for (let i = 0; i < samples.length; i += 1) {
      if (this.count >= cap - 1) {
        overflow += 1;
        continue;
      }
      this.buf[this.w] = samples[i];
      this.w = (this.w + 1) % cap;
      this.count += 1;
    }
    this.overflow = (this.overflow || 0) + overflow;
    return overflow;
  };

  RingBuffer.prototype.shift = function shift(n) {
    const take = Math.min(n, this.count);
    const out = new Float32Array(take);
    for (let i = 0; i < take; i += 1) {
      out[i] = this.buf[this.r];
      this.r = (this.r + 1) % this.buf.length;
    }
    this.count -= take;
    return out;
  };

  function JitterController() {
    this.targetS = PRIME_S;
    this.minS = JITTER_MIN_S;
    this.maxS = JITTER_MAX_S;
    this.lastArrive = 0;
    this.deltas = [];
    this.underruns = 0;
    this.locked = false;
  }

  JitterController.prototype.noteArrival = function noteArrival(nowMs, expectedMs) {
    if (this.lastArrive) {
      const delta = nowMs - this.lastArrive;
      this.deltas.push(delta);
      if (this.deltas.length > 40) this.deltas.shift();
    }
    this.lastArrive = nowMs;
    return expectedMs;
  };

  JitterController.prototype.p95 = function p95() {
    if (!this.deltas.length) return 20;
    const sorted = this.deltas.slice().sort(function (a, b) { return a - b; });
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];
  };

  JitterController.prototype.mean = function mean() {
    if (!this.deltas.length) return 20;
    let sum = 0;
    for (let i = 0; i < this.deltas.length; i += 1) sum += this.deltas[i];
    return sum / this.deltas.length;
  };

  JitterController.prototype.adapt = function adapt(underran) {
    if (underran) this.underruns += 1;
    if (this.locked && !underran) return this.targetS;
    const p95 = this.p95();
    const want = Math.min(this.maxS, Math.max(this.minS, 0.05 + (p95 / 1000) * 1.6));
    if (underran) {
      this.targetS = Math.min(this.maxS, this.targetS + 0.02);
    } else if (Math.abs(want - this.targetS) > 0.025) {
      this.targetS += (want - this.targetS) * 0.15;
    }
    this.targetS = Math.min(this.maxS, Math.max(this.minS, this.targetS));
    return this.targetS;
  };

  JitterController.prototype.beginResponse = function beginResponse() {
    this.locked = true;
    this.lastArrive = 0;
  };

  JitterController.prototype.endResponse = function endResponse() {
    this.locked = false;
    this.adapt(false);
  };

  function EvieAudioPlaybackEngine() {
    this.generation = 0;
    this.socketGeneration = 0;
    this.responseId = null;
    this.ctx = null;
    this.ctxId = 0;
    this.nextTime = 0;
    this.nodes = [];
    this.seen = {};
    this.lastSeq = -1;
    this.playing = false;
    this.envelope = 0;
    this.halfDuplex = false;
    this.onPlayingChange = null;
    this.onEnvelope = null;
    this.resampler = null;
    this.jitter = new JitterController();
    this.node = null;
    this.gain = null;
    this.backend = "uninitialized";
    this.sourceRate = INPUT_RATE;
    this._byteRemainder = new Uint8Array(0);
    this._batch = [];
    this._batchFrames = 0;
    this.metrics = {
      chunks: 0,
      duplicateDropped: 0,
      staleDropped: 0,
      outOfOrderDropped: 0,
      lateChunks: 0,
      underruns: 0,
      starveFrames: 0,
      responses: 0,
      contexts: 0,
      clips: 0,
      peak: 0,
      gaps: 0,
      overlaps: 0,
      overflows: 0,
      oddByteRemnants: 0,
      workletBatches: 0,
      playbackBackend: "uninitialized",
      jitterTargetMs: PRIME_S * 1000,
      jitterP95Ms: 0,
      jitterMeanMs: 0,
      contextSampleRate: 0,
      sourceSampleRate: INPUT_RATE,
      baseLatency: null,
      outputLatency: null,
    };
  }

  EvieAudioPlaybackEngine.prototype.ensure = async function ensure() {
    if (this.ctx && this.ctx.state !== "closed") {
      if (this.ctx.state === "suspended") await this.ctx.resume();
      return this.ctx;
    }
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    this.ctxId += 1;
    this.metrics.contexts += 1;
    this.metrics.contextSampleRate = this.ctx.sampleRate;
    this.metrics.baseLatency = this.ctx.baseLatency != null ? this.ctx.baseLatency : null;
    this.metrics.outputLatency = this.ctx.outputLatency != null ? this.ctx.outputLatency : null;
    this.resampler = new LinearResampler(INPUT_RATE, this.ctx.sampleRate);
    this.gain = this.ctx.createGain();
    this.gain.gain.value = 1;
    this.gain.connect(this.ctx.destination);
    if (this.ctx.state === "suspended") await this.ctx.resume();
    await this._attachWorklet();
    return this.ctx;
  };

  EvieAudioPlaybackEngine.prototype._attachWorklet = async function _attachWorklet() {
    if (!this.ctx.audioWorklet) {
      this.backend = "scheduled-buffer-fallback";
      this.metrics.playbackBackend = this.backend;
      return;
    }
    try {
      await this.ctx.audioWorklet.addModule("/evie/playback-worklet.js");
      this.node = new AudioWorkletNode(this.ctx, "evie-playback", {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      const self = this;
      this.node.port.onmessage = function (ev) {
        const msg = ev.data || {};
        if (msg.type !== "stats") return;
        self.metrics.underruns = msg.underruns || 0;
        self.metrics.starveFrames = msg.starveFrames || 0;
        self.metrics.overflows = msg.overflows || 0;
        self.envelope = Math.min(1, (msg.rms || 0) * 3.4);
        if (self.onEnvelope) self.onEnvelope(self.envelope);
        const starved = msg.underruns > self.jitter.underruns;
        if (starved) self.jitter.adapt(true);
        self.metrics.jitterTargetMs = self.jitter.targetS * 1000;
        if (!msg.primed && self.playing && msg.available === 0) {
          self._emitPlaying(false);
          self.playing = false;
        }
      };
      this.node.connect(this.gain);
      this.backend = "worklet-ring-buffer";
      this.metrics.playbackBackend = this.backend;
    } catch (_err) {
      this.backend = "scheduled-buffer-fallback";
      this.metrics.playbackBackend = this.backend;
    }
  };

  EvieAudioPlaybackEngine.prototype.beginTurn = function beginTurn(meta) {
    this.stopScheduled();
    this.generation += 1;
    this.responseId = (meta && meta.responseId) || null;
    this.socketGeneration = (meta && meta.socketGeneration) || this.socketGeneration;
    this.nextTime = 0;
    this.seen = {};
    this.lastSeq = -1;
    this.metrics.responses += 1;
    this.jitter.beginResponse();
    this._byteRemainder = new Uint8Array(0);
    this._batch = [];
    this._batchFrames = 0;
    if (this.resampler) this.resampler.reset();
    if (this.node) {
      this.node.port.postMessage({ type: "flush" });
      this.node.port.postMessage({
        type: "target",
        frames: Math.floor((this.ctx ? this.ctx.sampleRate : 48000) * this.jitter.targetS),
      });
      this.node.port.postMessage({ type: "start" });
    }
    return this.generation;
  };

  // Tool-continuation adopt: a new provider response id for the SAME spoken
  // turn (preamble + tool gap + continuation) must NOT flush queued audio.
  // Flushing drops the still-queued preamble tail, resets the resampler and
  // re-primes from zero — a chop plus fade restart on every tool turn, and
  // on any back-to-back responses whose tail is still draining. Adopt only
  // retargets the ids and ensures the worklet is running; the PCM appends
  // to the one audible stream.
  EvieAudioPlaybackEngine.prototype.adoptTurn = function adoptTurn(meta) {
    if (meta && meta.responseId) this.responseId = meta.responseId;
    if (meta && meta.socketGeneration != null) this.socketGeneration = meta.socketGeneration;
    // The continuation restarts chunk seq at 0: without clearing the dedup
    // map every continuation chunk would be dropped as a "duplicate" and the
    // answer after a tool call would go silent. Same-stream state (queued
    // PCM, batches, resampler phase, generation) is deliberately preserved.
    this.seen = {};
    this.lastSeq = -1;
    this.jitter.beginResponse();
    if (this.node) this.node.port.postMessage({ type: "start" });
    return this.generation;
  };

  EvieAudioPlaybackEngine.prototype.setSocketGeneration = function setSocketGeneration(gen) {
    if (gen === this.socketGeneration) return;
    this.socketGeneration = gen;
    this.stop();
  };

  EvieAudioPlaybackEngine.prototype.stopScheduled = function stopScheduled() {
    this.nodes.forEach(function (node) {
      try { node.stop(); } catch (_err) { /* already stopped */ }
      try { node.disconnect(); } catch (_err2) { /* closed */ }
    });
    this.nodes = [];
    this.nextTime = 0;
    this.playing = false;
    this.envelope = 0;
    this._batch = [];
    this._batchFrames = 0;
    if (this.node) this.node.port.postMessage({ type: "flush" });
  };

  EvieAudioPlaybackEngine.prototype.stop = function stop() {
    this.generation += 1;
    this.responseId = null;
    this.jitter.endResponse();
    this.stopScheduled();
    this._emitPlaying(false);
  };

  EvieAudioPlaybackEngine.prototype.flushReconnect = function flushReconnect() {
    this.stop();
  };

  EvieAudioPlaybackEngine.prototype.endStream = function endStream() {
    this._flushWorklet();
    if (this.node) this.node.port.postMessage({ type: "end" });
    this.jitter.endResponse();
  };

  EvieAudioPlaybackEngine.prototype._emitPlaying = function _emitPlaying(active) {
    if (this.onPlayingChange) this.onPlayingChange(!!active);
  };

  EvieAudioPlaybackEngine.prototype._accept = function _accept(payload) {
    const seq = payload.seq;
    const generation = payload.generation;
    const socketGeneration = payload.socketGeneration;
    if (socketGeneration != null && socketGeneration !== this.socketGeneration) {
      this.metrics.staleDropped += 1;
      return false;
    }
    if (payload.responseId && payload.responseId !== this.responseId) {
      if (this.playing) {
        // Audible audio from the previous id is still draining: this is a
        // tool continuation (or back-to-back turn), not a new turn. Adopt
        // instead of flushing, or the queued tail is dropped mid-word.
        this.adoptTurn({
          responseId: payload.responseId,
          socketGeneration: socketGeneration,
        });
      } else {
        this.beginTurn({
          responseId: payload.responseId,
          socketGeneration: socketGeneration,
        });
      }
    } else if (!this.generation) {
      this.beginTurn({ socketGeneration: socketGeneration });
    }
    if (generation != null && generation !== this.generation) {
      this.metrics.staleDropped += 1;
      return false;
    }
    if (seq != null && this.seen[seq]) {
      this.metrics.duplicateDropped += 1;
      return false;
    }
    if (seq != null && this.lastSeq >= 0 && seq < this.lastSeq) {
      this.metrics.outOfOrderDropped += 1;
      return false;
    }
    if (seq != null) {
      this.seen[seq] = true;
      this.lastSeq = seq;
    }
    return true;
  };

  EvieAudioPlaybackEngine.prototype.enqueuePcm16 = async function enqueuePcm16(payload) {
    if (!this._accept(payload)) return false;
    const now = typeof performance !== "undefined" ? performance.now() : Date.now();
    this.jitter.noteArrival(now, 20);
    this.metrics.jitterP95Ms = this.jitter.p95();
    this.metrics.jitterMeanMs = this.jitter.mean();
    const ctx = await this.ensure();
    const sourceRate = payload.sampleRate || INPUT_RATE;
    this.sourceRate = sourceRate;
    this.metrics.sourceSampleRate = sourceRate;
    if (!this.resampler || this.resampler.srcRate !== sourceRate || this.resampler.dstRate !== ctx.sampleRate) {
      this.resampler = new LinearResampler(sourceRate, ctx.sampleRate);
    }
    const decoded = int16BytesToFloat32(payload.bytes, this._byteRemainder);
    this._byteRemainder = decoded.remainder;
    if (decoded.remainder && decoded.remainder.length) this.metrics.oddByteRemnants += 1;
    const mono = decoded.samples;
    let peak = 0;
    for (let i = 0; i < mono.length; i += 1) {
      const mag = Math.abs(mono[i]);
      if (mag > peak) peak = mag;
      if (mag >= 0.999) this.metrics.clips += 1;
    }
    if (peak > this.metrics.peak) this.metrics.peak = peak;
    const resampled = this.resampler.process(mono);
    this.metrics.chunks += 1;
    if (this.backend === "worklet-ring-buffer" && this.node) {
      this._queueWorklet(resampled);
      if (!this.playing) {
        this.playing = true;
        this._emitPlaying(true);
      }
      return true;
    }
    return this._enqueueFallback(resampled, ctx);
  };

  EvieAudioPlaybackEngine.prototype._queueWorklet = function _queueWorklet(samples) {
    this._batch.push(samples);
    this._batchFrames += samples.length;
    const target = Math.floor((this.ctx ? this.ctx.sampleRate : 48000) * 0.06);
    if (this._batchFrames >= target) this._flushWorklet();
  };

  EvieAudioPlaybackEngine.prototype._flushWorklet = function _flushWorklet() {
    if (!this.node || !this._batch.length) return;
    let total = 0;
    for (let i = 0; i < this._batch.length; i += 1) total += this._batch[i].length;
    const copy = new Float32Array(total);
    let off = 0;
    for (let i = 0; i < this._batch.length; i += 1) {
      copy.set(this._batch[i], off);
      off += this._batch[i].length;
    }
    this._batch = [];
    this._batchFrames = 0;
    this.metrics.workletBatches += 1;
    this.node.port.postMessage({ type: "pcm", samples: copy });
  };

  EvieAudioPlaybackEngine.prototype._enqueueFallback = function _enqueueFallback(float32, ctx) {
    const duration = float32.length / ctx.sampleRate;
    const plan = nextPlayTime(ctx.currentTime, this.nextTime, duration, this.jitter.targetS);
    if (plan.underrun) {
      this.metrics.underruns += 1;
      this.metrics.gaps += 1;
      this.jitter.adapt(true);
    }
    const overlap = this.nextTime && plan.start < this.nextTime - 0.0005;
    if (overlap) this.metrics.overlaps += 1;
    this.nextTime = plan.queuedUntil;
    const buffer = ctx.createBuffer(1, Math.max(1, float32.length), ctx.sampleRate);
    buffer.getChannelData(0).set(float32);
    const node = ctx.createBufferSource();
    node.buffer = buffer;
    node.connect(this.gain || ctx.destination);
    const self = this;
    node.onended = function () {
      const idx = self.nodes.indexOf(node);
      if (idx >= 0) self.nodes.splice(idx, 1);
      if (!self.nodes.length) {
        self.playing = false;
        self.envelope = 0;
        self._emitPlaying(false);
      }
    };
    node.start(plan.start);
    this.nodes.push(node);
    this.envelope = 0.2;
    if (!this.playing) {
      this.playing = true;
      this._emitPlaying(true);
    }
    return true;
  };

  EvieAudioPlaybackEngine.prototype.playTestTone = async function playTestTone() {
    const ctx = await this.ensure();
    const seconds = 0.32;
    const n = Math.floor(INPUT_RATE * seconds);
    const pcm = new Int16Array(n);
    for (let i = 0; i < n; i += 1) {
      pcm[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / INPUT_RATE) * 16000);
    }
    this.beginTurn({ responseId: "tone" });
    const bytes = new Uint8Array(pcm.buffer);
    const frame = Math.floor(INPUT_RATE * 0.02) * 2;
    for (let off = 0; off < bytes.length; off += frame) {
      await this.enqueuePcm16({
        bytes: bytes.subarray(off, Math.min(bytes.length, off + frame)),
        seq: off / frame,
        socketGeneration: this.socketGeneration,
        sampleRate: INPUT_RATE,
        responseId: "tone",
      });
    }
    this.endStream();
    return {
      backend: this.backend,
      sampleRate: ctx.sampleRate,
      engine: AUDIO_ENGINE_VERSION,
    };
  };

  EvieAudioPlaybackEngine.prototype.playPcmAsset = async function playPcmAsset(bytes, sampleRate) {
    const src = sampleRate || INPUT_RATE;
    this.beginTurn({ responseId: "diag" });
    const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    const frame = Math.floor(src * 0.02) * 2;
    for (let off = 0; off < view.length; off += frame) {
      await this.enqueuePcm16({
        bytes: view.subarray(off, Math.min(view.length, off + frame)),
        seq: off / frame,
        socketGeneration: this.socketGeneration,
        sampleRate: src,
        responseId: "diag",
      });
    }
    this.endStream();
    return this.snapshot();
  };

  EvieAudioPlaybackEngine.prototype.snapshot = function snapshot() {
    return {
      backend: this.backend,
      engine: AUDIO_ENGINE_VERSION,
      sampleRate: this.ctx ? this.ctx.sampleRate : 0,
      metrics: this.metrics,
      contextState: this.ctx ? this.ctx.state : "none",
    };
  };

  const api = {
    AUDIO_ENGINE_VERSION: AUDIO_ENGINE_VERSION,
    INPUT_RATE: INPUT_RATE,
    PRIME_S: PRIME_S,
    JITTER_MIN_S: JITTER_MIN_S,
    JITTER_MAX_S: JITTER_MAX_S,
    nextPlayTime: nextPlayTime,
    pcm16ToFloat32: pcm16ToFloat32,
    int16BytesToFloat32: int16BytesToFloat32,
    LinearResampler: LinearResampler,
    RingBuffer: RingBuffer,
    JitterController: JitterController,
    maxAdjacentJump: maxAdjacentJump,
    EvieAudioPlaybackEngine: EvieAudioPlaybackEngine,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.EvieAudio = api;
})(typeof window !== "undefined" ? window : globalThis);
