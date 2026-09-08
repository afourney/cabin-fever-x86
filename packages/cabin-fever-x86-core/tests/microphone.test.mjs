import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../src/cabin_fever_x86_core/web_gateway/static/index.html", import.meta.url), "utf8");
const section = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));

function page(getUserMedia) {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {
      disabled: false, hidden: true, textContent: "", focus() {},
      classList: { add() {}, remove() {} },
    });
    return elements.get(id);
  };
  const errors = [], recordings = [];
  let requests = 0, connections = 0;
  const context = vm.createContext({
    $, status: $("status"), navigator: { mediaDevices: { getUserMedia: () => { requests++; return getUserMedia(); } } },
    MediaRecorder: class {
      static isTypeSupported() { return true; }
      constructor(stream) { this.stream = stream; this.state = "inactive"; recordings.push(this); }
      start() { this.state = "recording"; }
      stop() { this.state = "inactive"; }
    },
    say: (kind, message) => errors.push({ kind, message }),
    setStatus: text => { $("status").textContent = text; },
    cutPlayback() {}, rainLevel() {}, send() {}, openWeather: async () => {},
    connect: async () => { connections++; }, rememberedMute: () => false,
    URL, location: { href: "https://example.com/" }, history: { replaceState() {} },
  });
  vm.runInContext(`
    let recorder = null, keyed = false, playing = false, chunks = [];
    let turningOn = false, muted = false, sessionId = "test";
    const browserAuth = { authenticated: true };
    const RAIN_DUCKED = 0.04, RAIN_UNDER = 0.10;
    ${section("let micPending", "async function send()")}
    ${section("async function keyDown()", "// The splash always")}
    ${section("async function turnOn(resume)", 'splash.addEventListener("click"')}
  `, context);
  return { $, errors, recordings, requests: () => requests, connections: () => connections,
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
