// Provider-independent mono PCM playback. Transport chunks need not coincide
// with samples or playback buffers. Web Audio resamples to the device's rate.
export class PCMPlayer {
  constructor(context, { format, sample_rate, channels }) {
    if (format !== "pcm_s16le" || channels !== 1 ||
        !Number.isInteger(sample_rate) || sample_rate < 8000 || sample_rate > 96000) {
      throw new Error("Unsupported audio format");
    }
    this.context = context;
    this.rate = sample_rate;
    this.pending = [];
    this.frames = 0;
    this.offset = 0;
    this.lowByte = null;
    this.sources = new Set();
    this.ended = false;
    this.finished = false;
    this.cancelled = false;
    this.started = false;
    this.nextTime = 0;
    this.done = new Promise(resolve => { this.resolve = resolve; });
  }

  push(bytes) {
    if (this.finished || this.ended) return;
    if (this.lowByte !== null) {
      const joined = new Uint8Array(bytes.length + 1);
      joined[0] = this.lowByte;
      joined.set(bytes, 1);
      bytes = joined;
      this.lowByte = null;
    }
    if (bytes.length % 2) this.lowByte = bytes[bytes.length - 1];
    const count = Math.floor(bytes.length / 2);
    // Bound queued replies even if generation outruns playback or the audio
    // context is suspended. Fail visibly instead of retaining unlimited audio.
    if (this.frames + count > this.rate * 120) throw new Error("Audio buffer is full");
    if (count) {
      const view = new DataView(bytes.buffer, bytes.byteOffset, count * 2);
      const samples = new Float32Array(count);
      for (let i = 0; i < count; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
      this.pending.push(samples);
      this.frames += count;
    }
    this.pump();
  }

  end() {
    if (this.finished) return;
    if (this.lowByte !== null) throw new Error("Incomplete audio sample");
    this.ended = true;
    this.pump();
  }

  play(destination, onStart = () => {}) {
    if (this.finished || this.destination) return this.done;
    this.destination = destination;
    this.onStart = onStart;
    this.timer = setInterval(() => this.pump(), 25);
    this.pump();
    return this.done;
  }

  pump() {
    if (!this.destination || this.finished) return;
    const now = this.context.currentTime;
    // At startup or after an underrun, collect 120 ms before resuming. Flush
    // shorter final replies too; receiving the end marker never clips a tail.
    if (this.nextTime <= now && !this.ended && this.frames < this.rate * 0.12) return;
    while (this.frames && this.nextTime < now + 0.4) {
      const count = Math.min(this.frames, Math.round(this.rate * 0.1));
      const buffer = this.context.createBuffer(1, count, this.rate);
      const samples = buffer.getChannelData(0);
      let written = 0;
      while (written < count) {
        const head = this.pending[0];
        const take = Math.min(count - written, head.length - this.offset);
        samples.set(head.subarray(this.offset, this.offset + take), written);
        written += take;
        this.offset += take;
        if (this.offset === head.length) {
          this.pending.shift();
          this.offset = 0;
        }
      }
      this.frames -= count;
      if (!this.started) {
        this.started = true;
        this.onStart(now + 0.02);
        this.nextTime = now + 0.10; // 80 ms for the radio key-up
      } else if (this.nextTime <= now) {
        this.nextTime = now + 0.02;
      }
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.destination);
      this.sources.add(source);
      source.onended = () => {
        source.disconnect();
        this.sources.delete(source);
        this.pump();
      };
      source.start(this.nextTime);
      this.nextTime += count / this.rate;
    }
    if (this.ended && !this.frames && !this.sources.size) this.finish();
  }

  finish() {
    if (this.finished) return;
    this.finished = true;
    clearInterval(this.timer);
    this.resolve();
  }

  stop() {
    this.cancelled = true;
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.pending = [];
    this.frames = 0;
    this.lowByte = null;
    this.finish();
  }
}
