// Mic capture worklet for the hands-free "EVIE" stream.
//
// Emits fixed-size mono PCM16 frames at 16 kHz. The AudioContext is created at
// 16 kHz so the browser resamples for us; if a platform refuses that rate the
// worklet resamples linearly instead of silently shipping the wrong rate.

class PcmFrameProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const config = (options && options.processorOptions) || {};
    this.targetRate = config.targetRate || 16000;
    this.frameSamples = config.frameSamples || 320; // 20 ms at 16 kHz
    this.ratio = sampleRate / this.targetRate;
    this.buffer = new Int16Array(this.frameSamples);
    this.filled = 0;
    this.cursor = 0;
  }

  push(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.buffer[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (this.filled === this.frameSamples) {
      const frame = this.buffer.slice(0);
      this.port.postMessage(frame.buffer, [frame.buffer]);
      this.filled = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) {
      return true;
    }
    if (this.ratio === 1) {
      for (let i = 0; i < channel.length; i += 1) {
        this.push(channel[i]);
      }
      return true;
    }
    // Linear resample fallback (context rate !== 16 kHz).
    while (this.cursor < channel.length) {
      const index = Math.floor(this.cursor);
      const next = Math.min(index + 1, channel.length - 1);
      const fraction = this.cursor - index;
      this.push(channel[index] * (1 - fraction) + channel[next] * fraction);
      this.cursor += this.ratio;
    }
    this.cursor -= channel.length;
    return true;
  }
}

registerProcessor("pcm-frame-processor", PcmFrameProcessor);
