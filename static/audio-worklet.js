/*
 * Audio worklets for the voice call.
 *
 *  - "pcm-capture": microphone -> PCM16 LE mono 16 kHz chunks (~40 ms each).
 *    The AudioContext runs at the device's native rate (usually 44.1 or 48 kHz),
 *    so we low-pass filter (two cascaded biquads, ~24 dB/oct at 7 kHz) and then
 *    resample with linear interpolation at a fractional step. This avoids the
 *    aliasing you get from naive sample dropping and works for any input rate.
 *
 *  - "pcm-player": agent audio (PCM16 mono 24 kHz) -> speakers. Samples are kept
 *    in a ring buffer and upsampled to the context rate on the fly, so playback
 *    is gapless regardless of how the network chunks the stream. A "flush"
 *    message drops everything queued instantly (used for barge-in).
 */

const TARGET_RATE = 16000;
const CHUNK_MS = 40;

/** RBJ cookbook low-pass biquad coefficients, normalised by a0. */
function lowpassCoefficients(cutoff, rate, q) {
  const w0 = (2 * Math.PI * cutoff) / rate;
  const cos = Math.cos(w0);
  const alpha = Math.sin(w0) / (2 * q);
  const a0 = 1 + alpha;
  return {
    b0: (1 - cos) / 2 / a0,
    b1: (1 - cos) / a0,
    b2: (1 - cos) / 2 / a0,
    a1: (-2 * cos) / a0,
    a2: (1 - alpha) / a0,
  };
}

class Biquad {
  constructor(c) {
    this.c = c;
    this.x1 = this.x2 = this.y1 = this.y2 = 0;
  }
  step(x) {
    const c = this.c;
    const y = c.b0 * x + c.b1 * this.x1 + c.b2 * this.x2 - c.a1 * this.y1 - c.a2 * this.y2;
    this.x2 = this.x1;
    this.x1 = x;
    this.y2 = this.y1;
    this.y1 = y;
    return y;
  }
}

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // `sampleRate` is a global in AudioWorkletGlobalScope (the context rate).
    this.ratio = sampleRate / TARGET_RATE; // input samples per output sample
    this.bypassFilter = sampleRate <= TARGET_RATE * 1.05;
    // Butterworth-ish cascade (Q values for a 4th-order Butterworth).
    const cutoff = Math.min(7000, sampleRate * 0.45);
    this.filters = [
      new Biquad(lowpassCoefficients(cutoff, sampleRate, 0.5412)),
      new Biquad(lowpassCoefficients(cutoff, sampleRate, 1.3066)),
    ];
    this.chunkSize = Math.round((TARGET_RATE * CHUNK_MS) / 1000); // 640 samples
    this.out = new Int16Array(this.chunkSize);
    this.outIndex = 0;
    this.sumSquares = 0;
    // Fractional read position (in input samples) relative to `prev`.
    this.pos = 0;
    this.prev = 0;
  }

  emit(sample) {
    const s = sample < -1 ? -1 : sample > 1 ? 1 : sample;
    this.out[this.outIndex++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    this.sumSquares += s * s;
    if (this.outIndex === this.chunkSize) {
      const rms = Math.sqrt(this.sumSquares / this.chunkSize);
      const buffer = this.out.buffer;
      this.port.postMessage({ type: "chunk", buffer, rms }, [buffer]);
      this.out = new Int16Array(this.chunkSize);
      this.outIndex = 0;
      this.sumSquares = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    const [f1, f2] = this.filters;
    for (let i = 0; i < channel.length; i++) {
      const x = this.bypassFilter ? channel[i] : f2.step(f1.step(channel[i]));
      // Output samples fall at positions pos, pos+ratio, ... between prev (0) and x (1).
      while (this.pos <= 1) {
        this.emit(this.prev + (x - this.prev) * this.pos);
        this.pos += this.ratio;
      }
      this.pos -= 1;
      this.prev = x;
    }
    return true;
  }
}

const SOURCE_RATE = 24000;

class PcmPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = SOURCE_RATE * 120; // two minutes of audio
    this.ring = new Float32Array(this.capacity);
    this.readPos = 0; // integer index into ring
    this.frac = 0; // fractional position between readPos and readPos+1
    this.available = 0; // samples queued
    this.step = SOURCE_RATE / sampleRate; // source samples per output sample
    this.playing = false;
    // Small jitter buffer before (re)starting playback, unless the stream pauses.
    this.prebuffer = Math.round(SOURCE_RATE * 0.06);
    this.framesSinceWrite = 0;
    this.reportEvery = Math.max(1, Math.round((sampleRate * 0.05) / 128)); // ~50 ms
    this.quantum = 0;
    this.sumSquares = 0;
    this.sumCount = 0;

    this.port.onmessage = (event) => {
      const msg = event.data;
      if (msg.type === "pcm") this.write(new Int16Array(msg.buffer));
      else if (msg.type === "flush") this.flush();
    };
  }

  write(samples) {
    let n = samples.length;
    let offset = 0;
    if (n > this.capacity) {
      offset = n - this.capacity;
      n = this.capacity;
    }
    // On overflow, drop the oldest audio.
    const overflow = this.available + n - this.capacity;
    if (overflow > 0) {
      this.readPos = (this.readPos + overflow) % this.capacity;
      this.available -= overflow;
    }
    let writePos = (this.readPos + this.available) % this.capacity;
    for (let i = 0; i < n; i++) {
      this.ring[writePos] = samples[offset + i] / 0x8000;
      writePos = writePos + 1 === this.capacity ? 0 : writePos + 1;
    }
    this.available += n;
    this.framesSinceWrite = 0;
  }

  flush() {
    this.readPos = (this.readPos + this.available) % this.capacity;
    this.available = 0;
    this.frac = 0;
    this.playing = false;
    this.report(true);
  }

  report(force) {
    this.quantum++;
    if (!force && this.quantum % this.reportEvery !== 0) return;
    const rms = this.sumCount ? Math.sqrt(this.sumSquares / this.sumCount) : 0;
    this.sumSquares = 0;
    this.sumCount = 0;
    this.port.postMessage({
      type: "level",
      rms: force ? 0 : rms,
      bufferedMs: (this.available / SOURCE_RATE) * 1000,
      playing: this.playing,
      flushed: Boolean(force),
    });
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    const len = out.length;
    this.framesSinceWrite += len;

    if (!this.playing && this.available > 0) {
      // Start once we have a small cushion, or when the stream seems to have
      // paused (short utterance tail) so we never sit on queued audio.
      const stalled = this.framesSinceWrite > sampleRate * 0.08;
      if (this.available >= this.prebuffer || stalled) this.playing = true;
    }

    if (!this.playing) {
      out.fill(0);
      this.report(false);
      return true;
    }

    const cap = this.capacity;
    for (let i = 0; i < len; i++) {
      // Need samples at readPos and readPos+1 for interpolation.
      if (this.available < 2) {
        out.fill(0, i);
        this.playing = false;
        break;
      }
      const a = this.ring[this.readPos];
      const b = this.ring[this.readPos + 1 === cap ? 0 : this.readPos + 1];
      const s = a + (b - a) * this.frac;
      out[i] = s;
      this.sumSquares += s * s;
      this.sumCount++;
      this.frac += this.step;
      while (this.frac >= 1) {
        this.frac -= 1;
        this.readPos = this.readPos + 1 === cap ? 0 : this.readPos + 1;
        this.available--;
      }
    }
    this.report(false);
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
registerProcessor("pcm-player", PcmPlayerProcessor);
