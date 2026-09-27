import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../src/cabin_fever_x86_core/web_gateway/static/index.html", import.meta.url), "utf8");
const source = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/import .*?;\n/g, "").replace("\nopenWeather();", "");

function page() {
  const elements = new Map(), timers = new Map(), listeners = new Map();
  const sockets = [];
  let nextTimer = 0, reloads = 0;
  const element = () => ({ textContent: "", disabled: false, children: [],
    classList: { add() {}, remove() {} }, addEventListener() {},
    append(child) { this.children.push(child); } });
  const addEventListener = (name, callback) => listeners.set(name, callback);
  const document = { visibilityState: "visible", addEventListener, createElement: element,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    } };
  const setTimer = (callback, delay, repeat = false) => {
    timers.set(++nextTimer, { callback, delay, repeat });
    return nextTimer;
  };
  const scope = vm.createContext({ document, URLSearchParams, ArrayBuffer, Blob, console,
    location: { search: "", protocol: "https:", host: "radio.example", reload() { reloads++; } },
    BrowserAuthController: class {
      authenticated = true;
      state = { implicit_guest: true };
      constructor(options) { Object.assign(this, options); }
      async initialize() {}
    },
    SessionPickerController: class {},
    WebSocket: class {
      readyState = 0;
      sent = [];
      constructor(url) { this.url = url; sockets.push(this); }
      send(text) {
        if (this.readyState !== 1) throw new Error("Socket is not open");
        this.sent.push(text);
      }
      // Closing handshakes may stall; tests deliver the close event separately.
      close() { this.readyState = 2; }
      closed(code = 1006) { this.readyState = 3; this.onclose({ code }); }
      message(data) { this.onmessage({ data }); }
      open() { this.readyState = 1; this.onopen(); }
      session(id = "saved-session") {
        if (this.readyState === 0) this.open();
        this.message(JSON.stringify({ type: "session", session_id: id, voice: true,
          owner_token: "test-owner-token", connection_id: "test-connection" }));
      }
    },
    addEventListener, setTimeout: setTimer, clearTimeout: id => timers.delete(id),
    setInterval: (callback, delay) => setTimer(callback, delay, true),
    clearInterval: id => timers.delete(id),
  });
  vm.runInContext(source, scope);
  const run = code => vm.runInContext(code, scope);
  const tick = delay => {
    const match = [...timers].find(([, timer]) => timer.delay === delay);
    assert.ok(match, `Expected a ${delay}ms timer`);
    const [id, timer] = match;
    if (!timer.repeat) timers.delete(id);
    timer.callback();
  };
  return { sockets, timers, run, tick, document,
    element: id => document.getElementById(id),
    event: name => listeners.get(name)(), reloads: () => reloads,
    async start(id) {
      const ready = run("connect()");
      sockets.at(-1).session(id);
      await ready;
      return sockets.at(-1);
    } };
}

test("a dropped socket resumes the confirmed session and keeps the transcript", async () => {
  const p = page(), socket = await p.start("session / with spaces");
  socket.message(JSON.stringify({ type: "assistant", text: "Keep going." }));
  socket.closed();
  assert.equal(p.element("talk").disabled, true);
  assert.equal(p.element("status").textContent, "reconnecting…");
  p.tick(1000);
  const replacement = p.sockets.at(-1);
  assert.equal(replacement.url, "wss://radio.example/ws?protocol=2");
  replacement.open();
  assert.deepEqual(JSON.parse(replacement.sent[0]), { type: "open", resume: "session / with spaces",
    mode: "recover", owner_token: "test-owner-token" });
  assert.equal(p.element("talk").disabled, true);
  replacement.session("session / with spaces");
  assert.equal(p.element("talk").disabled, false);
  assert.equal(p.element("status").textContent, " ");
  assert.equal(p.element("log").children[0].textContent, "Keep going.");
  assert.equal(p.reloads(), 0);
});

test("failed retries back off to 30 seconds and success resets the delay", async () => {
  const p = page(), socket = await p.start();
  socket.closed(1000); // The gateway also closes normally when its upstream goes away.
  for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
    p.tick(delay);
    p.sockets.at(-1).closed();
  }
  p.tick(30000);
  p.sockets.at(-1).session();
  p.sockets.at(-1).closed();
  p.tick(1000);
  assert.equal(p.sockets.length, 10);
  assert.ok(p.sockets.slice(1).every(socket => socket.url.endsWith("/ws?protocol=2")));
});

