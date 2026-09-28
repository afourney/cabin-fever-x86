import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../src/cabin_fever_x86_core/web_gateway/static/index.html", import.meta.url), "utf8");
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));

function page(getUserMedia) {
  const elements = new Map(), timers = new Map(), nodes = [];
  let nextTimer = 0, volume = 0;
  const element = () => ({
    disabled: false, hidden: true, children: [], attributes: new Map(), classes: new Set(),
    focus() {},
    get textContent() { return this.children.length ? this.children.map(child => child.textContent).join("") : this.text || ""; },
    set textContent(text) { this.text = text; this.children = []; },
    get classList() { return { add: name => this.classes.add(name), remove: name => this.classes.delete(name) }; },
    setAttribute(name, value) { this.attributes.set(name, value); },
    removeAttribute(name) { this.attributes.delete(name); },
    append(child) { this.children.push(child); child.parent = this; },
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); },
  });
  const $ = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const errors = [], recordings = [];
  let requests = 0, connections = 0;
  const context = vm.createContext({
    $, log: $("log"), status: $("status"), Blob,
    document: { createElement: element },
    navigator: { mediaDevices: { getUserMedia: () => { requests++; return getUserMedia(); } } },
    setInterval(callback) { timers.set(++nextTimer, callback); return nextTimer; },
    clearInterval(id) { timers.delete(id); },
    audioCtx: {
      createMediaStreamSource() {
        const node = { connect() {}, disconnect() { this.disconnected = true; } };
        nodes.push(node); return node;
      },
      createAnalyser() {
        const node = { getFloatTimeDomainData(samples) { samples.fill(volume); },
          disconnect() { this.disconnected = true; } };
        nodes.push(node); return node;
      },
    },
    MediaRecorder: class {
      static isTypeSupported() { return true; }
      constructor(stream) { this.stream = stream; this.state = "inactive"; this.mimeType = "audio/webm"; recordings.push(this); }
      start() { this.state = "recording"; }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob(["voice"]) });
        this.onstop?.();
      }
    },
    say(kind, message) {
      if (kind !== "user") errors.push({ kind, message });
      const line = element(); line.textContent = message; $("log").append(line); return line;
    },
    fetch: async () => ({ ok: true, json: async () => ({ text: "" }) }),
    setStatus: text => { $("status").textContent = text; },
    cutPlayback() {}, rainLevel() {}, send() {}, openWeather: async () => {},
    connect: async () => { connections++; }, rememberedMute: () => false,
    URL, location: { href: "https://example.com/" }, history: { replaceState() {} },
  });
  vm.runInContext(`
    let recorder = null, keyed = false, playing = false, chunks = [];
    let ws = {}, ownerToken = "owner", connectionId = "connection";
    let turningOn = false, muted = false, sessionId = "test";
    const browserAuth = { authenticated: true };
    const RAIN_DUCKED = 0.04, RAIN_UNDER = 0.10;
    ${section("let micPending", "// The splash always")}
    ${section("async function turnOn(resume)", 'splash.addEventListener("click"')}
  `, context);
  return { $, errors, recordings, requests: () => requests, connections: () => connections,
    timers, nodes, level(value) { volume = value; for (const tick of timers.values()) tick(); },
    run: source => vm.runInContext(source, context) };
}

