import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { BrowserAuthController } from "../src/cabin_fever_x86_core/web_gateway/static/browser-auth.js";

function ui(state = {}) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        hidden: false, disabled: false, value: "", textContent: "", attributes: {},
        events: {}, addEventListener(name, listener) { this.events[name] = listener; },
        setAttribute(name, value) { this.attributes[name] = value; },
      });
      return elements.get(id);
    },
  };
  const calls = [], timers = [];
  let response = { status: 200, detail: "" }, expired = 0, signedIn = 0;
  const controller = new BrowserAuthController({
    document,
    fetch: async (path, options) => {
      calls.push({ path, ...options });
      return { ok: response.status < 400, status: response.status,
        json: async () => path === "/auth" ? state : { detail: response.detail } };
    },
    setInterval: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearInterval: () => {},
    onExpired: () => { expired++; },
    onSignedIn: () => { signedIn++; },
  });
  return { controller, calls, timers, state,
    element: id => document.getElementById(id),
    respond: value => { response = value; }, expired: () => expired,
    signedIn: () => signedIn };
}

test("sole configured guest keeps the existing click-to-turn-on radio", async () => {
  const page = ui({ authenticated: true, implicit_guest: true, guest_available: true, refresh_seconds: 300 });
  await page.controller.initialize();
  assert.equal(page.element("radio-call").hidden, false);
  assert.equal(page.element("auth-panel").hidden, true);
  assert.equal(page.element("logout").hidden, true);
  assert.equal(page.element("splash").attributes.role, "button");
  assert.equal(page.signedIn(), 0);
});

test("default browser APIs keep their global receiver during initialization and refresh", async (t) => {
  const page = ui();
  const calls = [], cleared = [], timers = [];
  t.mock.method(globalThis, "fetch", async function (path) {
    assert.equal(this, globalThis);
    calls.push(path);
    return {
      ok: true, status: 200,
      json: async () => ({ authenticated: true, implicit_guest: true, refresh_seconds: 300 }),
    };
  });
  t.mock.method(globalThis, "setInterval", function (callback, delay) {
    assert.equal(this, globalThis);
    timers.push({ callback, delay });
    return timers.length;
  });
  t.mock.method(globalThis, "clearInterval", function (timer) {
    assert.equal(this, globalThis);
    cleared.push(timer);
  });
  const controller = new BrowserAuthController({
    document: { getElementById: page.element },
  });

  await controller.initialize();
  assert.equal(page.element("auth-error").textContent, "");
  assert.equal(page.element("auth-retry").hidden, true);
  assert.equal(controller.authenticated, true);
  assert.equal(timers[0].delay, 300000);
  await timers[0].callback();
  assert.deepEqual(calls, ["/auth", "/auth/refresh"]);
  await controller.initialize();
  assert.deepEqual(cleared, [null, 1]);
  assert.equal(timers.length, 2);
});

test("multiple users require identification and expose only configured guest access", async () => {
  for (const guest_available of [true, false]) {
    const page = ui({ authenticated: false, login_available: true, guest_available });
    await page.controller.initialize();
    assert.equal(page.element("radio-call").hidden, true);
    assert.equal(page.element("login-form").hidden, false);
    assert.equal(page.element("guest-login").hidden, !guest_available);
    assert.equal(page.element("splash").attributes.role, "group");
  }
});

test("no browser identities gives an explicit denial without a login or guest button", async () => {
  const page = ui({ authenticated: false, login_available: false, guest_available: false });
  await page.controller.initialize();
  assert.equal(page.element("login-form").hidden, true);
  assert.equal(page.element("guest-login").hidden, true);
  assert.match(page.element("auth-message").textContent, /No browser access/);
});

test("login sends callsign as username, clears the password, and never falls back to guest", async () => {
  const page = ui({ authenticated: false, login_available: true, guest_available: true });
  await page.controller.initialize();
  page.element("callsign").value = "  Night Owl  ";
  page.element("password").value = "wrong password";
  page.respond({ status: 401, detail: "Callsign or password not recognized" });
  assert.equal(await page.controller.login(), false);
  assert.deepEqual(JSON.parse(page.calls.at(-1).body), { username: "Night Owl", password: "wrong password" });
  assert.equal(page.element("password").value, "");
  assert.match(page.element("auth-error").textContent, /not recognized/);
  assert.equal(page.calls.some(call => call.path === "/auth/guest"), false);
  assert.equal(page.element("login-submit").disabled, false);
  assert.equal(page.controller.authenticated, false);
  assert.equal(page.signedIn(), 0);
});

test("successful sign-in enables the radio, logout, and periodic HTTP refresh", async () => {
  const page = ui({ authenticated: false, login_available: true, refresh_seconds: 20 });
  await page.controller.initialize();
  page.state.authenticated = true;
  assert.equal(await page.controller.login(), true);
  assert.equal(page.signedIn(), 1);
  assert.equal(page.element("radio-call").hidden, false);
  assert.equal(page.element("logout").hidden, false);
  assert.equal(page.timers.at(-1).delay, 20000);
  await page.timers.at(-1).callback();
  assert.equal(page.calls.at(-1).path, "/auth/refresh");
  assert.equal(page.calls.at(-1).method, "POST");
  page.respond({ status: 401 });
  await page.controller.refresh();
  assert.equal(page.expired(), 1);
  assert.equal(page.controller.authenticated, false);
});

test("guest selection and logout use explicit POST requests", async () => {
  const page = ui({ authenticated: false, guest_available: true, refresh_seconds: 20 });
  await page.controller.guest();
  assert.equal(page.calls[0].path, "/auth/guest");
  assert.equal(page.calls[0].method, "POST");
  await page.controller.logout();
  assert.ok(page.calls.find(call => call.path === "/auth/logout" && call.method === "POST"));
  assert.equal(page.expired(), 1);
  assert.equal(page.signedIn(), 0);
});

test("explicit guest sign-in opens the radio immediately, but logout does not", async () => {
  const page = ui({ authenticated: true, guest_available: true, refresh_seconds: 20 });
  await page.controller.guest();
  assert.equal(page.signedIn(), 1);
  page.state.authenticated = false;
  await page.controller.logout();
  assert.equal(page.signedIn(), 1);
});

test("connection and rate-limit failures stay readable and retryable", async () => {
  const page = ui();
  page.respond({ status: 403 });
  await page.controller.initialize();
  assert.equal(page.element("auth-retry").hidden, false);
  assert.match(page.element("auth-error").textContent, /station/);
  page.respond({ status: 429, detail: "Too many sign-in attempts. Wait a minute and try again." });
  await page.controller.login();
  assert.match(page.element("auth-error").textContent, /Wait a minute/);
  assert.equal(page.element("guest-login").disabled, false);
});

test("the page labels callsign/password accessibly and preserves the original radio prompt", () => {
  const html = readFileSync(new URL("../src/cabin_fever_x86_core/web_gateway/static/index.html", import.meta.url), "utf8");
  assert.match(html, /<label for="callsign">Callsign<\/label>/);
  assert.match(html, /<label for="password">Password<\/label>/);
  assert.match(html, /autocomplete="current-password"/);
  assert.match(html, /role="alert" aria-live="polite"/);
  assert.match(html, /Click here to turn on your radio/);
  assert.match(html, /if \(turningOn \|\| !browserAuth.authenticated\) return/);
  assert.match(html, /onSignedIn: \(\) => turnOn\(\)/);
});