test("failed initial connections are left for the session picker to retry", async () => {
  for (const failure of ["close", "timeout", "error"]) {
    const p = page(), ready = p.run("connect()");
    const rejected = assert.rejects(ready, /Could not open|too long|No such session/);
    const socket = p.sockets[0];
    if (failure === "close") socket.closed();
    if (failure === "timeout") p.tick(30000);
    if (failure === "error") socket.message(JSON.stringify({ type: "error", text: "No such session" }));
    await rejected;
    assert.equal(p.timers.size, 0);
    assert.equal(p.sockets.length, 1);
  }
});

test("retry setup timeouts and gateway errors schedule another attempt", async () => {
  const p = page(), socket = await p.start();
  socket.closed();
  p.tick(1000);
  p.tick(30000); // Timed out before the gateway returned a session, without a close event.
  assert.equal(p.sockets.at(-1).readyState, 2);
  p.tick(2000);
  const stalled = p.sockets.at(-1);
  stalled.open();
  stalled.message(JSON.stringify({ type: "error", text: "Cannot reach the game. Please try again." }));
  stalled.closed(1011);
  assert.equal(p.run("ownerToken"), "test-owner-token");
  assert.equal(p.run("resumeRequired"), false);
  p.tick(4000);
  const recovered = p.sockets.at(-1);
  recovered.open();
  assert.deepEqual(JSON.parse(recovered.sent[0]), { type: "open", resume: "saved-session",
    mode: "recover", owner_token: "test-owner-token" });
  recovered.session();
  assert.equal(p.element("talk").disabled, false);
});

test("unanswered heartbeats replace sockets even if the close handshake never completes", async () => {
  const p = page(), socket = await p.start();
  p.tick(20000);
  assert.deepEqual(socket.sent.slice(1), ["."]);
  p.tick(10000);
  assert.equal(socket.readyState, 2);
  assert.equal(p.element("talk").disabled, true);
  p.tick(1000);
  assert.equal(p.sockets.length, 2);
  p.sockets[1].session();
  // Late events from the abandoned socket must not affect the new channel.
  socket.session("wrong-session");
  socket.message(JSON.stringify({ type: "assistant", text: "Stale reply" }));
  socket.onerror();
  socket.closed(4401);
  assert.equal(p.run("sessionId"), "saved-session");
  assert.equal(p.element("log").children.length, 0);
  assert.equal(p.element("talk").disabled, false);
  assert.equal(p.element("status").textContent, " ");
  assert.equal(p.reloads(), 0);
});

test("heartbeat replies keep a quiet session connected", async () => {
  const p = page(), socket = await p.start();
  for (let i = 0; i < 3; i++) {
    p.tick(20000);
    socket.message(JSON.stringify({ type: "pong" }));
    assert.equal([...p.timers.values()].some(timer => timer.delay === 10000), false);
  }
  assert.equal(p.sockets.length, 1);
  assert.equal(p.element("talk").disabled, false);
});

test("waking probes an open connection and accelerates a pending retry without duplicates", async () => {
  const p = page();
  p.event("online");
  assert.equal(p.sockets.length, 0);
  const socket = await p.start();
  p.document.visibilityState = "hidden";
  p.event("visibilitychange");
  assert.deepEqual(socket.sent.slice(1), []);
  p.document.visibilityState = "visible";
  p.event("visibilitychange");
  p.event("online");
  assert.deepEqual(socket.sent.slice(1), ["."]);
  socket.closed();
  p.event("online");
  p.event("visibilitychange");
  assert.equal(p.timers.size, 1);
  p.tick(0);
  p.event("online");
  assert.equal(p.sockets.length, 2);
});

