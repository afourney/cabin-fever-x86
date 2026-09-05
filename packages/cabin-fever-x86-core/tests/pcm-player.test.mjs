import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { PCMPlayer } from "../src/cabin_fever_x86_core/web_client/static/pcm-player.js";

const format = { format: "pcm_s16le", sample_rate: 24000, channels: 1 };

class Context {
  currentTime = 0;
  sampleRate = 48000;
  sources = [];
  destination = {};
  createBuffer(channels, count, rate) {
    const data = new Float32Array(count);
    return { duration: count / rate, sampleRate: rate, getChannelData: () => data };
  }
  createBufferSource() {
    const node = {
      connect() {}, disconnect() { this.disconnected = true; },
      start(at) { this.at = at; },
      stop() { this.stopped = true; this.onended?.(); },
    };
    this.sources.push(node);
    return node;
  }
  createGain() {
    const param = () => ({ value: 0, setTargetAtTime() {}, setValueAtTime() {},
      linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} });
    return { connect() {}, disconnect() {}, start() {}, stop() {},
      gain: param(), frequency: param(), Q: param(), threshold: param(), ratio: param(),
      attack: param(), release: param(), knee: param() };
  }
  createBiquadFilter() { return this.createGain(); }
  createDynamicsCompressor() { return this.createGain(); }
  createOscillator() { return this.createGain(); }
  createWaveShaper() { return this.createGain(); }
  advance(seconds) {
    this.currentTime += seconds;
    for (const node of [...this.sources]) {
      if (!node.loop && !node.stopped && node.at + node.buffer.duration <= this.currentTime) {
        node.stopped = true;
        node.onended?.();
      }
    }
  }
}

function player(t) {
  const context = new Context();
  const stream = new PCMPlayer(context, format);
  t.after(() => stream.stop());
  return { context, stream };
}

test("plays before end, schedules continuous buffers, and drains before resolving", async t => {
  const { context, stream } = player(t);
  let finished = false;
  stream.play(context.destination).then(() => { finished = true; });
  stream.push(new Uint8Array(24000 * 2 * 0.2));
  assert.equal(stream.ended, false);
  assert.equal(context.sources.length, 2);
  assert.equal(context.sources[0].buffer.sampleRate, 24000);
  assert.equal(context.sources[1].at, context.sources[0].at + 0.1);
  stream.end();
  await Promise.resolve();
  assert.equal(finished, false);
  context.advance(0.5);
  await stream.done;
  assert.equal(finished, true);
});

test("reassembles samples split across packets and flushes short replies", async t => {
  const { context, stream } = player(t);
  stream.play(context.destination);
  stream.push(new Uint8Array([0, 128, 255]));
  stream.push(new Uint8Array([127, 0, 0]));
  assert.equal(context.sources.length, 0);
  stream.end();
  assert.deepEqual([...context.sources[0].buffer.getChannelData(0)], [-1, 32767 / 32768, 0]);
  context.advance(1);
  await stream.done;
});

test("buffers queued audio until its turn and bounds scheduling ahead", t => {
  const { context, stream } = player(t);
  stream.push(new Uint8Array(24000 * 2 * 2));
  stream.end();
  assert.equal(context.sources.length, 0);
  stream.play(context.destination);
  assert.ok(context.sources.length <= 4);
  assert.ok(stream.frames > 0);
});

test("underrun waits for a small buffer, then resumes on the audio clock", t => {
  const { context, stream } = player(t);
  stream.play(context.destination);
  stream.push(new Uint8Array(24000 * 2 * 0.2));
  context.advance(2);
  stream.push(new Uint8Array(24000 * 2 * 0.05));
  assert.equal(context.sources.length, 2);
  stream.push(new Uint8Array(24000 * 2 * 0.1));
  assert.ok(context.sources[2].at >= context.currentTime);
});

test("stop clears scheduled and queued audio and ignores later packets", async t => {
  const { context, stream } = player(t);
  stream.play(context.destination);
  stream.push(new Uint8Array(24000 * 2));
  stream.stop();
  await stream.done;
  assert.ok(context.sources.every(s => s.stopped && s.disconnected));
  assert.equal(stream.frames, 0);
  stream.push(new Uint8Array(100));
  assert.equal(stream.frames, 0);
});

test("rejects unsupported formats, truncated samples, and excessive buffering", t => {
  assert.throws(() => new PCMPlayer(new Context(), { ...format, format: "mp3" }));
  const { stream } = player(t);
  stream.push(new Uint8Array([0]));
  assert.throws(() => stream.end(), /Incomplete/);
  assert.throws(() => stream.push(new Uint8Array(24000 * 2 * 121)), /full/);
});

