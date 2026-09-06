import assert from "node:assert/strict";
import test from "node:test";
import { SessionPickerController } from "../src/cabin_fever_x86_core/web_gateway/static/session-picker.js";

const older = { session_id: "11111111-1111-4111-8111-111111111111", modified: "2026-01-01T12:00:00Z" };
const newer = { session_id: "22222222-2222-4222-8222-222222222222", modified: "2026-01-02T12:00:00Z" };

function ui({ sessions = [], preferredSession = null, onStart = async () => {} } = {}) {
  const elements = new Map();
  const element = () => ({
    value: "", textContent: "", hidden: false, disabled: false, children: [], events: {},
    addEventListener(name, fn) { this.events[name] = fn; },
    replaceChildren() { this.children = []; },
    append(child) { this.children.push(child); },
  });
  const document = {
    createElement: element,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
  };
  let status = 200, expired = 0;
  const calls = [];
  const controller = new SessionPickerController({
    document, preferredSession, onStart,
    onExpired: () => { expired++; },
    fetch: async (path, options) => {
      calls.push({ path, options });
      return { ok: status === 200, status, json: async () => ({ sessions }) };
    },
  });
  return { controller, element: id => document.getElementById(id), calls,
    respond: code => { status = code; }, expired: () => expired };
}

test("an empty account offers only New Session and does not open a game until submitted", async () => {
  const started = [];
  const page = ui({ onStart: async resume => started.push(resume) });
  await page.controller.load();
  assert.deepEqual(page.element("session-choice").children.map(option => option.textContent), ["New Session"]);
  assert.match(page.element("sessions-status").textContent, /No saved sessions/);
  assert.equal(page.element("session-start").disabled, false);
  assert.deepEqual(started, []);
  await page.controller.start();
  assert.deepEqual(started, [null]);
  assert.equal(page.calls[0].path, "/sessions");
  assert.equal(page.calls[0].options.cache, "no-store");
});

test("saved sessions are newest first and the selected ID is passed to resume", async () => {
  const started = [];
  const page = ui({ sessions: [older, newer], onStart: async resume => started.push(resume) });
  await page.controller.load();
  assert.deepEqual(page.element("session-choice").children.map(option => option.value), ["", newer.session_id, older.session_id]);
  page.element("session-choice").value = older.session_id;
  page.element("session-choice").events.change();
  assert.equal(page.element("session-start").textContent, "Resume session");
  await page.controller.start();
  assert.deepEqual(started, [older.session_id]);
});

test("resume links only preselect sessions returned for the current user", async () => {
  for (const [preferredSession, expected] of [[older.session_id, older.session_id], [newer.session_id, ""]]) {
    const page = ui({ sessions: [older], preferredSession });
    await page.controller.load();
    assert.equal(page.element("session-choice").value, expected);
  }
});

test("failed listing cannot start a game and can be retried", async () => {
  const started = [];
  const page = ui({ onStart: async resume => started.push(resume) });
  page.respond(502);
  await page.controller.load();
  assert.match(page.element("sessions-error").textContent, /Could not load/);
  assert.equal(page.element("sessions-retry").hidden, false);
  assert.equal(page.element("session-start").disabled, true);
  await page.controller.start();
  assert.deepEqual(started, []);
  page.respond(200);
  await page.controller.load();
  assert.equal(page.element("sessions-error").textContent, "");
  assert.equal(page.element("session-start").disabled, false);
});

test("expired authentication returns to sign-in", async () => {
  for (const status of [401, 403]) {
    const page = ui();
    page.respond(status);
    await page.controller.load();
    assert.equal(page.expired(), 1);
    assert.equal(page.element("session-start").disabled, true);
  }
});

test("double submission opens one game and a failed start permits retry", async () => {
  let rejectStart, calls = 0;
  const page = ui({ onStart: () => {
    calls++;
    return new Promise((_, reject) => { rejectStart = reject; });
  } });
  await page.controller.load();
  const pending = page.controller.start();
  assert.equal(page.element("session-start").disabled, true);
  await page.controller.start();
  assert.equal(calls, 1);
  rejectStart(new Error("Microphone access is needed"));
  await pending;
  assert.match(page.element("sessions-error").textContent, /Microphone/);
  assert.equal(page.element("session-start").disabled, false);
});