test("expired authentication and logout stop reconnection", async () => {
  const p = page(), socket = await p.start();
  socket.closed(4401);
  assert.equal(p.reloads(), 1);
  assert.equal(p.timers.size, 0);
  p.event("online");
  assert.equal(p.sockets.length, 1);

  const pending = page(), oldSocket = await pending.start();
  oldSocket.closed();
  pending.run("browserAuth.onExpired()");
  assert.equal(pending.timers.size, 0);
  pending.event("online");
  assert.equal(pending.sockets.length, 1);
});

test("an interrupted recording is discarded and the next press reacquires the microphone", async () => {
  const p = page(), socket = await p.start();
  p.run(`globalThis.take = { uploaded: false, stopped: false, trackStopped: false,
    state: "recording", onstop() { this.uploaded = true; }, ondataavailable() {},
    stop() { this.stopped = true; this.onstop?.(); },
    stream: { getTracks: () => [{ stop() { take.trackStopped = true; } }] },
  };
  recorder = take; keyed = true; talkHeld = true; chunks = ["partial audio"];`);
  socket.closed();
  assert.equal(p.run("take.stopped && take.trackStopped"), true);
  assert.equal(p.run("take.uploaded"), false);
  assert.equal(p.run("take.ondataavailable"), null);
  assert.equal(p.run("recorder"), null);
  assert.equal(p.run("keyed || talkHeld || chunks.length > 0"), false);
  p.tick(1000);
  p.sockets.at(-1).session();
  p.run(`ensureMic = async () => {
    globalThis.micRequested = true;
    recorder = { start() {} };
    return true;
  };`);
  await p.run("keyDown()");
  assert.equal(p.run("micRequested && keyed"), true);
});

test("an upload finishing after disconnection preserves the reconnecting status", async () => {
  const p = page(), socket = await p.start();
  p.run(`recorder = { mimeType: "audio/webm", stream: { getTracks: () => [] } };
    chunks = [new Blob(["voice"])];
    fetch = () => new Promise(resolve => { globalThis.finishUpload = resolve; });`);
  const upload = p.run("send()");
  socket.closed();
  p.run('finishUpload({ ok: true, json: async () => ({ text: "Hello" }) })');
  await upload;
  assert.equal(p.element("status").textContent, "reconnecting…");
});

test("displacement stops retries and wake events until Resume here is clicked", async () => {
  const p = page(), socket = await p.start();
  socket.closed(4001);
  assert.equal(p.timers.size, 0);
  assert.equal(p.element("resume-radio").hidden, false);
  p.event("online"); p.event("visibilitychange");
  assert.equal(p.sockets.length, 1);
  const resume = p.element("resume-radio").onclick();
  await Promise.resolve();
  const replacement = p.sockets.at(-1);
  replacement.open();
  assert.deepEqual(JSON.parse(replacement.sent[0]), { type: "open", resume: "saved-session",
    mode: "takeover", owner_token: null });
  replacement.session();
  await resume;
  assert.equal(p.element("resume-radio").hidden, true);
  replacement.closed();
  p.tick(1000);
  assert.equal(p.sockets.length, 3);
});

test("a stale recovery token requires explicit resume even if displacement was missed", async () => {
  const p = page(), socket = await p.start();
  socket.closed();
  p.tick(1000);
  p.sockets.at(-1).open();
  assert.equal(JSON.parse(p.sockets.at(-1).sent[0]).owner_token, "test-owner-token");
  p.sockets.at(-1).message(JSON.stringify({ type: "error", text: "Explicitly resume", code: "resume_required" }));
  assert.equal(p.timers.size, 0);
  assert.equal(p.element("resume-radio").hidden, false);
  p.event("online"); p.event("visibilitychange");
  assert.equal(p.sockets.length, 2);
});

test("takes carry ownership and connection IDs outside the URL", async () => {
  const p = page();
  await p.start();
  p.run(`recorder = { mimeType: "audio/webm" }; chunks = [new Blob(["voice"])];
    fetch = async (url, options) => { globalThis.upload = { url, options };
      return { ok: true, json: async () => ({ text: "hello" }) }; };`);
  await p.run("send()");
  assert.equal(p.run("upload.url"), "/takes/saved-session");
  assert.equal(p.run('upload.options.headers["X-CF86-Owner-Token"]'), "test-owner-token");
  assert.equal(p.run('upload.options.headers["X-CF86-Connection"]'), "test-connection");
});