function stream() {
  const track = { readyState: "live", stop() { this.readyState = "ended"; } };
  return { getTracks: () => [track], getAudioTracks: () => [track] };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("startup opens chat even while permission is unanswered, and reports denial there", async () => {
  const permission = deferred();
  const ui = page(() => permission.promise);
  await ui.run("turnOn(null)");
  assert.equal(ui.connections(), 1);
  assert.equal(ui.$("radio").hidden, false);
  assert.equal(ui.$("session-screen").hidden, true);
  assert.match(ui.$("status").textContent, /Waiting for microphone/);
  permission.reject(Object.assign(new Error("blocked"), { name: "NotAllowedError" }));
  await ui.run("micPending");
  assert.equal(ui.errors[0].kind, "error");
  assert.match(ui.errors[0].message, /denied or blocked.*site settings/);
});

test("each new press retries denied access and later permission enables recording", async () => {
  let denied = true;
  const ui = page(async () => {
    if (denied) throw Object.assign(new Error(), { name: "NotAllowedError" });
    return stream();
  });
  await ui.run("keyDown()");
  ui.run("keyUp()");
  await ui.run("keyDown()");
  assert.equal(ui.requests(), 2);
  assert.equal(ui.errors.length, 2);
  ui.run("keyUp()");
  denied = false;
  await ui.run("keyDown()");
  assert.equal(ui.recordings[0].state, "recording");
  ui.run("keyUp()");
  assert.equal(ui.recordings[0].state, "inactive");
  await ui.run("keyDown()");
  assert.equal(ui.requests(), 3, "a live microphone is reused");
});

test("releasing while permission is pending never starts recording on approval", async () => {
  const permission = deferred();
  const ui = page(() => permission.promise);
  const press = ui.run("keyDown()");
  ui.run("keyUp()");
  permission.resolve(stream());
  await press;
  assert.equal(ui.recordings[0].state, "inactive");
  await ui.run("keyDown()");
  assert.equal(ui.recordings[0].state, "recording");
});

test("repeated presses share the pending request and start recording only once", async () => {
  const permission = deferred();
  const ui = page(() => permission.promise);
  const first = ui.run("keyDown()");
  ui.run("keyUp()");
  const second = ui.run("keyDown()");
  assert.equal(ui.requests(), 1);
  permission.resolve(stream());
  await Promise.all([first, second]);
  assert.equal(ui.recordings.length, 1);
  assert.equal(ui.recordings[0].state, "recording");
  assert.equal(ui.errors.length, 0);
});

test("disconnect during permission request prevents recording", async () => {
  const permission = deferred();
  const ui = page(() => permission.promise);
  const press = ui.run("keyDown()");
  ui.$("talk").disabled = true;
  permission.resolve(stream());
  await press;
  assert.equal(ui.recordings[0].state, "inactive");
});

test("an ended microphone stream is reacquired on the next press", async () => {
  const ui = page(async () => stream());
  await ui.run("keyDown()");
  ui.run("keyUp()");
  ui.recordings[0].stream.getTracks()[0].stop();
  await ui.run("keyDown()");
  assert.equal(ui.requests(), 2);
  assert.equal(ui.recordings[1].state, "recording");
});

test("device errors receive specific chat messages", async () => {
  for (const [name, message] of [["NotFoundError", /No microphone/], ["NotReadableError", /another app/]]) {
    const ui = page(async () => { throw Object.assign(new Error(), { name }); });
    await ui.run("keyDown()");
    assert.match(ui.errors[0].message, message);
  }
});

test("recording levels become dots, then the transcript replaces the same row", async () => {
  const ui = page(async () => stream());
  ui.run('fetch = () => new Promise(resolve => { globalThis.resolveTake = resolve; })');
  await ui.run("keyDown()");
  const line = ui.$("log").children[0];
  ui.level(0);
  assert.equal(line.textContent, "▁▁▁▁▁");
  ui.level(0.5);
  assert.equal(line.textContent, "▁▁▁▁█");
  ui.run("keyUp()");
  assert.equal(line.textContent, "...");
  assert.equal(line.classes.has("take-dots"), true);
  assert.equal(ui.timers.size, 0);
  assert.ok(ui.nodes.every(node => node.disconnected));
  ui.run('resolveTake({ ok: true, json: async () => ({ text: "Open the door." }) })');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.$("log").children.length, 1);
  assert.equal(ui.$("log").children[0], line);
  assert.equal(line.textContent, "Open the door.");
  assert.equal(line.classes.has("take-dots"), false);
  assert.equal(line.attributes.size, 0);
});

test("overlapping uploads fill their own rows even when responses arrive out of order", async () => {
  const ui = page(async () => stream());
  ui.run('globalThis.replies = []; fetch = () => new Promise(resolve => replies.push(resolve))');
  await ui.run("keyDown()"); ui.run("keyUp()");
  await ui.run("keyDown()"); ui.run("keyUp()");
  const [first, second] = ui.$("log").children;
  ui.run('replies[1]({ ok: true, json: async () => ({ text: "Second" }) })');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(first.textContent, "...");
  assert.equal(second.textContent, "Second");
  ui.run('replies[0]({ ok: true, json: async () => ({ text: "First" }) })');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(first.textContent, "First");
  assert.equal(ui.$("log").children.length, 2);
});

