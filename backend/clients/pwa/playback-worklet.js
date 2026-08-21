class EviePlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    const seconds = 2;
    this.buf = new Float32Array(Math.max(8192, Math.floor(sampleRate * seconds)));
    this.w = 0;
    this.r = 0;
    this.count = 0;
    this.playing = false;
    this.primed = false;
    this.generation = 0;
    this.targetFrames = Math.floor(sampleRate * 0.09);
    this.minTarget = Math.floor(sampleRate * 0.06);
    this.maxTarget = Math.floor(sampleRate * 0.28);
    this.underruns = 0;
    this.starveFrames = 0;
    this.fade = 0;
    this.envAcc = 0;
    this.envN = 0;
    this.envAt = 0;
    this.ended = false;
    this.overflows = 0;
    this.port.onmessage = (ev) => this.onMsg(ev.data);
  }

  onMsg(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "pcm" && msg.samples) {
      this.write(msg.samples);
    } else if (msg.type === "start") {
      this.playing = true;
      this.ended = false;
    } else if (msg.type === "stop" || msg.type === "flush") {
      this.playing = false;
      this.primed = false;
      this.fade = 0;
      this.count = 0;
      this.w = 0;
      this.r = 0;
      this.generation += 1;
      this.ended = true;
    } else if (msg.type === "target") {
      const frames = Math.floor(Number(msg.frames) || this.targetFrames);
      this.targetFrames = Math.max(this.minTarget, Math.min(this.maxTarget, frames));
    } else if (msg.type === "end") {
      this.ended = true;
    }
  }

  write(samples) {
    const src = samples;
    const cap = this.buf.length;
    for (let i = 0; i < src.length; i += 1) {
      if (this.count >= cap - 1) {
        this.overflows += 1;
        continue;
      }
      this.buf[this.w] = src[i];
      this.w = (this.w + 1) % cap;
      this.count += 1;
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    if (!this.playing) {
      out.fill(0);
      this.fade = 0;
      return true;
    }
    if (!this.primed) {
      if (this.count < this.targetFrames && !this.ended) {
        out.fill(0);
        return true;
      }
      this.primed = true;
      this.fade = 0;
    }
    const fadeStep = 1 / 96;
    for (let i = 0; i < out.length; i += 1) {
      if (this.count <= 0) {
        out[i] = 0;
        if (!this.ended) {
          this.underruns += 1;
          this.starveFrames += 1;
          this.primed = false;
          this.fade = 0;
        } else {
          this.playing = false;
        }
        continue;
      }
      let sample = this.buf[this.r];
      this.r = (this.r + 1) % this.buf.length;
      this.count -= 1;
      if (this.fade < 1) {
        this.fade = Math.min(1, this.fade + fadeStep);
        sample *= this.fade;
      }
      if (this.ended && this.count < 96) {
        sample *= this.count / 96;
      }
      out[i] = sample;
      this.envAcc += sample * sample;
      this.envN += 1;
    }
    if (this.envN >= Math.floor(sampleRate * 0.05) && currentTime - this.envAt >= 0.045) {
      const rms = Math.sqrt(this.envAcc / this.envN);
      this.envAcc = 0;
      this.envN = 0;
      this.envAt = currentTime;
      this.port.postMessage({
        type: "stats",
        rms: rms,
        available: this.count,
        underruns: this.underruns,
        starveFrames: this.starveFrames,
        overflows: this.overflows,
        primed: this.primed,
        sampleRate: sampleRate,
      });
    }
    return true;
  }
}

registerProcessor("evie-playback", EviePlaybackProcessor);