// Exercise the actual page handlers too: stream IDs, reply queue, half-duplex
// interruption, static and disconnect cleanup all live in the HTML module.
function page() {
  const elements = new Map();
  const element = () => ({ textContent: "", classList: { add() {}, remove() {} },
    addEventListener() {}, append() {}, disabled: false });
  const scope = vm.createContext({ PCMPlayer, Uint8Array, ArrayBuffer, DataView,
    Float32Array, URLSearchParams, console, setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval() {}, addEventListener() {},
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }, createElement: element },
    location: { search: "", protocol: "http:", host: "localhost" },
    WebSocket: class { send() {} },
  });
  const html = readFileSync(new URL("../src/cabin_fever_x86_core/web_client/static/index.html", import.meta.url), "utf8");
  const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
    .replace(/import .*?;\n/, "")
    .replace("\nopenWeather();", "");
  vm.runInContext(source, scope);
  scope.context = new Context();
  vm.runInContext(`audioCtx = context; recorder = { state: "inactive", start() {} }; connect();
    globalThis.handlers = {
      message: data => ws.onmessage({ data }), keyDown, cutPlayback,
      close: () => ws.onclose(),
      streams: () => streams, playing: () => playing,
    };`, scope);
  const json = msg => scope.handlers.message(JSON.stringify(msg));
  const chunk = (id, count = 4800) => {
    const bytes = new Uint8Array(4 + count * 2);
    new DataView(bytes.buffer).setUint32(0, id, false);
    scope.handlers.message(bytes.buffer);
  };
  const start = id => json({ type: "audio_start", stream_id: id, ...format });
  const end = id => json({ type: "audio_end", stream_id: id, status: "complete" });
  return { ...scope, json, chunk, start, end, elements };
}

const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

test("page queues streams, and microphone interruption discards current, queued and late audio", async t => {
  const p = page();
  t.after(() => p.handlers.cutPlayback());
  p.start(1); p.chunk(1);
  await settle();
  const first = p.handlers.streams().get(1);
  assert.equal(first.started, true);
  p.end(1); p.start(2); p.chunk(2); p.end(2);
  await settle();
  const second = p.handlers.streams().get(2);
  assert.equal(second.started, false);
  p.handlers.keyDown();
  p.chunk(1); p.end(1); p.start(3); p.chunk(3);
  await settle();
  assert.equal(first.cancelled, true);
  assert.equal(second.cancelled, true);
  assert.equal(p.handlers.streams().size, 0);
  assert.equal(p.handlers.playing(), null);
  assert.equal(p.elements.get("status").textContent, "TRANSMITTING");
});

test("page advances to the next reply after audio and squelch drain", async t => {
  const p = page();
  t.after(() => p.handlers.cutPlayback());
  p.start(1); p.chunk(1); p.end(1);
  p.start(2); p.chunk(2); p.end(2);
  await settle();
  const second = p.handlers.streams().get(2);
  assert.equal(second.started, false);
  p.context.advance(1);
  await settle();
  assert.equal(second.started, false); // first reply's squelch is still playing
  p.context.advance(1);
  await settle();
  assert.equal(second.started, true);
});

test("disconnect cuts audio and retains disconnected status after cleanup", async t => {
  const p = page();
  t.after(() => p.handlers.cutPlayback());
  p.start(1); p.chunk(1);
  await settle();
  p.handlers.close();
  await settle();
  assert.equal(p.handlers.playing(), null);
  assert.equal(p.handlers.streams().size, 0);
  assert.equal(p.elements.get("status").textContent, "disconnected");
});

test("empty replies play static and can be interrupted", async t => {
  const p = page();
  t.after(() => p.handlers.cutPlayback());
  p.json({ type: "assistant", text: "" });
  await settle();
  assert.equal(p.elements.get("status").textContent, "RECEIVING");
  assert.ok(p.context.sources.length > 0);
  p.handlers.keyDown();
  await settle();
  assert.equal(p.handlers.playing(), null);
  assert.equal(p.elements.get("status").textContent, "TRANSMITTING");
});

test("generation errors stop partial playback and release the reply queue", async t => {
  const p = page();
  t.after(() => p.handlers.cutPlayback());
  p.start(1); p.chunk(1);
  await settle();
  const first = p.handlers.streams().get(1);
  p.json({ type: "audio_end", stream_id: 1, status: "error" });
  p.start(2); p.chunk(2);
  await settle();
  assert.equal(first.cancelled, true);
  assert.equal(p.handlers.streams().get(2).started, true);
});