test("empty audio, no speech, and upload failures remove the pending indicator", async () => {
  for (const response of [
    'chunks = [];',
    'fetch = async () => ({ ok: true, json: async () => ({ text: "" }) });',
    'fetch = async () => ({ ok: false, json: async () => ({ detail: "failed" }) });',
    'fetch = async () => { throw new Error("offline"); };',
  ]) {
    const ui = page(async () => stream());
    await ui.run("keyDown()");
    const line = ui.$("log").children[0];
    // Run the stop handler explicitly, including the zero-byte recording case.
    ui.recordings[0].stop = function () { this.state = "inactive"; };
    ui.run("keyUp()");
    ui.run(response);
    if (!response.startsWith("chunks")) ui.run('chunks = [new Blob(["voice"])]');
    await ui.run("send()");
    assert.equal(ui.$("log").children.includes(line), false);
    assert.equal(ui.run("pendingTakes.size"), 0);
    assert.equal(ui.timers.size, 0);
  }
});

test("a held repress starts after the stop event without waiting for the upload", async () => {
  const ui = page(async () => stream());
  ui.run('globalThis.uploads = []; fetch = (url, options) => new Promise(resolve => uploads.push({ options, resolve }))');
  await ui.run("keyDown()");
  ui.recordings[0].stop = function () { this.state = "inactive"; };
  ui.run("keyUp()");
  await ui.run("keyDown()");
  assert.equal(ui.$("log").children.length, 1);
  assert.equal(ui.recordings[0].state, "inactive");
  ui.run('chunks = [new Blob(["first take"])]; globalThis.upload = send()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.recordings[0].state, "recording");
  assert.equal(ui.run("talkHeld"), true);
  assert.equal(ui.$("log").children.length, 2);
  assert.equal(ui.$("log").children[0].textContent, "...");
  assert.equal(await ui.run("uploads[0].options.body.text()"), "first take");
  assert.equal(ui.run("chunks.length"), 0);
  ui.run('uploads[0].resolve({ ok: true, json: async () => ({ text: "First take" }) })');
  await ui.run("upload");
  assert.equal(ui.$("log").children[0].textContent, "First take");
  assert.equal(ui.$("status").textContent, "TRANSMITTING");
});

test("releasing or disabling the radio cancels a queued repress", async () => {
  for (const cancel of ['keyUp()', '$("talk").disabled = true; keyUp(); clearTakeIndicators()']) {
    const ui = page(async () => stream());
    await ui.run("keyDown()");
    ui.recordings[0].stop = function () { this.state = "inactive"; };
    ui.run("keyUp()");
    await ui.run("keyDown()");
    ui.run(cancel);
    await ui.run("send()");
    assert.equal(ui.recordings[0].state, "inactive");
    assert.equal(ui.run("talkHeld"), false);
    assert.equal(ui.run("pendingTakes.size"), 0);
  }
});

test("an empty previous recording still starts the queued held press", async () => {
  const ui = page(async () => stream());
  await ui.run("keyDown()");
  ui.recordings[0].stop = function () { this.state = "inactive"; };
  ui.run("keyUp()");
  await ui.run("keyDown()");
  await ui.run("send()");
  assert.equal(ui.recordings[0].state, "recording");
  assert.equal(ui.$("log").children.length, 1);
});

test("meter setup failure still allows recording and transcription", async () => {
  const ui = page(async () => stream());
  ui.run('audioCtx.createAnalyser = () => { throw new Error("meter unavailable"); }');
  await ui.run("keyDown()");
  assert.equal(ui.recordings[0].state, "recording");
  assert.equal(ui.$("log").children[0].textContent, "▁▁▁▁▁");
  assert.equal(ui.nodes[0].disconnected, true);
  ui.run("keyUp()");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.run("pendingTakes.size"), 0);
});
